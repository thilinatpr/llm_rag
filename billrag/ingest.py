"""Build the index from data/bills/bills.json + the downloaded English PDFs."""
import json
import re
import sys
from multiprocessing import Pool
from pathlib import Path

from .parse import parse_pdf, split_text, norm
from .store import Store, bill_key


def _safe(s):  # must match scripts/scrape_bills.py
    return re.sub(r"[^\w.-]+", "_", s)


def english_pdf(data_dir: Path, b: dict):
    year = (b.get("date") or "")[:4] or "unknown"
    p = data_dir / "pdfs" / year / f"{_safe(b['billNoText'])}_ENGLISH.pdf"
    return p if p.exists() and p.stat().st_size > 0 else None


def classify(title: str):
    m = re.search(r"\s*-\s*(bill|gs)\s*$", title.strip(), re.I)
    kind = {"bill": "Bill", "gs": "GS"}.get(m.group(1).lower()) if m else "Other"
    base = title[:m.start()] if m else title
    return kind, norm(base)


def _work(args):
    path, title = args
    try:
        return parse_pdf(str(path), title), None
    except Exception as e:  # corrupt/odd PDF: record and continue
        return None, f"{type(e).__name__}: {e}"


def head_line(bill: dict) -> str:
    return f"Bill {bill['key']} \u2014 {bill['title']} ({bill['kind']}, {bill['date'][:10]})"


def overview_text(bill: dict, outline: list, summary=None) -> str:
    """Bill-level chunk: title, optional LLM summary + keywords, long title, presenter, section outline."""
    parts = [head_line(bill) + "."]
    if summary:
        parts.append("Summary (auto-generated): " + summary)
    if bill.get("long_title"):
        parts.append(bill["long_title"])
    if bill.get("presented_by"):
        parts.append(f"Presented by {bill['presented_by']}.")
    parts.append("Outline: " + "; ".join(outline))
    return " ".join(parts)[:3600]


def build_chunks(bill: dict, parsed):
    head = head_line(bill)
    chunks = []
    outline = []
    for s in parsed.sections:
        ref = f"\u00a7{s.no}" if s.no is not None else (s.label or "schedule")
        outline.append(f"{ref} {s.heading or s.text[:60]}")
        ctx_head = " | ".join(x for x in (head, s.part, ref, s.heading) if x)
        for piece in split_text(s.text):
            chunks.append({"kind": s.kind, "part": s.part, "section_no": s.no, "heading": s.heading,
                           "page": s.page, "text": piece, "ctx": f"{ctx_head}\n{piece}"})
    overview = overview_text({**bill, "presented_by": parsed.cover.get("presented_by")}, outline, bill.get("summary"))
    chunks.insert(0, {"kind": "overview", "page": 1, "text": overview, "ctx": overview})
    return chunks


def ingest(data_dir: Path, db_path: Path, limit=None, workers=4, since_year=0):
    meta = json.loads((data_dir / "bills.json").read_text(encoding="utf-8"))
    jobs = []
    for b in sorted(meta, key=lambda x: x.get("date") or "", reverse=True):
        if since_year and int((b.get("date") or "0")[:4]) < since_year:
            continue
        p = english_pdf(data_dir, b)
        if p:
            jobs.append((b, p))
    if limit:
        jobs = jobs[:limit]
    store = Store(db_path)
    skipped = len(meta) - len(jobs)
    errors = 0
    with Pool(workers) as pool:
        for n, ((b, p), (parsed, err)) in enumerate(
                zip(jobs, pool.imap(_work, [(p, b["descriptionEnglish"] or "") for b, p in jobs], chunksize=2)), 1):
            if err or not parsed or not parsed.sections:
                errors += 1
                print(f"  ! {b['billNoText']}: {err or 'no sections found'}", file=sys.stderr)
                continue
            b["descriptionEnglish"] = re.sub(r"\s+", " ", b["descriptionEnglish"] or "").strip()
            kind, tnorm = classify(b["descriptionEnglish"])
            year = int(b["date"][:4])
            c = parsed.cover
            row = {"key": bill_key(b["billNo"], b["billSubNo"]), "bill_no": b["billNo"], "year": b["billSubNo"],
                   "date": b["date"], "title": b["descriptionEnglish"], "title_norm": tnorm, "kind": kind,
                   "long_title": c.get("long_title"), "presented_by": c.get("presented_by"),
                   "presented_on": c.get("presented_on"), "gazette_date": c.get("gazette_date"),
                   "printed_no": c.get("printed_no"), "n_pages": parsed.n_pages,
                   "n_sections": sum(1 for s in parsed.sections if s.kind == "section"), "pdf_path": str(p),
                   "summary": None}
            prev = store.bill(row["key"])
            row["summary"] = prev["summary"] if prev else None  # LLM summaries survive a re-ingest
            store.replace_bill(row, build_chunks(row, parsed))
            if n % 50 == 0:
                store.commit()
                print(f"  {n}/{len(jobs)}", file=sys.stderr)
    store.commit()
    from . import meta  # families / newest instance / section cross-references (outside `chunks`)
    print(f"  meta: {meta.build(store)}", file=sys.stderr)
    st = store.stats()
    print(f"Indexed {st['bills']} bills / {st['chunks']} chunks; {errors} failed; "
          f"{skipped} bills without an English PDF skipped", file=sys.stderr)
    return st


def ingest_si(data_dir: Path, db_path: Path, since_year=2025, lo=0.4, hi=2.5):
    """Link the official Sinhala text of each bill (date >= since_year) to its English sections (store.local_sections).

    Only Unicode Sinhala PDFs that line up with the English bill section-for-section are linked (see parse_si); within a
    linked bill, a section whose length is outside lo..hi x the English one (boundary slip, tables) is left out."""
    from . import parse_si
    store = Store(db_path)
    keys = [r[0] for r in store.db.execute("SELECT key FROM bills WHERE substr(date,1,4) >= ? ORDER BY date DESC",
                                           (str(since_year),))]
    for n, key in enumerate(keys, 1):
        hits = sorted((data_dir / "pdfs").glob(f"*/{key.replace('/', '_')}_SINHALA.pdf"))
        en = store.section_map(key)
        if not hits or not en:
            store.replace_local(key, "si", "no Sinhala PDF" if not hits else "no English sections", [])
            continue
        try:
            secs, why = parse_si.parse_pdf(str(hits[0]), max(en))
        except Exception as e:  # odd PDF: record and continue
            secs, why = [], f"error: {type(e).__name__}: {e}"[:200]
        keep = [s for s in secs if s.no in en and (len(en[s.no]) < 150 or lo <= len(s.text) / len(en[s.no]) <= hi)]
        store.replace_local(key, "si", why or "ok", [(s.no, s.heading, s.page, s.text) for s in keep],
                            str(hits[0]), max(en))
        if n % 25 == 0:
            store.commit()
            print(f"  {n}/{len(keys)}", file=sys.stderr)
    store.commit()
    from . import glossary  # the glossary is mined from the linked sections: rebuild it with them
    print(f"  glossary: {glossary.build(store)}", file=sys.stderr)
    for r in store.local_stats("si"):
        print(f"  {r['n']:4d} bills  {r['status']}" + (f"  ({r['kept']} sections linked)" if r["kept"] else ""), file=sys.stderr)
