"""Gist-question eval ("key provisions of / summarise / explain X", X named by number, title, partial title or typo).

Gold (evals/gist_eval.jsonl) is mechanical: `accept` = keys that are the named instrument (its bill versions and Act), `must` = groups of
words of which one must appear in the answer, written from the official long title and checked to occur in the accepted keys' own text.
Per question: cites_target (an accepted key is cited in the answer), facts (share of `must` groups present), hedged (grader said the
evidence was not enough, or the answer says so), warnings (unsupported citations), clarify (the answer asks which bill). An ambiguous item
passes if the answer asks which bill or cites an accepted key. strict = cites_target and every fact and no warning (or a clarify on an
ambiguous item). Run from the checkout to be scored:  python scripts/eval_gist.py --out rows.jsonl [--runs 2]
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.getcwd())
HEDGE = re.compile(r"(evidence|passages?|excerpts?) (does|do) not|not in the (evidence|passages)|cannot (give|say|tell|provide)|"
                   r"only (partly|partially)|partial (account|summary)|not a complete|no (overview|summary)", re.I)


def mentions(answer, key):
    if key.startswith("Act "):
        n, y = key[4:].split("/")
        return bool(re.search(rf"Act\s*(?:No\.?\s*)?{int(n)}\s*(?:/|of)\s*{y}", answer, re.I))
    return key in answer


def score(item, res):
    a = res.answer or ""
    low = a.lower()
    clar = bool(getattr(res, "clarify", False))
    cites = any(mentions(a, k) for k in item["accept"])
    facts = sum(any(w in low for w in g) for g in item["must"]) / len(item["must"])
    hedged = (not res.enough) or bool(HEDGE.search(a))
    warn = len(res.warnings or [])
    ok = (clar and item["ambiguous"]) or (cites and facts == 1 and not warn and not clar)
    return {"cites": cites, "facts": facts, "hedged": hedged and not clar, "warn": warn, "clarify": clar, "strict": ok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--gold", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "evals", "gist_eval.jsonl"))
    a = ap.parse_args()
    from billrag.config import load_settings
    from billrag.llm import make_answer_llm, make_embedder, make_llm, make_reranker
    from billrag.pipeline import Pipeline
    from billrag.retrieve import Retriever
    from billrag.store import Store
    s = load_settings()
    st = Store(s.db_path if hasattr(s, "db_path") else "data/bills/index.sqlite")
    llm, rt = make_llm(s), None
    rt = Retriever(st, make_embedder(s), s.vectors_path, None, s.rerank, make_reranker(s))
    items = [json.loads(l) for l in open(a.gold)]
    rows = []
    for run in range(a.runs):
        for it in items:
            p = Pipeline(st, rt, llm, grade_effort=s.grade_effort, answer_effort=s.answer_effort, answer_llm=make_answer_llm(s))
            try:
                r = p.ask(it["q"])
                row = {"q": it["q"], "run": run, **score(it, r), "answer": r.answer}
            except Exception as e:  # a provider failure is a result, not a crash
                row = {"q": it["q"], "run": run, "error": str(e)[:200], "strict": False, "cites": False, "facts": 0.0,
                       "hedged": False, "warn": 0, "clarify": False}
            rows.append(row)
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    n = len(rows)
    for k in ("strict", "cites", "hedged", "clarify"):
        print(f"{k:8s} {sum(bool(r[k]) for r in rows)}/{n}")
    print(f"facts    {sum(r['facts'] for r in rows) / n:.2f}   warnings {sum(r['warn'] for r in rows)}   errors {sum(1 for r in rows if 'error' in r)}")


if __name__ == "__main__":
    main()
