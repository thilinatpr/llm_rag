"""Two/three-turn eval of the bill-focused mode (Pipeline(whole_bill=True)) against the normal pipeline.

For each recent eval item, one chat:
  turn 1  "What is the <gold bill title> about?"      (title only, no number: the chat becomes about that bill)
  turn 2  the eval question                           (SCORED: judge + cited gold, like eval-answers)
  turn 3  a fixed generic follow-up                   (not scored: shows the cache effect on a third question)
Run once with WHOLE_BILL=0 and once with WHOLE_BILL=1 (same DeepSeek settings as the README), e.g.
  WHOLE_BILL=1 python scripts/experiments/whole_bill/followup_eval.py --workers 4 --out evals/answer_model_comparison/72.followup-wholebill.jsonl
"""
import argparse, dataclasses, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
from billrag import answer_eval as ae, calltrace  # noqa: E402
from billrag.config import load_settings  # noqa: E402
from billrag.evaluate import load_items  # noqa: E402
from billrag.llm import make_answer_llm, make_embedder, make_llm, make_reranker  # noqa: E402
from billrag.pipeline import Pipeline  # noqa: E402
from billrag.retrieve import Retriever  # noqa: E402
from billrag.store import Store  # noqa: E402

TURN3 = "What offences or penalties does this bill create, if any?"

ap = argparse.ArgumentParser()
ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--out", required=True)
a = ap.parse_args()
whole = os.environ.get("WHOLE_BILL", "0") == "1"
s = load_settings()
llm, answer_llm = make_llm(s), make_answer_llm(s)
local = threading.local()  # SQLite handles are per thread


def tools():
    if not hasattr(local, "st"):
        local.st = Store(s.db_path)
        local.r = Retriever(local.st, make_embedder(s), s.vectors_path, None, s.rerank, make_reranker(s))
    return local.st, local.r


extra = {}


class Chat:
    def __init__(self, item):
        self.item = item

    def ask(self, q):
        st, r = tools()
        p = Pipeline(st, r, llm, grade_effort=s.grade_effort, answer_effort=s.answer_effort, answer_llm=answer_llm,
                     whole_bill=whole)
        title = re.sub(r"\s*-\s*(GS|Bill)\s*$", "", st.bill(self.item["source"]["bill"])["title"]).strip()
        info = {}
        with calltrace.tracing() as tr:
            r1 = p.ask(f"What is the {title} about?")
        info["t1"] = {"path": r1.path, "focus": p.focus_key, "cost": calltrace.cost_summary(tr.calls)}
        t = time.time()
        with calltrace.tracing() as tr:
            r2 = p.ask(q)
        info["t2"] = {"path": r2.path, "whole_bill": r2.whole_bill, "cost": calltrace.cost_summary(tr.calls),
                      "seconds": round(time.time() - t, 1)}
        t = time.time()
        with calltrace.tracing() as tr:
            r3 = p.ask(TURN3)
        info["t3"] = {"path": r3.path, "whole_bill": r3.whole_bill, "cost": calltrace.cost_summary(tr.calls),
                      "seconds": round(time.time() - t, 1), "warnings": r3.warnings}
        extra[self.item["id"]] = info
        return r2


items = [ae.expand_gold(Store(s.db_path), i) for i in load_items("evals/retrieval_eval.jsonl") if i["source"]["year"] >= 2025]
items = items[: a.limit] if a.limit else items
e = os.environ
judge = make_llm(dataclasses.replace(s, chat_model=e["BILLRAG_JUDGE_MODEL"], chat_provider="openai",
                                     chat_base_url=e["BILLRAG_JUDGE_BASE_URL"], chat_api_key=e["BILLRAG_JUDGE_API_KEY"]))
t0 = time.time()
with ThreadPoolExecutor(a.workers) as ex:
    rows = list(ex.map(lambda it: ae.run_one(it, lambda: Chat(it), judge), items))
for row in rows:
    row.update(extra.get(row["id"], {}))
with open(a.out, "w", encoding="utf-8") as f:
    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
print(ae.format_report(ae.summarize(rows), f"follow-up turn 2, whole_bill={'on' if whole else 'off'}, n={len(rows)}"))


def usd(rs, t):
    v = [r[t]["cost"]["usd"] for r in rs if r.get(t, {}).get("cost")]
    return sum(v) / max(len(v), 1)


def hit(rs, t):
    c = [r[t]["cost"] for r in rs if r.get(t, {}).get("cost")]
    return sum(x["cache_hit"] for x in c) / max(sum(x["in"] for x in c), 1)


for t in ("t1", "t2", "t3"):
    rs = [r for r in rows if t in r]
    paths = {}
    for r in rs:
        paths[r[t]["path"]] = paths.get(r[t]["path"], 0) + 1
    secs = [r[t]["seconds"] for r in rs if "seconds" in r[t]]
    print(f"{t}: paths {paths}  avg usd {usd(rs, t):.5f}  cache hit {hit(rs, t):.0%}"
          + (f"  avg s {sum(secs) / len(secs):.1f}" if secs else ""))
print("focus set after turn 1:", sum(1 for r in rows if r.get("t1", {}).get("focus")), "/", len(rows))
fb = {}
for r in rows:
    w = (r.get("t2", {}).get("whole_bill") or {}).get("fallback")
    if w:
        fb[w] = fb.get(w, 0) + 1
print("turn 2 fallbacks:", fb, f"| wall {time.time() - t0:.0f}s")
