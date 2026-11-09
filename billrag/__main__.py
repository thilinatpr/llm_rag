"""CLI: python -m billrag {ingest,ingest-acts,link-acts,stats,search,ask,chat,serve,embed,summarize}"""
import argparse
from pathlib import Path
import os
import sys
import time

from .config import load_settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="billrag", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="parse downloaded English PDFs and build the index")
    p.add_argument("--limit", type=int)
    p.add_argument("--since-year", type=int, default=0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--only-new", action="store_true", help="leave bills already in the index alone (keeps their chunk ids and vectors)")
    sub.add_parser("stats")
    p = sub.add_parser("ingest-si", help="link the official Sinhala text (Unicode PDFs, 2025+) to the English sections")
    p.add_argument("--since-year", type=int, default=2025)
    p.add_argument("--keys", help="comma-separated bill keys: only these (the others' PDFs need not be on this machine)")
    p = sub.add_parser("ingest-acts", help="add certified Acts from data/acts and link them to their bills, then run `embed`")
    p.add_argument("--since-year", type=int, default=2024)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--only-new", action="store_true", help="leave Acts already in the index alone")
    p.add_argument("--keys", help="comma-separated N/YYYY: only these Acts, of any year (base Acts; see base-acts)")
    p = sub.add_parser("base-acts", help="check that every Act named as amended by an indexed bill or Act is in the index (exit 1 if one cannot be found)")
    p.add_argument("--listing", help="documents.gov.lk acts.json (default: data/acts/acts.json): which missing Acts can be fetched")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    p = sub.add_parser("add-base-act", help="index one Act from a PDF you found (English text), then run `embed`")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--pdf", help="a PDF with a text layer")
    src.add_argument("--text", help="a reviewed transcription of a scan (pages separated by form feeds; see billrag/ocr.py); "
                                    "answers that use it get the scanned-copy note")
    p.add_argument("--reviewed", help="with --text: date the transcription was reviewed, YYYY-MM-DD")
    p.add_argument("--no", type=int, required=True, help="Act number, e.g. 15")
    p.add_argument("--year", type=int, required=True, help="e.g. 1979")
    p.add_argument("--title", required=True, help='without "Act, No. ..": e.g. "Code of Criminal Procedure"')
    p.add_argument("--date", help="certified date YYYY-MM-DD if the PDF's cover has none")
    sub.add_parser("link-acts", help="recompute bill -> Act links and the section-by-section comparison (no re-parse)")
    sub.add_parser("glossary", help="mine English <-> official Sinhala titles and defined terms from the linked sections")
    p = sub.add_parser("reclassify", help="re-read GS/Bill/Other from the stored titles in place (no re-ingest, vectors stay valid), "
                       "then rebuild meta, titles and Act links")
    p.add_argument("--dry-run", action="store_true", help="only list what would change")
    sub.add_parser("meta", help="(re)build bill metadata: families, newest instance, bill type, section cross-references")
    p = sub.add_parser("titles", help="(re)build the title index (which bill does a question name?); --eval scores the ranker")
    p.add_argument("--eval", action="store_true", help="score the ranker on synthetic title questions instead of building")
    p = sub.add_parser("tag", help="LLM topic tags per bill (newest first, resumable); run `meta` first")
    p.add_argument("--since-year", type=int, default=2024)
    p.add_argument("--limit", type=int, default=0)
    p = sub.add_parser("search", help="retrieval only (no LLM / API key needed)")
    p.add_argument("--type", dest="bill_type", choices=["appropriation", "incorporation", "amendment", "new"])
    p.add_argument("--tag")
    p.add_argument("--latest", action="store_true", help="only the newest instance of a recurring title")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=6)
    p.add_argument("--year", type=int)
    p.add_argument("--bill")
    p = sub.add_parser("ask", help="one-shot question")
    p.add_argument("question")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline",
                   help="pipeline (default): retrieve -> AI evidence grading -> AI summary; agent: free-form tool loop")
    p = sub.add_parser("chat", help="interactive chat")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline")
    p = sub.add_parser("serve", help="simple web chat UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline")
    p = sub.add_parser("eval", help="retrieval recall@k / MRR on an eval set (uses the env-configured retriever)")
    p.add_argument("--file", default="evals/retrieval_eval.jsonl")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("--misses", type=int, default=0, help="print N missed questions with what was retrieved")
    p.add_argument("--since-year", type=int, default=0, help="only score items whose source bill is from this year on")
    p.add_argument("--route", choices=["split", "none"], default="split",
                   help="split (default, mirrors the agent): clause questions search clauses only, bill questions "
                        "search overview chunks only. none: search every chunk kind for every question")
    p.add_argument("--label", default="", help="append the result to evals/results.jsonl under this label")
    p = sub.add_parser("eval-answers", help="score full answers (citations, LLM judge, latency) for pipeline or agent")
    p.add_argument("--file", default="evals/retrieval_eval.jsonl")
    p.add_argument("--mode", choices=["pipeline", "agent"], default="pipeline")
    p.add_argument("--since-year", type=int, default=2025)
    p.add_argument("--limit", type=int, default=24, help="evenly spaced subset size (0 = all)")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--label", default="")
    sub.add_parser("embed", help="compute dense vectors (needs BILLRAG_EMBED_*)")
    p = sub.add_parser("summarize", help="LLM-summarise each bill to improve overview retrieval")
    p.add_argument("--since-year", type=int, default=0)
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)

    s = load_settings()
    from .store import Store, normalize_ref
    if a.cmd == "ingest":
        from .ingest import ingest
        ingest(s.data_dir, s.db_path, a.limit, a.workers, a.since_year, a.only_new)
        if s.vectors_path.exists():
            print(f"NOTE: {s.vectors_path} no longer matches the index. Re-run `python -m billrag embed` "
                  f"(dense retrieval will refuse to start until you do).", file=sys.stderr)
        return 0
    if not s.db_path.exists():
        sys.exit(f"No index at {s.db_path}. Run: python -m billrag ingest")
    store = Store(s.db_path)
    if a.cmd == "ingest-si":
        from .ingest import ingest_si
        ingest_si(s.data_dir, s.db_path, a.since_year, keys=set(a.keys.split(",")) if a.keys else None)
        return 0
    if a.cmd == "ingest-acts":
        from .ingest import ingest_acts
        keys = {k.strip() for k in a.keys.split(",") if k.strip()} if a.keys else None
        out = ingest_acts(s.data_dir.parent / "acts", s.db_path, a.since_year, a.workers, a.only_new, keys)
        print({k: v for k, v in out.items() if k != "skipped"})
        return 0
    if a.cmd == "base-acts":
        from . import baseacts
        res = baseacts.check(store, baseacts.load_listing(a.listing or s.data_dir.parent / "acts" / "acts.json"))
        if a.json:
            import json
            print(json.dumps(res, ensure_ascii=False, indent=1))
        else:
            print(f"{res['required']} Acts are named as amended by indexed bills/Acts: {res['held']} held, {len(res['fetchable'])} can be fetched, "
                  f"{len(res['waived'])} waived, {len(res['unresolved'])} NOT FOUND")
            for label, group in (("fetchable", "fetchable"), ("waived", "waived"), ("NOT FOUND", "unresolved")):
                for k, v in sorted(res[group].items()):
                    print(f"  [{label}] {baseacts.describe(k, v)}")
        return 1 if res["unresolved"] else 0
    if a.cmd == "add-base-act":
        from .ingest import add_base_act
        key, n = add_base_act(s.db_path, Path(a.pdf) if a.pdf else None, a.no, a.year, a.title, a.date,
                              text=Path(a.text) if a.text else None, reviewed=a.reviewed)
        print(f"Indexed {key}: {n} sections. Now run `python -m billrag embed`, then publish.")
        return 0
    if a.cmd == "link-acts":
        from . import acts
        print(acts.build(store))
        return 0
    if a.cmd == "glossary":
        from . import glossary
        print(glossary.build(store))
        return 0
    if a.cmd == "reclassify":
        from .ingest import classify, reclassify
        if a.dry_run:
            rows = store.db.execute("SELECT key, title, kind, title_norm FROM bills WHERE kind != 'Act' ORDER BY date DESC").fetchall()
            changed = [(k, kd, *classify(t or "")) for k, t, kd, tn in rows if classify(t or "") != (kd, tn)]
        else:
            changed = reclassify(store)
        for key, old, new, tn in changed:
            print(f"{key}: {old} -> {new}  ({tn})")
        print(f"{len(changed)} documents {'would change' if a.dry_run else 'changed'}")
        if changed and not a.dry_run:
            from . import acts, meta, titles
            print(meta.build(store))
            print(titles.build(store))
            print(acts.build(store))
        return 0
    if a.cmd == "meta":
        from . import meta
        print(meta.build(store))
        from . import titles
        print(titles.build(store))
        return 0
    if a.cmd == "titles":
        from . import titles
        print(titles.evaluate(store) if a.eval else titles.build(store))
        return 0
    if a.cmd == "tag":
        from . import meta
        from .llm import make_llm
        print({"tagged": meta.tag_bills(store, make_llm(s), a.since_year, effort=s.grade_effort, limit=a.limit)})
        return 0
    if a.cmd == "stats":
        print(store.stats())
        return 0

    if a.cmd == "serve":
        from .web import serve
        serve(s, a.host, a.port, a.mode)
        return 0

    from .llm import make_answer_llm, make_embedder, make_llm, make_reranker
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
        from . import meta
        keys = [normalize_ref(a.bill)] if a.bill else meta.keys_where(store, bill_type=a.bill_type, tag=a.tag, latest_only=a.latest)
        for c in r.search(a.query, a.k, a.year, keys):
            print(fmt_passage(c, 500), "\n")
        return 0

    if a.cmd == "eval-answers":
        import json, pathlib
        from . import answer_eval as ae
        from .agent import Agent
        from .evaluate import load_items
        from .pipeline import Pipeline
        llm = make_llm(s)
        retriever = Retriever(store, embedder, s.vectors_path, llm if s.rerank == "llm" else None, s.rerank, make_reranker(s))
        make = ((lambda: Pipeline(store, retriever, llm, grade_effort=s.grade_effort, answer_effort=s.answer_effort,
                                  answer_llm=make_answer_llm(s)))
                if a.mode == "pipeline" else (lambda: Agent(store, retriever, llm)))
        items = [ae.expand_gold(store, i) for i in ae.select([i for i in load_items(a.file) if i["source"]["year"] >= a.since_year], a.limit)]
        t0 = time.time()
        judge = llm
        if os.environ.get("BILLRAG_JUDGE_MODEL"):  # an independent judge: BILLRAG_JUDGE_{MODEL,BASE_URL,API_KEY,PROVIDER}
            import dataclasses
            e = os.environ.get
            judge = make_llm(dataclasses.replace(
                s, chat_model=e("BILLRAG_JUDGE_MODEL"), chat_provider=e("BILLRAG_JUDGE_PROVIDER", "openai"),
                chat_base_url=e("BILLRAG_JUDGE_BASE_URL") or None, chat_api_key=e("BILLRAG_JUDGE_API_KEY") or None))
        rows = ae.run(items, make, judge, a.workers)
        summ = ae.summarize(rows)
        label = a.label or f"{a.mode} | {a.since_year}+ | n={len(items)}"
        print(ae.format_report(summ, label)); print(f"(wall {time.time() - t0:.0f}s)")
        out = pathlib.Path(a.file).parent
        (out / f"answers_{a.mode}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        with open(out / "answer_results.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"label": label, "mode": a.mode, "summary": summ}) + "\n")
        return 0

    if a.cmd == "eval":
        import json, pathlib
        from .evaluate import evaluate, format_report, load_items
        r = Retriever(store, embedder, s.vectors_path, make_llm(s) if s.rerank == "llm" else None, s.rerank, make_reranker(s))
        from .answer_eval import with_act_gold
        items = [with_act_gold(store, i) for i in load_items(a.file) if i["source"]["year"] >= a.since_year]
        summary, rows, secs = (evaluate(r, items, a.k, ("overview",), ("section", "schedule")) if a.route == "split"
                               else evaluate(r, items, a.k))
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
        print(summarize_all(store, llm, a.since_year, a.limit, a.overwrite, a.workers, effort=s.grade_effort), "bills summarised")
        return 0
    retriever = Retriever(store, embedder, s.vectors_path, llm, s.rerank, make_reranker(s))
    if a.mode == "agent":
        ev = (lambda n, args: print(f"  \u2192 {n} {args}", file=sys.stderr)) if a.verbose else None
        agent = Agent(store, retriever, llm, on_event=ev)
        answer = agent.ask
    else:
        from . import translate
        from .pipeline import Pipeline
        ev = (lambda n, d: print(f"  \u2192 {n}: {d}", file=sys.stderr)) if a.verbose else None
        pipe = Pipeline(store, retriever, llm, on_event=ev, grade_effort=s.grade_effort, answer_effort=s.answer_effort,
                        answer_llm=make_answer_llm(s), translate_mode=s.translate,
                        translator=translate.make_translator(s, llm))

        def answer(q):
            from . import calltrace
            with calltrace.tracing() as trace:
                r = pipe.ask(q)
            cost = calltrace.cost_summary(trace.calls)
            out = r.answer
            if r.warnings:
                out += "\n\n\u26a0 " + "\n\u26a0 ".join(r.warnings)
            if r.evidence:
                out += "\n\nSources:\n" + "\n".join("  " + s for s in r.sources())
            if a.verbose:
                out += f"\n\n({r.llm_calls} LLM calls; " + ", ".join(f"{k} {v:.1f}s" for k, v in r.seconds.items()) + ")"
                if cost:
                    out += (f"\n(tokens in {cost['in']} (cache hit {cost['cache_hit']}), out {cost['out']}; "
                            + (f"cost ${cost['usd']:.5f} for {cost['priced_calls']}/{cost['chat_calls']} calls"
                               f"{' at PEAK rates' if cost['peak'] else ''}" if cost["priced_calls"] else "no price known for this model")
                            + ")")
            return out
    if a.cmd == "ask":
        print(answer(a.question))
        return 0
    print("Ask about the bills (Ctrl-D to quit).")
    while True:
        try:
            q = input("\nyou> ").strip()
        except EOFError:
            return 0
        if q:
            print("\n" + answer(q))


if __name__ == "__main__":
    from .retrieve import VectorsError
    try:
        sys.exit(main())
    except VectorsError as e:
        sys.exit(f"error: {e}")
