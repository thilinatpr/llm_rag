"""Answer eval for Procurement Rules: python -m procurement.evaluate [--runs 3] [--features both] [--out rows.jsonl]

Gold (evals/procurement_eval.jsonl, written by hand from the source text, not from model answers):
  gold  = passages that answer the question, any one counts, e.g. "PM2024 §2.9"
  facts = regexes the answer must all match (the amounts, periods and names that make it right); each must occur in the gold source
  says  = regexes the answer must also match but the source cannot contain, e.g. an admission that the rules do not cover a role
  kind  = "answer", or "gap": the documents do not settle it, so the answer must say so (`facts` then match that admission)
  history = earlier turns for a follow-up, as the page sends them
Scored without a model judge: evidence = a gold passage was shown; cited = the answer cites one; facts = all regexes match;
strict = facts and cited (for a gap: facts only); clean = strict with no unsupported citation. `warned` = any warning.
"""
import argparse
import json
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import ask as ask_mod
from .index import Index

EVAL = Path("evals/procurement_eval.jsonl")
_MAX_XREF, _CHAIN = ask_mod.MAX_XREF, Index.chain


def set_features(chain, xref):
    """Turn the heading chain (`Under: ...`) and clause pointers (`Referred to by ...`) on or off. Process-wide: one setting per run."""
    ask_mod.MAX_XREF = _MAX_XREF if xref else 0
    Index.chain = _CHAIN if chain else (lambda self, doc, ref: [])


def norm(s):
    return re.sub(r"\s+", " ", s.replace("Art.", "Art").replace("[", "").replace("]", "")).strip().lower()


def under(label, gold):
    """label is the gold passage or part of it: "pm2024 §2.9.1" under "pm2024 §2.9", "pfc0126 §2" under "pfc0126"."""
    return label == gold or label.startswith((gold + ".", gold + " "))


def shown(units, gold):
    return any(under(norm(u["label"]), norm(g)) for u in units for g in gold)


def cited(answer, gold):
    """A citation of a gold passage or of a sub-clause of it. [PG2024 §2.9] and [PG2024 §2.9.1] both count for gold PG2024 §2.9."""
    found = {norm(ask_mod.label(ask_mod._CANON.get(d.lower(), d), r or f)) for d, r, f in ask_mod.CITE.findall(answer)}
    return any(under(c, norm(g)) for g in gold for c in found)


def plain(text):
    """Models write narrow no-break spaces and non-breaking hyphens ("seven years", "beneficial-owner"): those are not wrong answers."""
    return re.sub(r"[\u2010-\u2015]", "-", re.sub(r"[\u00a0\u2000-\u200a\u202f\u205f]", " ", text))


def score(item, res):
    answer = plain(res.answer)
    required = item["facts"] + item.get("says", [])
    facts = all(re.search(f, answer, re.I | re.S) for f in required)
    gap = item.get("kind") == "gap"
    c = cited(res.answer, item["gold"]) if item["gold"] else True
    return dict(id=item["id"], question=item["question"], kind=item.get("kind", "answer"), evidence=shown(res.evidence, item["gold"]) if item["gold"] else True,
                cited=c, facts=facts, strict=facts if gap else facts and c, warned=bool(res.warnings),
                clean=(facts if gap else facts and c) and not any("unsupported" in w for w in res.warnings), warnings=res.warnings,
                seconds=res.seconds, answer=res.answer, labels=[u["label"] for u in res.evidence],
                missing=[f for f in required if not re.search(f, answer, re.I | re.S)])


def check_gold(index, items):
    """Every required fact must occur in the source text of the item's gold passages: [(id, problem)]. Keeps the gold honest."""
    bad = []
    for item in items:
        if item.get("kind") == "gap":
            continue
        text = ""
        for g in item["gold"]:
            m = re.fullmatch(r"(\w+)(?: (?:§|Art\. )?(.+))?", g)
            rows = index.db.execute("SELECT text, note FROM chunks WHERE doc=? AND (ref=? OR ? IS NULL)", (m[1], m[2], m[2])).fetchall()
            if not rows:
                bad.append((item["id"], f"no passage {g}"))
            text += " ".join(f"{r['text']} {r['note'] or ''}" for r in rows) + " "
        text = re.sub(r"\s+", " ", text)
        bad += [(item["id"], f"fact not in source: {f}") for f in item["facts"] if not re.search(f, text, re.I | re.S)]
    return bad


def run(make_index, llm, items, workers=6):
    local = threading.local()  # one SQLite connection per thread: sharing one gives InterfaceError under load (serve.py asks one at a time)

    def one(item):
        try:
            if not hasattr(local, "index"):
                local.index = make_index()
            return score(item, ask_mod.ask(local.index, llm, item["question"], history=item.get("history")))
        except Exception as e:  # a failed call is a wrong answer, not a dropped question
            return dict(id=item["id"], question=item["question"], kind=item.get("kind", "answer"), evidence=False, cited=False, facts=False,
                        strict=False, clean=False, warned=True, warnings=[f"{type(e).__name__}: {e}"], seconds=0.0, answer="", labels=[], missing=item["facts"])
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, items))


def summary(rows):
    n = len(rows) or 1
    out = {m: round(sum(bool(r[m]) for r in rows) / n, 3) for m in ("evidence", "cited", "facts", "strict", "clean", "warned")}
    return dict(n=len(rows), **out, median_s=round(statistics.median(r["seconds"] for r in rows), 1) if rows else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="procurement.evaluate")
    ap.add_argument("--file", default=str(EVAL)); ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--features", choices=["on", "off", "both"], default="on"); ap.add_argument("--out")
    ap.add_argument("--only", help="comma-separated ids"); ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--check-gold", action="store_true", help="only check the gold against the source text (no model call)")
    a = ap.parse_args(argv)
    from billrag.config import load_settings
    from billrag.llm import make_llm, make_reranker
    items = [json.loads(l) for l in open(a.file, encoding="utf-8") if l.strip()]
    if a.only:
        items = [i for i in items if i["id"] in a.only.split(",")]
    if a.check_gold:
        bad = check_gold(Index(), items)
        for b in bad:
            print(*b)
        print(f"{len(items)} items, {len(bad)} problems")
        return 1 if bad else 0
    s = load_settings()
    reranker, llm = make_reranker(s), make_llm(s)
    settings = {"on": [True], "off": [False], "both": [False, True]}[a.features]
    out = open(a.out, "w", encoding="utf-8") if a.out else None
    for run_no in range(a.runs):  # alternate, so hosted-model drift over the session does not favour one setting
        for on in (settings if run_no % 2 == 0 else settings[::-1]):
            set_features(on, on)
            rows = run(lambda: Index(reranker=reranker), llm, items, a.workers)
            print(f"run {run_no + 1} features={'on' if on else 'off'}: {summary(rows)}", flush=True)
            for r in rows:
                if out:
                    out.write(json.dumps(dict(r, run=run_no + 1, features="on" if on else "off"), ensure_ascii=False) + "\n")
            if out:
                out.flush()
    if out:
        out.close()


if __name__ == "__main__":
    main()
