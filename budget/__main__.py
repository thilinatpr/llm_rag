"""python -m budget build | search "query" | check"""
import argparse
import sys

from . import index, parse

DATA = "data/budget_src/pdfs"
BILLS = [
    dict(key="Bill 080/2026", pdf=f"{DATA}/bill_080_2026_EN.pdf", title="Appropriation Bill 2027 (as presented)", date="2026-10-07",
         source="documents.gov.lk 080/2026", status="presented to Parliament; may change at committee stage"),
    dict(key="GS 069/2026", pdf=f"{DATA}/bill_069_2026_EN.pdf", title="Appropriation Bill 2027 (Gazette text)", date="2026-09-21",
         source="documents.gov.lk 069/2026", status="gazetted before presentation"),
]
SPEECHES = [
    dict(key="Speech 2027", pdf=f"{DATA}/speech_2027_EN.pdf", title="Budget Speech 2027", date="2026-11-12",
         source="treasury.gov.lk/web/budget-speeches/section/2027"),
]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="budget")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--out", default="data/budget/index.sqlite")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-k", type=int, default=5); s.add_argument("--db", default="data/budget/index.sqlite")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        bad = 0
        for bl in BILLS:
            probs = parse.check(parse.parse_bill(bl["pdf"]))
            print(bl["key"], "OK" if not probs else probs)
            bad += bool(probs)
        return 1 if bad else 0
    if a.cmd == "build":
        print(index.build(a.out, BILLS, SPEECHES))
        return 0
    db = index.connect(a.db)
    for r in index.search(db, a.query, a.k):
        print(f"[{r['doc']} {r['ref']} p.{r['page']}] {r['heading']}\n   {r['text'][:200]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
