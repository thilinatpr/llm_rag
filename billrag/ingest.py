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


def build_chunks(bill: dict, parsed):
    head = f"Bill {bill['key']} — {bill['title']} ({bill['kind']}, {bill['date'][:10]})"
    chunks = []
    outline = []
    for s in parsed.sections:
        ref = f"§{s.no}" if s.no is not None else (s.label or "schedule")
        outline.append(f"{ref} {s.heading or s.text[:60]}")
        ctx_head = " | ".join(x for x in (head, s.part, ref, s.heading) if x)
        for piece in split_text(s.text):
            chunks.append({"kind": s.kind, "part": s.part, "section_no": s.no, "heading": s.heading,
                           "page": s.page, "text": piece, "ctx": f"{ctx_head}\n{piece}"})
    c = parsed.cover
    overview = " ".join(x for x in (
        head + ".", bill["long_title"] or "", f"Presented by {c['presented_by']}." if c.get("presented_by") else "",
        "Outline: " + "; ".join(outline)) if x)[:3000]
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
            store.replace_bill(row, build_chunks(row, parsed))
            if n % 50 == 0:
                store.commit()
                print(f"  {n}/{len(jobs)}", file=sys.stderr)
    store.commit()
    st = store.stats()
    print(f"Indexed {st['bills']} bills / {st['chunks']} chunks; {errors} failed; "
          f"{skipped} bills without an English PDF skipped", file=sys.stderr)
    return st
