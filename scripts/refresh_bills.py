#!/usr/bin/env python3
"""Add newly published bills and Acts to the index, in one command. Works on a copy; the live index changes only with --publish.

  set -a; . ./.local.env; set +a
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY .venv/bin/python scripts/refresh_bills.py             # build + check, publish nothing
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY .venv/bin/python scripts/refresh_bills.py --publish   # ... and publish if every check passes

Steps: copy the live index from R2 -> scrape the listings and recent PDFs -> stop here if nothing is new -> parse only the new documents (existing
chunks keep their ids, so their vectors stay valid) -> link Sinhala text and bill -> Act -> summarize -> embed the new text -> shrink -> check -> publish.
`upload_r2.py --include-index` backs up the live pair and swaps it safely. A running container keeps its index: it restores the new one when it next starts
(it sleeps after 15 idle minutes). Every step can be re-run; summaries cost about 1 model call per new document.

Base Acts: every Act that an indexed bill or Act amends (named in its long title) must be in the index (billrag/baseacts.py). The ones documents.gov.lk
has are fetched here; the others stop the publish and appear in the report (`base_acts_missing`, and the admin panel) until someone adds the text
(`python -m billrag add-base-act`) or records why not in evals/base_acts_waived.json. The first run after this was added needs --force (it fetches ~36 Acts).

Kinds: every run first re-reads GS/Bill from the stored titles (`billrag reclassify`; " (GS)", " - (Bill)", " GS", ... were stored as Other before
2026-11-09). Only `bills` rows change, so it costs no embedding; a run where it changed something counts as having work to do.

Checks before publishing: nothing that was in the index is lost, every new document can be found by its own title, retrieval on the recent eval
questions stays above --min-mrr (measured 0.87-0.88; needs Workers AI). Chat model: the BILLRAG_CHAT_* settings, or deepseek-flash if only DEEPSEEK_API_KEY is set.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from billrag import baseacts  # noqa: E402
from billrag.store import Store, bill_key  # noqa: E402

ACTS_SINCE = 2024  # acts.ACT_SINCE: older Acts are not indexed


class Stop(Exception):
    """A step or a check failed; nothing was published."""


def new_documents(work, known):
    """Listing rows with an English PDF on disk and no entry in the index -> {"bills": {key: title}, "acts": {key: title}}."""
    from billrag.acts import act_key
    from billrag.ingest import english_pdf, english_pdf_named
    bills_dir, acts_dir = work / "bills", work / "acts"
    out = {"bills": {}, "acts": {}}
    for b in json.loads((bills_dir / "bills.json").read_text(encoding="utf-8")):
        key = bill_key(b["billNo"], b["billSubNo"])
        if key not in known and english_pdf(bills_dir, b):
            out["bills"][key] = " ".join((b.get("descriptionEnglish") or "").split())
    path = acts_dir / "acts.json"
    for a in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
        if not a.get("actNo") or int(a.get("actSubNo") or 0) < ACTS_SINCE:
            continue
        key = act_key(a["actNo"], a["actSubNo"])
        if key not in known and english_pdf_named(acts_dir, a["date"], a["actNoText"]):
            out["acts"][key] = " ".join((a.get("descriptionEnglish") or "").split())
    return out


def listing_keys(work, since_year):
    """Keys of every listed bill (from since_year) and Act the index could hold, whether or not a PDF was found: what the admin panel's daily check sees."""
    from billrag.acts import act_key
    out = set()
    path = work / "bills" / "bills.json"
    for b in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
        if int((b.get("date") or "0")[:4] or 0) >= since_year:
            out.add(bill_key(b["billNo"], b["billSubNo"]))
    path = work / "acts" / "acts.json"
    for a in json.loads(path.read_text(encoding="utf-8")) if path.exists() else []:
        if a.get("actNo") and int(a.get("actSubNo") or 0) >= max(ACTS_SINCE, since_year):
            out.add(act_key(a["actNo"], a["actSubNo"]))
    return out


def base_acts(run, work, bills_dir):
    """Fetch and index the base Acts (the law an indexed bill or Act amends) that documents.gov.lk has and the index lacks. Returns their ids.
    What it cannot fetch is reported by the check after the build (baseacts.check) and stops the publish until someone finds the text or waives it."""
    res = base_check(work, bills_dir)
    ids = sorted(res["fetchable"])
    if ids:
        print(f"\nBase Acts to fetch: {ids}", file=sys.stderr)
        keys = ",".join(ids)
        run("scripts/scrape_bills.py", "--source", "acts", "--out", str(work / "acts"), "--keys", keys, "--langs", "ENGLISH", step="fetch base Acts")
        run("-m", "billrag", "ingest-acts", "--keys", keys, "--only-new", step="parse base Acts")
    return ids


def reclassify_kinds(bills_dir):
    """`python -m billrag reclassify` in effect: kind + title_norm re-read from the stored titles, then meta/titles rebuilt. Returns the changed keys.
    Changes only `bills` rows (no chunk text, so no embedding); idempotent, so after the first run it returns []."""
    from billrag import meta, titles
    from billrag.ingest import reclassify
    store = Store(bills_dir / "index.sqlite")
    changed = reclassify(store)
    if changed:
        print(f"\nReclassified {len(changed)} documents (version suffix in the title), e.g. {[c[0] for c in changed[:5]]}", file=sys.stderr)
        meta.build(store)
        titles.build(store)
    return [c[0] for c in changed]


def base_check(work, bills_dir):
    """Module level on purpose: run_refresh imports its own `Store` later, which would make the name local to it."""
    return baseacts.check(Store(bills_dir / "index.sqlite"), baseacts.load_listing(work / "acts" / "acts.json"))


def unresolved_base_acts(work, bills_dir):
    """baseacts.check after the fetch: a base Act the listing offers but the index still lacks failed to download or parse, so it counts as missing."""
    res = base_check(work, bills_dir)
    res["unresolved"].update({k: {**v, "why": "documents.gov.lk lists it, but fetching or reading its PDF failed"} for k, v in res["fetchable"].items()})
    res["fetchable"] = {}
    return res


def snapshot(db_path):
    db = sqlite3.connect(str(db_path))
    try:
        return {"keys": {r[0] for r in db.execute("SELECT key FROM bills")},
                "chunks": {r[0] for r in db.execute("SELECT id FROM chunks")},
                "summaries": {r[0] for r in db.execute("SELECT key FROM bills WHERE summary IS NOT NULL AND summary != ''")}}
    finally:
        db.close()


def check_nothing_lost(before, after):
    """Problems (empty = fine). Bills, chunk ids and summaries that were in the index must still be there."""
    problems = []
    for name, what in (("keys", "documents"), ("chunks", "chunks"), ("summaries", "summaries")):
        gone = before[name] - after[name]
        if gone:
            problems.append(f"{len(gone)} {what} disappeared, e.g. {sorted(gone, key=str)[:3]}")
    return problems


def check_findable(search, wanted):
    """search(title, key) -> bool for each new document {key: title}. Problems (empty = fine)."""
    return [f"{key} is not found by its own title: {title[:70]}" for key, title in wanted.items() if not search(title, key)]


def chat_env(env):
    """The model for summaries: what is configured, else DeepSeek as in production (AGENTS.md)."""
    if env.get("BILLRAG_CHAT_MODEL") or not env.get("DEEPSEEK_API_KEY"):
        return {}
    return {"BILLRAG_CHAT_PROVIDER": "openai", "BILLRAG_CHAT_MODEL": "deepseek-flash", "BILLRAG_CHAT_BASE_URL": "https://api.deepseek.com",
            "BILLRAG_CHAT_API_KEY": env["DEEPSEEK_API_KEY"], "BILLRAG_GRADE_EFFORT": "off"}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", default="data/refresh", help="working copy (git-ignored; the shared data/bills is not touched)")
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET") or "gov-rag")
    ap.add_argument("--since-year", type=int, default=datetime.now(timezone.utc).year - 1, help="scrape PDFs from this year on (default: last year)")
    ap.add_argument("--publish", action="store_true", help="publish to R2 if every check passes")
    ap.add_argument("--min-mrr", type=float, default=0.80)
    ap.add_argument("--force", action="store_true", help="go on even if nothing is new (to retry missing summaries)")
    ap.add_argument("--action-id", help="documents.gov.lk Next-Action id, if the site redeployed (see AGENTS.md)")
    ap.add_argument("--report", help="write the outcome here as JSON (state nothing_new | built | published | failed, new documents, MRR, problems): "
                    "the GitHub workflow sends it to the admin panel (scripts/refresh_r2.py)")
    a = ap.parse_args(argv)
    report = {"state": "failed", "min_mrr": a.min_mrr}
    try:
        return run_refresh(a, report)
    except Exception as e:  # a crash, not a failed check: say so in the report, then let it show
        report["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if a.report:
            Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def run_refresh(a, report):

    work, bills_dir = Path(a.work), Path(a.work) / "bills"
    bills_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "BILLRAG_DATA": str(bills_dir), "OPENBLAS_NUM_THREADS": "1", **chat_env(os.environ)}
    py = sys.executable

    def run(*cmd, step=None):
        print(f"\n== {step or ' '.join(cmd[:3])}", file=sys.stderr, flush=True)
        if subprocess.run([py, *cmd], cwd=ROOT, env=env, check=False).returncode:
            raise Stop(f"step failed: {step or ' '.join(cmd)}")

    try:
        run("scripts/fetch_index.py", "--bucket", a.bucket, "--data", str(bills_dir), "--force", step="copy the live index from R2")
        before = snapshot(bills_dir / "index.sqlite")
        reclassified = reclassify_kinds(bills_dir)  # GS/Bill read from every spelling of the title suffix; bills rows only, vectors stay valid
        report.update(reclassified=reclassified)
        extra = ["--action-id", a.action_id] if a.action_id else []
        run("scripts/scrape_bills.py", "--out", str(bills_dir), "--since-year", str(a.since_year), *extra, step="scrape bills")
        run("scripts/scrape_bills.py", "--source", "acts", "--out", str(work / "acts"), "--since-year", str(max(a.since_year, ACTS_SINCE)),
            "--langs", "ENGLISH", "SINHALA", *extra, step="scrape Acts")
        new = new_documents(work, before["keys"])
        report.update(new_bills=new["bills"], new_acts=new["acts"])
        print(f"\nNew: {len(new['bills'])} bills {sorted(new['bills'])}, {len(new['acts'])} Acts {sorted(new['acts'])}", file=sys.stderr)
        if not new["bills"] and not new["acts"] and not reclassified and not a.force:
            print("Nothing new. The live index is unchanged.", file=sys.stderr)
            report.update(base_acts_missing={k: {"name": v["name"], "cited_by": v["cited_by"], "why": v["why"]} for k, v in base_check(work, bills_dir)["unresolved"].items()})  # only reported: nothing is published
            report.update(state="nothing_new", unindexed=sorted(listing_keys(work, a.since_year) - before["keys"]))
            return 0
        run("-m", "billrag", "ingest", "--only-new", "--since-year", str(a.since_year), step="parse new bills")
        if new["bills"]:
            run("-m", "billrag", "ingest-si", "--since-year", str(a.since_year), "--keys", ",".join(new["bills"]), step="link official Sinhala text")
        run("-m", "billrag", "ingest-acts", "--only-new", "--since-year", str(max(a.since_year, ACTS_SINCE)), step="parse new Acts, link bills to Acts")
        base_added = base_acts(run, work, bills_dir)  # every Act that an indexed bill or Act amends must be in the index too
        if new["acts"] or new["bills"] or reclassified:
            run("-m", "billrag", "link-acts", step="recompute bill -> Act links")
        run("-m", "billrag", "summarize", "--since-year", str(a.since_year), step="summarise what has no summary")
        run("-m", "billrag", "embed", step="embed the new text")
        run("scripts/shrink_vectors.py", "--data", str(bills_dir), step="shrink vectors")

        after = snapshot(bills_dir / "index.sqlite")
        problems = check_nothing_lost(before, after)
        unparsed = sorted((set(new["bills"]) | set(new["acts"])) - after["keys"])
        base = unresolved_base_acts(work, bills_dir)  # after the fetch: what is still missing
        if base["unresolved"]:
            problems.append(baseacts.problem(base))
        report.update(base_acts_missing={k: {"name": v["name"], "cited_by": v["cited_by"], "why": v["why"]} for k, v in base["unresolved"].items()},
                      base_acts_added=sorted(set(base_added) - set(base["unresolved"])))
        report.update(unparsed=unparsed, unindexed=sorted(listing_keys(work, a.since_year) - after["keys"]),
                      chunks_added=len(after["chunks"]) - len(before["chunks"]), problems=list(problems))
        if unparsed:  # a PDF the parser cannot read: report it, but it must not hold back the others
            print(f"\nNOT indexed (parser found no sections): {unparsed}", file=sys.stderr)
        added = {**new["bills"], **new["acts"]}
        added = {k: t for k, t in added.items() if k in after["keys"]}
        no_summary = sorted(set(added) - after["summaries"])
        report.update(new_bills={k: t for k, t in new["bills"].items() if k in after["keys"]},
                      new_acts={k: t for k, t in new["acts"].items() if k in after["keys"]}, no_summary=no_summary)
        if no_summary:
            print(f"\nNo summary yet (searchable, but less well): {no_summary}", file=sys.stderr)

        from billrag.config import load_settings
        from billrag.llm import make_embedder, make_reranker
        from billrag.retrieve import Retriever
        from billrag.store import Store
        os.environ.update({"BILLRAG_DATA": str(bills_dir)})
        s = load_settings()
        retriever = Retriever(Store(s.db_path), make_embedder(s), s.vectors_path, None, s.rerank, make_reranker(s))
        problems += check_findable(lambda title, key: any(c["bill_key"] == key for c in retriever.search(title, 15, kinds=("overview",))), added)
        from billrag.answer_eval import with_act_gold
        from billrag.evaluate import evaluate, load_items
        items = [with_act_gold(retriever.store, i) for i in load_items(ROOT / "evals/retrieval_eval.jsonl") if i["source"]["year"] >= 2025]
        summary = evaluate(retriever, items, 10, ("overview",), ("section", "schedule"))[0]
        mrr = summary["all"]["mrr"] if "all" in summary else summary.get("mrr")
        report.update(mrr=round(mrr, 3), eval_items=len(items))
        print(f"\nRetrieval on {len(items)} recent questions: MRR {mrr:.3f} (floor {a.min_mrr})", file=sys.stderr)
        if mrr < a.min_mrr:
            problems.append(f"retrieval MRR {mrr:.3f} is below {a.min_mrr}")
        report["problems"] = list(problems)
        if problems:
            raise Stop("checks failed:\n  " + "\n  ".join(problems))
        print(f"\nChecks passed: {len(added)} new documents, {len(after['chunks']) - len(before['chunks'])} new chunks.", file=sys.stderr)
        if not a.publish:
            print(f"Not published (no --publish). The built index is in {bills_dir}.", file=sys.stderr)
            report["state"] = "built"
            return 0
        run("scripts/upload_r2.py", "--bucket", a.bucket, "--data", str(bills_dir), "--include-index", step="publish to R2")
        print("Published. A running container picks it up when it next starts.", file=sys.stderr)
        report["state"] = "published"
        return 0
    except Stop as e:
        print(f"\nSTOPPED, nothing was published: {e}", file=sys.stderr)
        report.update(state="failed", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
