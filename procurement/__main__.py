"""python -m procurement build | search "query" | ask "question" | outline PG2024

Model settings come from the same BILLRAG_* env vars as billrag (see billrag/config.py); the reranker turns on when
Cloudflare credentials are set (BILLRAG_RERANK=none turns it off).
"""
import argparse
import json
import sys

from . import index as ix


def main(argv=None):
    ap = argparse.ArgumentParser(prog="procurement")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--docs", nargs="*")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("-k", type=int, default=8)
    s.add_argument("--all", action="store_true", help="include the 2006 documents")
    a = sub.add_parser("ask"); a.add_argument("question", nargs="+"); a.add_argument("-k", type=int, default=8)
    a.add_argument("--all", action="store_true"); a.add_argument("--json", action="store_true")
    o = sub.add_parser("outline"); o.add_argument("doc")
    c = sub.add_parser("check-tables", help="compare the numbers in each table transcription with the PDF text layer")
    c.add_argument("doc", nargs="?", default="PM2024")
    args = ap.parse_args(argv)

    if args.cmd == "build":
        ix.build(docs=args.docs)
        return
    if args.cmd == "check-tables":
        from . import sources, tables
        bad = tables.check(args.doc, sources.BY_ID[args.doc]["file"])
        for p, extra, missing in bad:
            print(f"p{p}: only in transcription {extra} | only in PDF text {missing}")
        print(f"{len(tables.load(args.doc))} transcribed pages, {len(bad)} with differences")
        return 1 if bad else 0
    from billrag.config import load_settings
    from billrag.llm import make_reranker
    from .ask import ALL, CURRENT
    s = load_settings()
    index = ix.Index(reranker=make_reranker(s))
    if args.cmd == "outline":
        for r in index.db.execute("SELECT ref, heading, MIN(page) p, COUNT(*) n FROM chunks WHERE doc=? GROUP BY ref, heading "
                                  "ORDER BY MIN(ord)", (args.doc,)):
            print(f"{r['ref']:>11}  p{r['p']:<4} {r['heading'][:90]}" + (f"  ({r['n']} parts)" if r["n"] > 1 else ""))
    elif args.cmd == "search":
        for c in index.search(args.query, k=args.k, docs=ALL if args.all else CURRENT):
            print(f"[{c['doc']} §{c['ref']}] {c['heading'][:70]}  (p. {c['page']}, part {c['part']})")
            print("   " + c["text"][:220].replace("\n", " ") + "\n")
    elif args.cmd == "ask":
        from billrag.llm import make_llm
        from .ask import ask
        r = ask(index, make_llm(s), " ".join(args.question), k=args.k, docs=ALL if args.all else None)
        if args.json:
            print(json.dumps(dict(question=r.question, answer=r.answer, warnings=r.warnings, seconds=r.seconds,
                                  sources=r.sources(), docs=r.docs), ensure_ascii=False))
        else:
            print(r.answer + "\n")
            for w in r.warnings:
                print("WARNING:", w)
            print("Sources:\n  " + "\n  ".join(r.sources()) + f"\n({r.seconds} s)")


if __name__ == "__main__":
    sys.exit(main())
