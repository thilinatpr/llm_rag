#!/usr/bin/env python3
"""Upload the scraped bills (PDFs + metadata) to a Cloudflare R2 bucket.

Credentials come from the environment (never from files in the repo):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (or --bucket)

Key layout mirrors data/bills/:  bills/pdfs/<year>/<bill>_<LANG>.pdf, bills/bills.json, bills/bills.csv
Idempotent: objects that already exist with the same size are skipped.

  python scripts/upload_r2.py --bucket gov-rag [--prefix bills] [--dry-run] [--include-index]

--include-index also uploads index.sqlite and vectors.npz (always re-uploaded: they can change without changing size).
Restore them in a fresh session with scripts/fetch_index.py.
"""
import argparse
import mimetypes
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config


INDEX_FILES = ("index.sqlite", "vectors.npz")


def client():
    need = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"]
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        sys.exit("Missing env vars: " + ", ".join(missing))
    return boto3.client(
        "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto", config=Config(connect_timeout=15, read_timeout=120, retries={"max_attempts": 5, "mode": "standard"}))


def existing(s3, bucket, prefix):
    sizes, token = {}, None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix + "/"}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            sizes[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            return sizes
        token = r["NextContinuationToken"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/bills")
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET"))
    ap.add_argument("--prefix", default="bills")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-index", action="store_true", help="also upload index.sqlite and vectors.npz")
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("Pass --bucket or set R2_BUCKET")
    root = Path(a.data)
    files = sorted((root / "pdfs").rglob("*.pdf")) + [root / n for n in ("bills.json", "bills.csv") if (root / n).exists()]
    always = set()
    if a.include_index:
        for name in INDEX_FILES:
            if (root / name).exists():
                files.append(root / name)
                always.add(name)
    s3 = client()
    have = existing(s3, a.bucket, a.prefix)
    todo = [(p, f"{a.prefix}/{p.relative_to(root).as_posix()}") for p in files]
    todo = [(p, k) for p, k in todo if have.get(k) != p.stat().st_size or p.name in always]
    print(f"{len(files)} local files, {len(files) - len(todo)} already in R2, {len(todo)} to upload "
          f"({sum(p.stat().st_size for p, _ in todo) / 1e6:.0f} MB)", file=sys.stderr)
    if a.dry_run:
        return 0

    def up(item):
        p, k = item
        try:
            s3.upload_file(str(p), a.bucket, k, ExtraArgs={
                "ContentType": mimetypes.guess_type(p.name)[0] or "application/octet-stream"})
            return k, None
        except Exception as e:
            return k, f"{type(e).__name__}: {e}"

    failed = []
    with ThreadPoolExecutor(a.workers) as ex:
        for n, (k, err) in enumerate(ex.map(up, todo), 1):
            if err:
                failed.append((k, err))
                print(f"  ! {k}: {err}", file=sys.stderr)
            elif n % 250 == 0:
                print(f"  {n}/{len(todo)}", file=sys.stderr)
    print(f"Done: {len(todo) - len(failed)} uploaded, {len(failed)} failed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
