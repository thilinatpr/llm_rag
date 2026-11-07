#!/usr/bin/env python3
"""Scrape Sri Lankan parliamentary bills from documents.gov.lk.

The bills page is a Next.js app whose list is loaded through a server action
(POST /web/bills with a `Next-Action` header). We replay that action to get
bill metadata as JSON, then download each bill's PDFs (Sinhala/Tamil/English)
from /api/content-file-proxy.

Outputs (default under data/bills/, which is git-ignored):
  bills.json         full metadata for every bill
  bills.csv          one row per bill/language file, with local path
  pdfs/<year>/<billNoText>_<LANG>.pdf

Re-runnable: existing non-empty PDFs are skipped.

If the site is redeployed the action ID changes; pass the new one with
--action-id (find it in DevTools -> Network -> POST /web/bills -> Next-Action).

Usage:
  python scripts/scrape_bills.py                  # metadata + all PDFs
  python scripts/scrape_bills.py --langs ENGLISH  # English only
  python scripts/scrape_bills.py --metadata-only
  python scripts/scrape_bills.py --since-year 2020
"""
import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://documents.gov.lk"
DEFAULT_ACTION_ID = "40dd03742cae2465a031936880def365fdd014c22b"
# What each listing page itself sends: same server action, its own page and backend path. `no` is the number field in its records.
SOURCES = {"bills": {"page": "/web/bills", "api": "http://gvp-api:4500/website-data/bill/get-all", "no": "billNoText", "file": "bills"},
           "acts": {"page": "/web/acts", "api": "http://gvp-api:4500/website-data/act/get-all", "no": "actNoText", "file": "acts"}}
UA = "Mozilla/5.0 (compatible; llm-rag-bill-scraper/1.0)"


def http(req, retries=4, timeout=60):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            code = getattr(e, "code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))


def fetch_page(page, limit, action_id, source="bills"):
    body = json.dumps([{
        "apiEndpoint": SOURCES[source]["api"], "page": page, "limit": limit,
        "q": "", "search": "", "forwarded": {},
    }]).encode()
    req = urllib.request.Request(
        f"{BASE}{SOURCES[source]['page']}", data=body, method="POST",
        headers={"Next-Action": action_id, "Accept": "text/x-component",
                 "Content-Type": "text/plain;charset=UTF-8", "User-Agent": UA})
    text = http(req).decode("utf-8")
    for line in text.split("\n"):
        if line.startswith("1:"):
            return json.loads(line[2:])
    raise RuntimeError("Unexpected response (action ID stale?): " + text[:200])


def fetch_all_metadata(action_id, limit=100, source="bills"):
    first = fetch_page(1, limit, action_id, source)
    bills = list(first["data"])
    total_pages = int(first["pagination"]["totalPages"])
    for p in range(2, total_pages + 1):
        bills.extend(fetch_page(p, limit, action_id, source)["data"])
        print(f"  metadata page {p}/{total_pages}", file=sys.stderr)
    total = int(first["pagination"]["total"])
    if len(bills) != total:
        print(f"warning: expected {total} bills, got {len(bills)}", file=sys.stderr)
    # de-duplicate by id in case the list shifted while paging
    return list({b["id"]: b for b in bills}.values())


def safe(s):
    return re.sub(r"[^\w.-]+", "_", s)


def pdf_path(out, bill, lang):
    year = (bill.get("date") or "")[:4] or "unknown"
    no = bill.get("billNoText") or bill["actNoText"]
    return out / "pdfs" / year / f"{safe(no)}_{lang}.pdf"


def csv_path(out, name, since_year):
    """The CSV lists only the years scraped. A partial list gets its own name, so upload_r2.py (which sends bills.csv) cannot replace the full one in R2."""
    return out / (f"{name}.since-{since_year}.csv" if since_year else f"{name}.csv")


def download(url_file, dest):
    if dest.exists() and dest.stat().st_size > 0:
        return "skipped"
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/api/content-file-proxy?" + urllib.parse.urlencode(
        {"file": "/" + url_file.lstrip("/")})
    data = http(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120)
    if not data.startswith(b"%PDF"):
        raise RuntimeError("not a PDF")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return "downloaded"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=sorted(SOURCES), default="bills", help="which listing (acts: use --out data/acts)")
    ap.add_argument("--out", default=None, help="default data/<source>")
    ap.add_argument("--action-id", default=DEFAULT_ACTION_ID)
    ap.add_argument("--langs", nargs="+", default=["SINHALA", "TAMIL", "ENGLISH"],
                    choices=["SINHALA", "TAMIL", "ENGLISH"])
    ap.add_argument("--since-year", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--metadata-only", action="store_true")
    args = ap.parse_args()

    src = SOURCES[args.source]
    out = Path(args.out or f"data/{args.source}")
    out.mkdir(parents=True, exist_ok=True)

    print(f"Fetching {args.source} metadata...", file=sys.stderr)
    bills = fetch_all_metadata(args.action_id, source=args.source)
    bills.sort(key=lambda b: b.get("date") or "", reverse=True)
    (out / f"{src['file']}.json").write_text(
        json.dumps(bills, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{len(bills)} {args.source} -> {out / (src['file'] + '.json')}", file=sys.stderr)

    jobs = []
    for b in bills:
        if args.since_year and int((b.get("date") or "0")[:4] or 0) < args.since_year:
            continue
        for c in b.get("contents", []):
            if c["language"] in args.langs:
                jobs.append((b, c))

    results = {}
    if not args.metadata_only:
        print(f"Downloading {len(jobs)} PDFs with {args.workers} workers...", file=sys.stderr)
        with ThreadPoolExecutor(args.workers) as ex:
            futs = {ex.submit(download, c["uploadedFile"], pdf_path(out, b, c["language"])):
                    (b, c) for b, c in jobs}
            done = 0
            for f in as_completed(futs):
                b, c = futs[f]
                try:
                    results[(b["id"], c["language"])] = f.result()
                except Exception as e:  # keep going; report at the end
                    results[(b["id"], c["language"])] = f"failed: {e}"
                done += 1
                if done % 100 == 0:
                    print(f"  {done}/{len(jobs)}", file=sys.stderr)

    with csv_path(out, src["file"], args.since_year).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["bill_no", "date", "title", "language", "original_file",
                    "remote_file", "local_path", "status"])
        for b, c in jobs:
            p = pdf_path(out, b, c["language"])
            w.writerow([b[src["no"]], (b.get("date") or "")[:10],
                        b.get("descriptionEnglish") or "", c["language"],
                        c.get("originalFileName", ""), c["uploadedFile"],
                        str(p) if p.exists() else "",
                        results.get((b["id"], c["language"]), "not attempted")])

    failed = [k for k, v in results.items() if v.startswith("failed")]
    n_dl = sum(v == "downloaded" for v in results.values())
    n_skip = sum(v == "skipped" for v in results.values())
    print(f"Done: {n_dl} downloaded, {n_skip} skipped, {len(failed)} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
