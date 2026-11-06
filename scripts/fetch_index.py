#!/usr/bin/env python3
"""Restore the built search index (index.sqlite + vectors.npz) from R2, so a fresh session can skip ingest + embed.

Credentials come from the environment (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET or --bucket).
Add --metadata to also fetch bills.json / bills.csv (the PDFs are not needed for search).
Add --pdfs SINHALA --since-year 2025 to fetch those PDFs too (needed by `python -m billrag ingest-si`).

  python scripts/fetch_index.py --bucket gov-rag [--data data/bills] [--metadata] [--force]
  python scripts/fetch_index.py --bucket gov-rag --pdfs SINHALA --since-year 2025
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_r2 import INDEX_FILES, PublishError, check_pair, client  # noqa: E402

PAIR_ATTEMPTS = 3
PAIR_WAIT = 20  # seconds; a publish swaps the two files within seconds


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET"))
    ap.add_argument("--prefix", default="bills")
    ap.add_argument("--data", default="data/bills")
    ap.add_argument("--metadata", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing local files")
    ap.add_argument("--pdfs", help="also fetch PDFs of this language (ENGLISH | SINHALA | TAMIL) into <data>/pdfs/<year>/")
    ap.add_argument("--since-year", type=int, default=2025, help="with --pdfs: only these year folders and newer")
    ap.add_argument("--index", action=argparse.BooleanOptionalAction, default=True, help="fetch index.sqlite + vectors.npz")
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("Pass --bucket or set R2_BUCKET")
    s3, root = client(), Path(a.data)
    root.mkdir(parents=True, exist_ok=True)
    if a.pdfs:
        fetch_pdfs(s3, a.bucket, a.prefix, root, a.pdfs.upper(), a.since_year, a.force)
    if a.metadata:
        fetch_files(s3, a.bucket, a.prefix, root, ["bills.json", "bills.csv"], a.force)
    if a.index:
        fetch_pair(s3, a.bucket, a.prefix, root, a.force)
    return 0


def fetch_files(s3, bucket, prefix, root, names, force) -> int:
    fetched = 0
    for name in names:
        dest = root / name
        if dest.exists() and not force:
            print(f"skip {dest} (exists; use --force)", file=sys.stderr)
            continue
        key = f"{prefix}/{name}"
        size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        tmp = dest.with_suffix(dest.suffix + ".part")
        s3.download_file(bucket, key, str(tmp))
        if tmp.stat().st_size != size:
            sys.exit(f"size mismatch for {key}: got {tmp.stat().st_size}, expected {size}")
        tmp.replace(dest)
        print(f"fetched {key} -> {dest} ({size / 1e6:.0f} MB)", file=sys.stderr)
        fetched += 1
    return fetched


def fetch_pair(s3, bucket, prefix, root, force, attempts=PAIR_ATTEMPTS, wait=PAIR_WAIT):
    """Download index + vectors and check they belong together. A restore that overlaps a publish can get the old index and the new
    vectors, and the container restores only once, so a mismatch would last until its next restart: fetch again instead."""
    if not fetch_files(s3, bucket, prefix, root, list(INDEX_FILES), force):
        return
    for n in range(1, attempts + 1):
        try:
            check_pair(root)
            return
        except PublishError as e:
            print(f"  ! index and vectors do not match (attempt {n}/{attempts}): {e}", file=sys.stderr)
        if n < attempts:
            time.sleep(wait)
            fetch_files(s3, bucket, prefix, root, list(INDEX_FILES), True)
    sys.exit(f"{prefix}/index.sqlite and vectors.npz in {bucket} do not match: re-publish them with scripts/upload_r2.py --include-index")


def fetch_pdfs(s3, bucket, prefix, root, lang, since_year, force):
    got = skipped = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/pdfs/"):
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")  # bills/pdfs/<year>/<NNN_YYYY>_<LANG>.pdf
            if len(parts) != 4 or not parts[3].endswith(f"_{lang}.pdf") or not parts[2].isdigit() or int(parts[2]) < since_year:
                continue
            dest = root / "pdfs" / parts[2] / parts[3]
            if dest.exists() and dest.stat().st_size == o["Size"] and not force:
                skipped += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, o["Key"], str(dest))
            got += 1
    print(f"pdfs: fetched {got}, already present {skipped} ({lang}, folders >= {since_year})", file=sys.stderr)


if __name__ == "__main__":
    main()
