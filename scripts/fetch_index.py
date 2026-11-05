#!/usr/bin/env python3
"""Restore the built search index (index.sqlite + vectors.npz) from R2, so a fresh session can skip ingest + embed.

Credentials come from the environment (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET or --bucket).
Add --metadata to also fetch bills.json / bills.csv (the PDFs are not needed for search).

  python scripts/fetch_index.py --bucket gov-rag [--data data/bills] [--metadata] [--force]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_r2 import INDEX_FILES, client  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET"))
    ap.add_argument("--prefix", default="bills")
    ap.add_argument("--data", default="data/bills")
    ap.add_argument("--metadata", action="store_true")
    ap.add_argument("--force", action="store_true", help="overwrite existing local files")
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("Pass --bucket or set R2_BUCKET")
    s3, root = client(), Path(a.data)
    root.mkdir(parents=True, exist_ok=True)
    names = list(INDEX_FILES) + (["bills.json", "bills.csv"] if a.metadata else [])
    for name in names:
        dest = root / name
        if dest.exists() and not a.force:
            print(f"skip {dest} (exists; use --force)", file=sys.stderr)
            continue
        key = f"{a.prefix}/{name}"
        size = s3.head_object(Bucket=a.bucket, Key=key)["ContentLength"]
        tmp = dest.with_suffix(dest.suffix + ".part")
        s3.download_file(a.bucket, key, str(tmp))
        if tmp.stat().st_size != size:
            sys.exit(f"size mismatch for {key}: got {tmp.stat().st_size}, expected {size}")
        tmp.replace(dest)
        print(f"fetched {key} -> {dest} ({size / 1e6:.0f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main()
