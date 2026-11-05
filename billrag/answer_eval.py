"""Answer-level evaluation: run the pipeline (or agent) on eval questions and score the *answers*.

Per question:
  strict      correct (judge) AND cited the gold clause/bill: the headline number
  cited_gold  the answer cites the gold clause (clause-level) or a gold bill (bill-level)   [objective]
  cand_gold   (pipeline) the gold was among the retrieved candidates
  kept_gold   (pipeline) the gold survived the AI grading step into the evidence
  verdict     LLM judge vs the reference answer: correct / partial / incorrect              [same model: indicative only]
  seconds, llm_calls
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

from .pipeline import CITE, LEAK, STRICT, parse_json

JUDGE = """You are grading an answer to a question about a Sri Lankan bill against a reference answer taken from the bill text.

QUESTION: {question}
REFERENCE ANSWER: {reference}
CANDIDATE ANSWER:
{answer}

Verdict: "correct" = states the key facts of the reference without contradicting it (extra correct detail is fine);
"partial" = right bill/topic but misses or garbles a key fact; "incorrect" = wrong, contradicts the reference, or says it
cannot find the answer. Return ONLY JSON: {{"verdict": "correct"}}"""


def _gold_pairs(item):
    return {(g["bill"], g.get("section")) for g in item["gold"]}


def cites_gold(item, answer):
    cites = [(k, int(s) if s else None) for k, s in CITE.findall(answer)]
    gold = _gold_pairs(item)
    if item["level"] == "bill":
        return any(k in {b for b, _ in gold} for k, _ in cites)
    return any((k, s) in gold for k, s in cites)


def chunk_has_gold(item, chunk):
    pairs = {(chunk["bill_key"], chunk.get("section_no"))} | {(b, chunk.get("section_no")) for b in chunk.get("also_in", [])}
    gold = _gold_pairs(item)
    if item["level"] == "bill":
        return bool({b for b, _ in pairs} & {b for b, _ in gold})
    return bool(pairs & gold)


def expand_gold(store, item):
    """Count the same section of sibling versions (GS vs Bill of the same title, within 300 days) as correct too:
    their text differs slightly, so the identical-text gold from make_eval misses them."""
    gold = list(item["gold"])
    have = _gold_pairs(item)
    for g in item["gold"]:
        b = store.bill(g["bill"])
        if not b:
            continue
        for v in store.versions(b["title_norm"], b["date"]):
            if (v["key"], g.get("section")) not in have:
                gold.append({"bill": v["key"], **({"section": g["section"]} if "section" in g else {})})
                have.add((v["key"], g.get("section")))
    return {**item, "gold": gold}


def select(items, limit):
    if not limit or limit >= len(items):
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def run_one(item, make_engine, llm, judge_effort="low"):
    t = time.time()
    row = {"id": item["id"], "type": item["type"], "level": item["level"], "question": item["question"]}
    try:
        eng = make_engine()
        out = eng.ask(item["question"])
        if hasattr(out, "answer"):  # pipeline Result
            row.update(answer=out.answer, llm_calls=out.llm_calls, warnings=out.warnings, enough=out.enough,
                       cand_gold=any(chunk_has_gold(item, e.chunk) for e in out.graded),
                       kept_gold=any(chunk_has_gold(item, e.chunk) for e in out.evidence))
        else:  # agent: plain text
            row.update(answer=LEAK.split(out)[0].strip() or out)
    except Exception as e:  # one failing question must not abort the run
        row.update(answer="", error=f"{type(e).__name__}: {str(e)[:150]}")
    row["seconds"] = round(time.time() - t, 1)
    row["cited_gold"] = cites_gold(item, row["answer"])
    row["verdict"] = "error"
    if row["answer"]:
        prompt = JUDGE.format(question=item["question"], reference=item["answer"], answer=row["answer"][:3500])
        row["verdict"] = "unjudged"
        for attempt, (suffix, eff, mt) in enumerate((("", judge_effort, 600), (STRICT, None, 1200))):
            try:
                txt = llm.chat("You output only valid JSON.", [{"role": "user", "content": prompt + suffix}],
                               max_tokens=mt, effort=eff).text
                v = (parse_json(txt) or {}).get("verdict", "")
            except Exception:
                v = ""
            if v in ("correct", "partial", "incorrect"):
                row["verdict"] = v
                break
    return row


def run(items, make_engine, llm, workers=3):
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda it: run_one(it, make_engine, llm), items))


def summarize(rows):
    def agg(rs):
        n = len(rs) or 1
        out = {"n": len(rs), "cited_gold": sum(r["cited_gold"] for r in rs) / n,
               "strict": sum(r["cited_gold"] and r["verdict"] == "correct" for r in rs) / n,
               "correct": sum(r["verdict"] == "correct" for r in rs) / n,
               "correct_or_partial": sum(r["verdict"] in ("correct", "partial") for r in rs) / n,
               "avg_seconds": sum(r["seconds"] for r in rs) / n, "errors": sum(1 for r in rs if r.get("error"))}
        pr = [r for r in rs if "cand_gold" in r]
        if pr:
            out["cand_gold"] = sum(r["cand_gold"] for r in pr) / len(pr)
            out["kept_gold"] = sum(r["kept_gold"] for r in pr) / len(pr)
            out["avg_llm_calls"] = sum(r["llm_calls"] for r in pr) / len(pr)
        return out
    return {"all": agg(rows), "clause": agg([r for r in rows if r["level"] == "clause"]),
            "bill": agg([r for r in rows if r["level"] == "bill"])}


def format_report(summary, label):
    keys = ["n", "strict", "cited_gold", "correct", "avg_seconds"] + \
           [k for k in ("cand_gold", "kept_gold", "avg_llm_calls") if k in summary["all"]]
    lines = [f"== {label}", f"{'':8}" + "".join(f"{k:>20}" for k in keys)]
    for name in ("all", "clause", "bill"):
        s = summary[name]
        lines.append(f"{name:8}" + "".join(f"{s.get(k, 0):>20.2f}" if k != "n" else f"{s['n']:>20}" for k in keys))
    return "\n".join(lines)
