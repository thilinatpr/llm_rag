"""CLI: python -m billrag {ingest,stats,search,ask,chat,embed,summarize}"""
import argparse
import sys

from .config import load_settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="billrag", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="parse downloaded English PDFs and build the index")
    p.add_argument("--limit", type=int)
    p.add_argument("--since-year", type=int, default=0)
    p.add_argument("--workers", type=int, default=4)
    sub.add_parser("stats")
    p = sub.add_parser("search", help="retrieval only (no LLM / API key needed)")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=6)
    p.add_argument("--year", type=int)
    p.add_argument("--bill")
    p = sub.add_parser("ask", help="one-shot question")
    p.add_argument("question")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("chat", help="interactive chat")
    p.add_argument("-v", "--verbose", action="store_true")
    p = sub.add_parser("eval", help="retrieval recall@k / MRR on an eval set (uses the env-configured retriever)")
    p.add_argument("--file", default="evals/retrieval_eval.jsonl")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--misses", type=int, default=0, help="print N missed questions with what was retrieved")
    p.add_argument("--label", default="", help="append the result to evals/results.jsonl under this label")
    sub.add_parser("embed", help="compute dense vectors (needs BILLRAG_EMBED_*)")
    p = sub.add_parser("summarize", help="LLM-summarise each bill to improve overview retrieval")
    p.add_argument("--since-year", type=int, default=0)
    p.add_argument("--limit", type=int)
    a = ap.parse_args(argv)

    s = load_settings()
    from .store import Store, normalize_ref
    if a.cmd == "ingest":
        from .ingest import ingest
        ingest(s.data_dir, s.db_path, a.limit, a.workers, a.since_year)
        if s.vectors_path.exists():
            print(f"NOTE: {s.vectors_path} no longer matches the index. Re-run `python -m billrag embed` "
                  f"(dense retrieval will refuse to start until you do).", file=sys.stderr)
        return 0
    if not s.db_path.exists():
        sys.exit(f"No index at {s.db_path}. Run: python -m billrag ingest")
    store = Store(s.db_path)
    if a.cmd == "stats":
        print(store.stats())
        return 0

    from .llm import make_embedder, make_llm, make_reranker
    from .retrieve import Retriever
    from .agent import Agent, fmt_passage
    embedder = make_embedder(s)
    if a.cmd == "embed":
        from .enrich import embed_all
        if embedder is None:
            sys.exit("Set BILLRAG_EMBED_PROVIDER and BILLRAG_EMBED_MODEL first.")
        embed_all(store, embedder, s.vectors_path)
        return 0
    if a.cmd == "search":
        r = Retriever(store, embedder, s.vectors_path, None, s.rerank, make_reranker(s))
        keys = [normalize_ref(a.bill)] if a.bill else None
        for c in r.search(a.query, a.k, a.year, keys):
            print(fmt_passage(c, 500), "\n")
        return 0

    if a.cmd == "eval":
        import json, pathlib
        from .evaluate import evaluate, format_report, load_items
        r = Retriever(store, embedder, s.vectors_path, make_llm(s) if s.rerank == "llm" else None, s.rerank, make_reranker(s))
        items = load_items(a.file)
        summary, rows, secs = evaluate(r, items, a.k)
        print(format_report(summary, a.label or f"rerank={s.rerank} dense={'on' if r.mat is not None else 'off'}"))
        print(f"({len(items)} questions, {secs:.1f}s)")
        for row in [x for x in rows if not x["rank"]][: a.misses]:
            it = row["item"]
            print(f"\nMISS [{it['type']}] {it['question']}\n   gold: {[(g['bill'], g.get('section')) for g in it['gold'][:3]]}\n   got:  {row['top']}")
        if a.label:
            with open(pathlib.Path(a.file).parent / "results.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"label": a.label, "file": a.file, "k": a.k, "summary": summary}) + "\n")
        return 0

    llm = make_llm(s)
    if a.cmd == "summarize":
        from .enrich import summarize_all
        print(summarize_all(store, llm, a.since_year, a.limit), "bills summarised")
        return 0
    retriever = Retriever(store, embedder, s.vectors_path, llm, s.rerank, make_reranker(s))
    ev = (lambda n, args: print(f"  → {n} {args}", file=sys.stderr)) if a.verbose else None
    agent = Agent(store, retriever, llm, on_event=ev)
    if a.cmd == "ask":
        print(agent.ask(a.question))
        return 0
    print("Ask about the bills (Ctrl-D to quit).")
    while True:
        try:
            q = input("\nyou> ").strip()
        except EOFError:
            return 0
        if q:
            print("\n" + agent.ask(q))


if __name__ == "__main__":
    from .retrieve import VectorsError
    try:
        sys.exit(main())
    except VectorsError as e:
        sys.exit(f"error: {e}")
