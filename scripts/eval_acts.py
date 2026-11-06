#!/usr/bin/env python3
"""Bill-vs-Act answer check: `python scripts/eval_acts.py [--out rows.jsonl]` (needs a chat model; ~12 questions, ~40 LLM calls).

Each item in evals/act_eval.jsonl names citations the answer must contain and phrases it must (not) contain. These are mechanical
checks of a small hand-written set: read the printed answers too, because a phrase match does not prove an answer is right."""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from billrag import translate  # noqa: E402
from billrag.config import load_settings  # noqa: E402
from billrag.llm import make_answer_llm, make_embedder, make_llm, make_reranker  # noqa: E402
from billrag.pipeline import CITE, Pipeline  # noqa: E402
from billrag.retrieve import Retriever  # noqa: E402
from billrag.store import Store  # noqa: E402


def check(item, answer):
    cited = {k for k, _ in CITE.findall(answer)} | {f"{k} \u00a7{s}" for k, s in CITE.findall(answer) if s}
    need = item.get("must_cite") or []
    ok_cite = (any(c in cited for c in need) if item.get("must_any_cite") else all(c in cited for c in need)) if need else True
    ok_say = not item.get("must_say") or bool(re.search(item["must_say"], answer, re.I))
    ok_not = not item.get("must_not_say") or not re.search(item["must_not_say"], answer, re.I)
    return {"cite": ok_cite, "say": ok_say, "not_say": ok_not, "pass": ok_cite and ok_say and ok_not}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="evals/act_eval.jsonl")
    ap.add_argument("--out")
    a = ap.parse_args()
    items = [json.loads(x) for x in Path(a.file).read_text(encoding="utf-8").splitlines() if x.strip()]
    s = load_settings()
    store, llm = Store(s.db_path), make_llm(s)
    retriever = Retriever(store, make_embedder(s), s.vectors_path, None, s.rerank, make_reranker(s))
    rows = []
    for it in items:
        pipe = Pipeline(store, retriever, llm, grade_effort=s.grade_effort, answer_effort=s.answer_effort, answer_llm=make_answer_llm(s),
                        translate_mode=s.translate, translator=translate.make_translator(s, llm))  # a fresh conversation per question
        res = pipe.ask(it["question"])
        c = check(it, res.answer)
        rows.append({**it, "answer": res.answer, "evidence": [f"{e.key} \u00a7{e.section}" for e in res.evidence], "acts": res.acts,
                     "warnings": res.warnings, "path": res.path, "check": c})
        print(f"\n[{'PASS' if c['pass'] else 'FAIL'}] {it['id']} {c} path={res.path} warnings={res.warnings}\nQ: {it['question']}\nA: {res.answer}\n"
              f"evidence: {rows[-1]['evidence']}", flush=True)
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"\n{sum(r['check']['pass'] for r in rows)}/{len(rows)} passed the mechanical checks")


if __name__ == "__main__":
    main()
