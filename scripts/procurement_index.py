#!/usr/bin/env python3
"""Procurement Rules' index (data/procurement/index.sqlite) <-> R2, so its image can be built on GitHub's servers (deploy.yml, target
procurement). The index is built from the owner's PDFs (`python -m procurement build`), which only exist on the owner's machine: upload it
after each rebuild; the deploy job fetches it and checks its SHA-256.

  set -a; . ./.local.env; set +a
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/procurement_index.py upload   # after `python -m procurement build`
  python scripts/procurement_index.py fetch                                                      # deploy job: R2 -> data/procurement/

R2 keys (bucket R2_BUCKET or --bucket): procurement/index.sqlite, procurement/index.json ({sha256, bytes, uploaded}). Public documents
only (NPC guidelines and manuals, circulars, FR, the Constitution), 17 MB.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_r2 import client  # noqa: E402

KEY, META = "procurement/index.sqlite", "procurement/index.json"
LOCAL = Path("data/procurement/index.sqlite")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def upload(s3, bucket, path):
    digest, size = sha256(path), path.stat().st_size
    s3.upload_file(str(path), bucket, KEY)
    head = s3.head_object(Bucket=bucket, Key=KEY)
    if head["ContentLength"] != size:
        sys.exit(f"upload incomplete: {head['ContentLength']} of {size} bytes")
    meta = {"sha256": digest, "bytes": size, "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    s3.put_object(Bucket=bucket, Key=META, Body=json.dumps(meta).encode(), ContentType="application/json")
    print(f"uploaded {path} -> {bucket}/{KEY} ({size} bytes, sha256 {digest[:12]})")


def fetch(s3, bucket, path):
    meta = json.loads(s3.get_object(Bucket=bucket, Key=META)["Body"].read())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    s3.download_file(bucket, KEY, str(tmp))
    got = sha256(tmp)
    if got != meta["sha256"]:
        tmp.unlink()
        sys.exit(f"checksum mismatch: {got[:12]} != {meta['sha256'][:12]} (an upload in progress?): try again")
    tmp.replace(path)
    print(f"fetched {bucket}/{KEY} -> {path} ({meta['bytes']} bytes, uploaded {meta['uploaded']}, sha256 ok)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["upload", "fetch"])
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET") or "gov-rag")
    ap.add_argument("--path", default=str(LOCAL))
    a = ap.parse_args(argv)
    s3 = client()
    (upload if a.action == "upload" else fetch)(s3, a.bucket, Path(a.path))


if __name__ == "__main__":
    main()
