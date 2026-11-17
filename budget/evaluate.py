"""Answer eval for the Budget service (mechanical, no model judge): python -m budget.evaluate [--db ..] [--limit N]

Per item: figures present (regex per figure, so "Rs. 470.5 billion" and "Rs. 470,499,998,000" both count), the expected
citation present (any one of the listed labels), no warnings (unsupported citation, a figure not in the passages). `refuse` items must say the passages do not answer it (and carry no warning); `calc` items (need a sum no passage gives) must not state a sum: warnings are the signal.
Rows go to evals/budget_results.jsonl. This is a regression check, not an accuracy score: read the answers.
"""
import argparse
import json
import re
import sys
import time

from billrag.config import load_settings
from billrag.llm import make_llm

from . import ask as askmod
from . import index

NEG = re.compile(r"\b(?:not (?:in|among|included|give|given|contain|covered|printed|answer|set out)|no passage|do(?:es)? not (?:give|contain|answer|cover|include|set)|cannot|can't|nothing about)", re.I)


def check(item, res) -> dict:
    a = res.answer
    out = {"id": item["id"], "q": item["q"], "answer": a, "warnings": res.warnings, "seconds": res.seconds,
           "evidence": [u["label"] for u in res.evidence]}
    if item.get("refuse"):
        out["ok"] = bool(NEG.search(a)) and not res.warnings  # says it is not in the passages; related printed figures are fine if verified
    elif item.get("calc"):
        out["ok"] = not res.warnings  # a sum the model made itself is flagged by verify_numbers
        out["note"] = "calc: check by hand that it says no total is printed or shows only printed figures"
    else:
        figs = all(re.search(p, a) for p in item["must_re"])
        cited = not item.get("cite") or any(c.lower() in a.lower() for c in item["cite"])  # any of the labels that hold the figure
        out.update(figures=figs, cited=cited, ok=figs and cited and not res.warnings)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/budget/index.sqlite")
    ap.add_argument("--gold", default="evals/budget_answers.jsonl")
    ap.add_argument("--out", default="evals/budget_results.jsonl")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--label", default="run")
    a = ap.parse_args(argv)
    db = index.connect(a.db)
    llm = make_llm(load_settings())
    items = [json.loads(line) for line in open(a.gold)]
    if a.limit:
        items = items[: a.limit]
    rows = []
    for it in items:
        rows.append(check(it, askmod.ask(db, llm, it["q"])))
        r = rows[-1]
        print(f"{'ok  ' if r['ok'] else 'FAIL'} {r['id']:4} {r['seconds']:5.1f}s {r['warnings'] or ''}", flush=True)
    with open(a.out, "a") as fh:
        for r in rows:
            fh.write(json.dumps({**r, "label": a.label, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
    print(f"{sum(r['ok'] for r in rows)}/{len(rows)} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
