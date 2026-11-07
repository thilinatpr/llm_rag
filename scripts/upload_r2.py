#!/usr/bin/env python3
"""Upload the scraped bills (PDFs + metadata) to a Cloudflare R2 bucket.

Credentials come from the environment (never from files in the repo):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET (or --bucket)

Key layout mirrors data/bills/:  bills/pdfs/<year>/<bill>_<LANG>.pdf, bills/bills.json, bills/bills.csv
Idempotent: objects that already exist with the same size are skipped.

  python scripts/upload_r2.py --bucket gov-rag [--prefix bills] [--dry-run] [--include-index]

--manifest-only writes bills/indexed-keys.json (the documents in the live index; --include-index writes it after every publish).
--include-index also publishes index.sqlite and vectors.npz (always, as they can change without changing size), as one pair: a
container that restores a new index with old vectors, or the reverse, refuses to start (VectorsError). See publish_index.
Restore them in a fresh session with scripts/fetch_index.py.
"""
import argparse
import hashlib
import mimetypes
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config


INDEX_FILES = ("index.sqlite", "vectors.npz")
# Eight parallel parts of 8 MB lost a connection part-way through a 230 MB upload on 2026-11-06; this setting then worked.
INDEX_TRANSFER = TransferConfig(multipart_chunksize=16 * 1024 * 1024, max_concurrency=3)
INDEX_ATTEMPTS = 4


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


class PublishError(RuntimeError):
    """The pair was not published. The message says what state the live files are in."""


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def check_pair(root: Path):
    """Refuse a pair that could not serve: vectors.npz must be stamped with the fingerprint of this index.sqlite."""
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from billrag.store import Store
    for name in INDEX_FILES:
        if not (root / name).is_file():
            raise PublishError(f"{root / name} is missing. Nothing was changed.")
    store = Store(root / "index.sqlite")
    try:
        same = "fingerprint" in (z := np.load(root / "vectors.npz")).files and str(z["fingerprint"]) == store.fingerprint()
    finally:
        store.db.close()
    if not same:
        raise PublishError("vectors.npz was not built from this index.sqlite: run `python -m billrag embed`. Nothing was changed.")


MANIFEST = "indexed-keys.json"


def index_keys(root: Path):
    """The documents (bill and Act keys) in root/index.sqlite, sorted."""
    import sqlite3
    db = sqlite3.connect(str(root / "index.sqlite"))
    try:
        return sorted(r[0] for r in db.execute("SELECT key FROM bills"))
    finally:
        db.close()


def write_manifest(s3, bucket, prefix, root: Path):
    """{prefix}/indexed-keys.json = which documents the LIVE index holds. The admin panel's daily check (src/refresh.js) compares the
    documents.gov.lk listing with it to find new bills. Written after the pair is published, so it never claims more than is live."""
    import json
    keys = index_keys(root)
    s3.put_object(Bucket=bucket, Key=f"{prefix}/{MANIFEST}", ContentType="application/json",
                  Body=json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "keys": keys}).encode())
    print(f"Wrote {prefix}/{MANIFEST}: {len(keys)} documents.", file=sys.stderr)


def _exists(s3, bucket, key) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _staged(s3, bucket, path: Path, key: str, attempts=INDEX_ATTEMPTS):
    """Upload to a staging key and read it back: what will go live is checked, not what we meant to send."""
    want, err = sha256(path), None
    for n in range(1, attempts + 1):
        try:
            s3.upload_file(str(path), bucket, key, Config=INDEX_TRANSFER, ExtraArgs={"ContentType": "application/octet-stream"})
            with tempfile.TemporaryDirectory() as d:
                back = Path(d) / path.name
                s3.download_file(bucket, key, str(back))
                if sha256(back) == want:
                    print(f"  staged and verified {key}", file=sys.stderr)
                    return
            err = "the staged copy has a different checksum"
        except Exception as e:  # a lost connection on one attempt is the expected failure here
            err = f"{type(e).__name__}: {str(e)[:160]}"
        print(f"  ! {key} attempt {n}/{attempts}: {err}", file=sys.stderr)
        if n < attempts:
            time.sleep(5)
    raise PublishError(f"could not stage {path.name} ({err}). The live files were not touched.")


def publish_index(s3, bucket, prefix, root: Path, stamp=None):
    """Replace the live index + vectors as a pair, so that no failed upload can leave a mismatch.

    1. Check the pair locally. 2. Upload both to a staging prefix and verify them by checksum: a failure up to here leaves the live files
    untouched. 3. Back up the live pair. 4. Swap by server-side copy (no upload, so no long transfer to fail); if the swap fails, put back
    whatever was changed. R2 has no multi-object transaction, so the two live keys disagree while the second copy runs: 3.5-6 s measured for 67 MB of vectors,
    which is why the 230 MB index goes first (it takes 14-18 s). A container restoring across that moment is caught by the same check in
    scripts/fetch_index.py, which fetches again."""
    stamp = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    check_pair(root)
    live = {n: f"{prefix}/{n}" for n in INDEX_FILES}
    staging = {n: f"{prefix}/.staging/{stamp}/{n}" for n in INDEX_FILES}
    backup = {n: f"backups/{prefix}/{stamp}/{n}" for n in INDEX_FILES}
    for n in INDEX_FILES:
        _staged(s3, bucket, root / n, staging[n])
    had = {n: _exists(s3, bucket, live[n]) for n in INDEX_FILES}
    try:
        for n in INDEX_FILES:
            if had[n]:
                s3.copy_object(Bucket=bucket, Key=backup[n], CopySource={"Bucket": bucket, "Key": live[n]})
    except Exception as e:
        raise PublishError(f"could not back up the live files ({type(e).__name__}: {e}). The live files were not touched; staged at "
                           f"{prefix}/.staging/{stamp}/.") from e
    done = []
    try:
        for n in INDEX_FILES:
            s3.copy_object(Bucket=bucket, Key=live[n], CopySource={"Bucket": bucket, "Key": staging[n]})
            done.append(n)
    except Exception as e:
        undo = []
        for n in done:
            try:
                if had[n]:
                    s3.copy_object(Bucket=bucket, Key=live[n], CopySource={"Bucket": bucket, "Key": backup[n]})
                else:
                    s3.delete_object(Bucket=bucket, Key=live[n])
            except Exception as e2:
                undo.append(f"{n}: {type(e2).__name__}: {e2}")
        if undo:
            raise PublishError(f"SWAP FAILED AND ROLLBACK FAILED, THE LIVE PAIR IS MISMATCHED: {'; '.join(undo)}. Fix at once: copy "
                               f"backups/{prefix}/{stamp}/ over {prefix}/.") from e
        raise PublishError(f"the swap failed ({type(e).__name__}: {e}); the previous pair was put back.") from e
    for n in INDEX_FILES:
        s3.delete_object(Bucket=bucket, Key=staging[n])
    kept = f"backups/{prefix}/{stamp}/" if any(had.values()) else "none (nothing was live)"
    print(f"Published {prefix}/{{{','.join(INDEX_FILES)}}}. Previous pair: {kept}. Delete old backups when no longer needed.", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/bills")
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET"))
    ap.add_argument("--prefix", default="bills")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-index", action="store_true", help="also publish index.sqlite and vectors.npz as a pair")
    ap.add_argument("--manifest-only", action="store_true",
                    help="only write indexed-keys.json from --data/index.sqlite (use once, with the index that is live, so the daily new-bills check has a list)")
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("Pass --bucket or set R2_BUCKET")
    root = Path(a.data)
    if a.manifest_only:
        write_manifest(client(), a.bucket, a.prefix, root)
        return 0
    files = sorted((root / "pdfs").rglob("*.pdf")) + [root / n for n in ("bills.json", "bills.csv") if (root / n).exists()]
    s3 = client()
    have = existing(s3, a.bucket, a.prefix)
    todo = [(p, f"{a.prefix}/{p.relative_to(root).as_posix()}") for p in files]
    todo = [(p, k) for p, k in todo if have.get(k) != p.stat().st_size]
    print(f"{len(files)} local files, {len(files) - len(todo)} already in R2, {len(todo)} to upload "
          f"({sum(p.stat().st_size for p, _ in todo) / 1e6:.0f} MB)", file=sys.stderr)
    if a.dry_run:
        if a.include_index:
            try:
                check_pair(root)
                print(f"Would publish the index pair ({sum((root / n).stat().st_size for n in INDEX_FILES) / 1e6:.0f} MB); it is consistent", file=sys.stderr)
            except PublishError as e:
                print(f"Would NOT publish the index pair: {e}", file=sys.stderr)
                return 1
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
    if a.include_index:
        try:
            publish_index(s3, a.bucket, a.prefix, root)
        except PublishError as e:
            print(f"Index NOT published: {e}", file=sys.stderr)
            return 1
        try:
            write_manifest(s3, a.bucket, a.prefix, root)
        except Exception as e:  # the index is live; only the daily check's list is stale
            print(f"WARNING: published, but the manifest was not written ({type(e).__name__}: {e}). Run with --manifest-only.", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
