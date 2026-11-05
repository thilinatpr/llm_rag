"""Conversation log, one JSON record per turn, for analysing and improving answers.

Each record: ts, session, turn, type (ask|search), question, answer, error, seconds, and for the pipeline the queries it
ran, how many candidates were kept, whether it judged the evidence enough, what it said was missing, the cited
bill/sections with grades, warnings and LLM call timings.

Sinks (both best-effort; logging never breaks a request):
  - local JSONL: $BILLRAG_LOG_DIR/YYYY-MM-DD.jsonl (default data/chatlog)
  - R2: <prefix>/YYYY-MM-DD/<ts>-<session>-<turn>.json, one object per turn (objects can't be appended), when R2_* env and
    R2_BUCKET are set. Prefix is BILLRAG_LOG_PREFIX (default "chatlogs"). The container's disk is ephemeral, so R2 is
    where logs actually survive.
Set BILLRAG_CHATLOG=0 to turn logging off. Read them back with scripts/read_chatlogs.py.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
_s3 = None
_build = None


def build_id():
    """Short hash of the billrag source files running now, so a log record says which code produced it."""
    global _build
    if _build is None:
        import hashlib
        h = hashlib.sha1()
        for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
            h.update(f.name.encode() + f.read_bytes())
        _build = h.hexdigest()[:8]
    return _build


def enabled():
    return os.environ.get("BILLRAG_CHATLOG", "1") != "0"


def _r2():
    global _s3
    need = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    if not all(os.environ.get(k) for k in need):
        return None
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client(
            "s3", endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto", config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 3}))
    return _s3


def _write(rec, day, name):
    line = json.dumps(rec, ensure_ascii=False)
    try:
        d = Path(os.environ.get("BILLRAG_LOG_DIR", "data/chatlog"))
        d.mkdir(parents=True, exist_ok=True)
        with _lock, open(d / f"{day}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"chatlog: local write failed: {e}", file=sys.stderr)
    try:
        s3 = _r2()
        if s3:
            prefix = os.environ.get("BILLRAG_LOG_PREFIX", "chatlogs").strip("/")
            s3.put_object(Bucket=os.environ["R2_BUCKET"], Key=f"{prefix}/{day}/{name}.json",
                          Body=line.encode(), ContentType="application/json")
    except Exception as e:
        print(f"chatlog: R2 write failed: {type(e).__name__}: {e}", file=sys.stderr)


def log_turn(**fields):
    """Fire-and-forget (background thread) so the user never waits on storage."""
    if not enabled():
        return
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat(timespec="milliseconds"), "build": build_id(), **fields}
    sid = "".join(c for c in str(fields.get("session", "")) if c.isalnum())[:8] or "anon"
    name = f"{now.strftime('%H%M%S%f')[:9]}-{sid}-{fields.get('turn', 0)}"
    threading.Thread(target=_write, args=(rec, now.strftime("%Y-%m-%d"), name), daemon=True).start()


class Timer:
    def __enter__(self):
        self.t = time.time()
        return self

    def __exit__(self, *a):
        self.seconds = round(time.time() - self.t, 2)
