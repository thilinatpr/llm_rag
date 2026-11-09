"""Build the index from data/bills/bills.json + the downloaded English PDFs."""
import json
import re
import sys
from multiprocessing import Pool
from pathlib import Path

from .parse import parse_act_pdf, parse_pdf, split_text, norm, split_kind
from .store import Store, bill_key


def _safe(s):  # must match scripts/scrape_bills.py
    return re.sub(r"[^\w.-]+", "_", s)


def english_pdf(data_dir: Path, b: dict):
    year = (b.get("date") or "")[:4] or "unknown"
    p = data_dir / "pdfs" / year / f"{_safe(b['billNoText'])}_ENGLISH.pdf"
    return p if p.exists() and p.stat().st_size > 0 else None


def english_pdf_named(root: Path, date, no_text, lang="ENGLISH"):
    p = root / "pdfs" / (date or "")[:4] / f"{_safe(no_text)}_{lang}.pdf"
    return p if p.exists() and p.stat().st_size > 0 else None


def classify(title: str):
    kind, base = split_kind(title)
    return kind or "Other", norm(base)


def reclassify(store):
    """Re-read kind and title_norm from each stored title with the current `classify` rule, in place. Only the `bills` row changes:
    chunk ids and text (so `fingerprint()` and vectors.npz) stay valid, no re-ingest or `embed`. Acts keep their own kind.
    Run `meta`, `titles` and `link-acts` afterwards (the CLI command does). Returns [(key, old kind, new kind, new title_norm)]."""
    changed = []
    for key, title, kind, tnorm in store.db.execute("SELECT key, title, kind, title_norm FROM bills WHERE kind != 'Act'").fetchall():
        nk, ntn = classify(title or "")
        if (nk, ntn) != (kind, tnorm):
            changed.append((key, kind, nk, ntn))
    store.db.executemany("UPDATE bills SET kind=?, title_norm=? WHERE key=?", [(k, tn, key) for key, _o, k, tn in changed])
    store.commit()
    return changed


def _work(args):
    path, title = args
    try:
        return parse_pdf(str(path), title), None
    except Exception as e:  # corrupt/odd PDF: record and continue
        return None, f"{type(e).__name__}: {e}"


def head_line(bill: dict) -> str:
    prefix = "" if bill["kind"] == "Act" else "Bill "  # an Act's key already says "Act"
    return f"{prefix}{bill['key']} \u2014 {bill['title']} ({bill['kind']}, {bill['date'][:10]})"


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


def ingest(data_dir: Path, db_path: Path, limit=None, workers=4, since_year=0, only_new=False):
    """only_new: leave bills already in the index alone, so their chunk ids and vectors stay valid and only new text needs embedding."""
    meta = json.loads((data_dir / "bills.json").read_text(encoding="utf-8"))
    known = {r[0] for r in Store(db_path).db.execute("SELECT key FROM bills")} if only_new else set()
    jobs = []
    for b in sorted(meta, key=lambda x: x.get("date") or "", reverse=True):
        if since_year and int((b.get("date") or "0")[:4]) < since_year:
            continue
        if bill_key(b["billNo"], b["billSubNo"]) in known:
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
    from . import titles as _titles
    print(f"  titles: {_titles.build(store)}", file=sys.stderr)
    st = store.stats()
    print(f"Indexed {st['bills']} bills / {st['chunks']} chunks; {errors} failed; "
          f"{skipped} bills without an English PDF skipped", file=sys.stderr)
    return st


def link_si(store, key, pdf, lo=0.4, hi=2.5):
    """Link one document's official Sinhala PDF to its English sections (fail-safe rules: see ingest_si, parse_si)."""
    from . import parse_si
    en = store.section_map(key)
    if not pdf or not en:
        store.replace_local(key, "si", "no Sinhala PDF" if not pdf else "no English sections", [])
        return
    try:
        secs, why = parse_si.parse_pdf(str(pdf), max(en))
    except Exception as e:  # odd PDF: record and continue
        secs, why = [], f"error: {type(e).__name__}: {e}"[:200]
    keep = [s for s in secs if s.no in en and (len(en[s.no]) < 150 or lo <= len(s.text) / len(en[s.no]) <= hi)]
    store.replace_local(key, "si", why or "ok", [(s.no, s.heading, s.page, s.text) for s in keep], str(pdf), max(en))


def ingest_si(data_dir: Path, db_path: Path, since_year=2025, lo=0.4, hi=2.5, keys=None):
    """Link the official Sinhala text of each bill (date >= since_year) to its English sections (store.local_sections).

    Only Unicode Sinhala PDFs that line up with the English bill section-for-section are linked (see parse_si); within a
    linked bill, a section whose length is outside lo..hi x the English one (boundary slip, tables) is left out.
    keys: only these bills. Needed when the other bills' PDFs are not on this machine: a missing PDF would unlink them."""
    store = Store(db_path)
    wanted, keys = keys, None
    keys = [r[0] for r in store.db.execute("SELECT key FROM bills WHERE substr(date,1,4) >= ? AND kind!='Act' ORDER BY date DESC",
                                           (str(since_year),)) if wanted is None or r[0] in wanted]
    for n, key in enumerate(keys, 1):
        hits = sorted((data_dir / "pdfs").glob(f"*/{key.replace('/', '_')}_SINHALA.pdf"))
        link_si(store, key, hits[0] if hits else None, lo, hi)
        if n % 25 == 0:
            store.commit()
            print(f"  {n}/{len(keys)}", file=sys.stderr)
    store.commit()
    from . import glossary  # the glossary is mined from the linked sections: rebuild it with them
    print(f"  glossary: {glossary.build(store)}", file=sys.stderr)
    for r in store.local_stats("si"):
        print(f"  {r['n']:4d} bills  {r['status']}" + (f"  ({r['kept']} sections linked)" if r["kept"] else ""), file=sys.stderr)


def _work_act(args):
    path, title, no, year = args
    try:
        return parse_act_pdf(str(path), title, no, year), None
    except Exception as e:  # corrupt/odd PDF, or a file that is not this Act: record and continue
        return None, f"{type(e).__name__}: {e}"


def _act_row(parsed, p, title, no, year, listed_date, prev):
    from . import acts
    c = parsed.cover
    return {"key": acts.act_key(no, year), "bill_no": no, "year": year, "date": (c.get("certified") or listed_date[:10]) + "T00:00:00.000Z",
            "title": f"{re.sub(r'\s+Act$', '', title.rstrip(' ,'))} Act, No. {no} of {year}", "title_norm": norm(re.sub(r"\s+Act$", "", title)), "kind": "Act",
            "long_title": c.get("long_title"), "presented_by": None, "presented_on": None,
            "gazette_date": c.get("gazette_date"), "printed_no": c.get("printed_no"), "n_pages": parsed.n_pages,
            "n_sections": sum(1 for s in parsed.sections if s.kind == "section"), "pdf_path": str(p),
            "summary": prev["summary"] if prev else None}


def add_base_act(db_path: Path, pdf: Path, no, year, title, date=None):
    """Index one Act from a PDF someone found (a base Act that documents.gov.lk does not have): ingest_acts for one file.
    `date` = certified date YYYY-MM-DD, used when the cover has none. Run `embed` afterwards."""
    from . import acts, meta
    from . import titles as _titles
    parsed = parse_act_pdf(str(pdf), title, no, year)
    if not parsed.sections:
        raise ValueError(f"no sections found in {pdf}: is it the English text of Act {no}/{year}?")
    store = Store(db_path)
    row = _act_row(parsed, pdf, title, no, year, date or f"{year}-01-01", store.bill(acts.act_key(no, year)))
    store.replace_bill(row, build_chunks(row, parsed))
    store.commit()
    meta.build(store)
    _titles.build(store)
    acts.build(store)
    return row["key"], row["n_sections"]


def ingest_acts(acts_dir: Path, db_path: Path, since_year=2024, workers=4, only_new=False, keys=None):
    """Add certified Acts (date >= since_year) from data/acts (scripts/scrape_bills.py --source acts) to the index, then link them to
    their bills and link their official Sinhala text. Existing chunks keep their ids, so `embed` only has to embed the new text.
    keys = {"24/2017", ...}: only these Acts, of any year (base Acts, see baseacts.py); since_year is then ignored."""
    from . import acts, meta
    listing = json.loads((acts_dir / "acts.json").read_text(encoding="utf-8"))
    jobs, skipped = [], []
    known = {r[0] for r in Store(db_path).db.execute("SELECT key FROM bills WHERE kind='Act'")} if only_new else set()
    for a in sorted(listing, key=lambda x: x.get("date") or "", reverse=True):
        if keys is not None:
            if f"{a.get('actNo')}/{a.get('actSubNo')}" not in keys:
                continue
        elif int(a.get("actSubNo") or 0) < since_year:
            continue
        if a.get("actNo") and acts.act_key(a["actNo"], a["actSubNo"]) in known:
            continue
        if not a.get("actNo"):  # six listing rows are numbered 0
            skipped.append((a["actNoText"], "no Act number"))
            continue
        p = english_pdf_named(acts_dir, a["date"], a["actNoText"])
        if not p:
            skipped.append((a["actNoText"], "no English PDF"))
            continue
        title = re.sub(r"\s+", " ", a["descriptionEnglish"] or "").strip()
        jobs.append((a, p, title))
    store = Store(db_path)
    added = 0
    with Pool(workers) as pool:
        work = [(p, t, a["actNo"], a["actSubNo"]) for a, p, t in jobs]
        for (a, p, title), (parsed, err) in zip(jobs, pool.imap(_work_act, work, chunksize=2)):
            if err or not parsed or not parsed.sections:
                skipped.append((a["actNoText"], err or "no sections found"))
                continue
            no, year = a["actNo"], a["actSubNo"]
            key = acts.act_key(no, year)
            row = _act_row(parsed, p, title, no, year, a["date"], store.bill(key))
            store.replace_bill(row, build_chunks(row, parsed))
            si = english_pdf_named(acts_dir, a["date"], a["actNoText"], "SINHALA")
            link_si(store, key, si)
            added += 1
    store.commit()
    print(f"  meta: {meta.build(store)}", file=sys.stderr)
    from . import titles as _titles
    print(f"  titles: {_titles.build(store)}", file=sys.stderr)
    links = acts.build(store)
    print(f"  links: {links}", file=sys.stderr)
    for k, why in skipped:
        print(f"  ! {k}: {why}", file=sys.stderr)
    print(f"Indexed {added} Acts; {len(skipped)} skipped", file=sys.stderr)
    return {"indexed": added, "skipped": skipped, **links}
