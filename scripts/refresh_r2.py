#!/usr/bin/env python3
"""R2 side of the scheduled index refresh (.github/workflows/refresh-index.yml). The admin panel (src/refresh.js) reads what this writes.

  refresh_r2.py status running|built|failed|nothing_new|published [--mode build|publish] [--report report.json] [--run-url URL]
      writes refresh/status.json (state, time, the report's fields, the run's address); also refresh/skipped.json when the report lists `unindexed`
  refresh_r2.py stage   --data DIR     upload DIR/index.sqlite + vectors.npz to refresh/staging/ (checked by size), nothing live changes
  refresh_r2.py unstage --data DIR     download the staged pair into DIR (then upload_r2.py --include-index publishes it with its checks)
  refresh_r2.py clear                  delete refresh/staging/

Credentials from the environment, as upload_r2.py (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET).
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_r2 import INDEX_FILES, INDEX_TRANSFER, client  # noqa: E402

STATUS = "refresh/status.json"
SKIPPED = "refresh/skipped.json"
STAGING = "refresh/staging/"
KEEP = ("new_bills", "new_acts", "mrr", "min_mrr", "eval_items", "chunks_added", "problems", "unparsed", "no_summary", "base_acts_missing", "base_acts_added", "error")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def put_json(s3, bucket, key, obj):
    s3.put_object(Bucket=bucket, Key=key, ContentType="application/json", Body=json.dumps(obj, ensure_ascii=False).encode())


def status_record(state, mode=None, report=None, run_url=None):
    """What the panel reads. `report` is refresh_bills.py --report; its own `state` is used when `state` is "report"."""
    rec = {"state": state, "at": now()}
    if mode:
        rec["mode"] = mode
    if run_url:
        rec["run_url"] = run_url
    for k in KEEP:
        if report and report.get(k) not in (None, [], {}):
            rec[k] = report[k]
    return rec


def stage(s3, bucket, root):
    for n in INDEX_FILES:
        path = root / n
        s3.upload_file(str(path), bucket, STAGING + n, Config=INDEX_TRANSFER)
        got = s3.head_object(Bucket=bucket, Key=STAGING + n)["ContentLength"]
        if got != path.stat().st_size:
            raise SystemExit(f"staged {n} is {got} bytes, expected {path.stat().st_size}")
    print(f"Staged {', '.join(INDEX_FILES)} at {STAGING}", file=sys.stderr)


def unstage(s3, bucket, root):
    root.mkdir(parents=True, exist_ok=True)
    for n in INDEX_FILES:
        s3.download_file(bucket, STAGING + n, str(root / n), Config=INDEX_TRANSFER)
    print(f"Downloaded the staged pair into {root}", file=sys.stderr)


def clear(s3, bucket):
    for n in INDEX_FILES:
        s3.delete_object(Bucket=bucket, Key=STAGING + n)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "stage", "unstage", "clear"])
    ap.add_argument("state", nargs="?", help="with `status`: running | built | failed | nothing_new | published")
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET") or "gov-rag")
    ap.add_argument("--data", default="data/refresh/bills")
    ap.add_argument("--mode")
    ap.add_argument("--report")
    ap.add_argument("--run-url")
    a = ap.parse_args(argv)
    s3 = client()
    if a.cmd == "status":
        if not a.state:
            ap.error("status needs a state")
        report = json.loads(Path(a.report).read_text(encoding="utf-8")) if a.report and Path(a.report).exists() else None
        put_json(s3, a.bucket, STATUS, status_record(a.state, a.mode, report, a.run_url))
        if report and "unindexed" in report:
            put_json(s3, a.bucket, SKIPPED, {"at": now(), "keys": report["unindexed"]})
        print(f"Status: {a.state}", file=sys.stderr)
    elif a.cmd == "stage":
        stage(s3, a.bucket, Path(a.data))
    elif a.cmd == "unstage":
        unstage(s3, a.bucket, Path(a.data))
    else:
        clear(s3, a.bucket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
