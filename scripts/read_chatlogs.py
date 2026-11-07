#!/usr/bin/env python3
"""Download and summarise the conversation logs written by `billrag serve` (see billrag/chatlog.py).

Credentials from the environment (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET or --bucket).

  python scripts/read_chatlogs.py [--since 2026-11-05] [--prefix chatlogs] [--out chatlogs.jsonl] [--show 20]

Prints totals plus the turns most worth a look: answers that found nothing, evidence judged insufficient, errors and slow
turns, and CPU/memory per turn from the `usage` field (see billrag/resources.py). With --out the raw records are
also written as one JSONL file.
"""
import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from upload_r2 import client  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from billrag import calltrace  # noqa: E402
from billrag.resources import usage_summary  # noqa: E402


def fetch(s3, bucket, prefix, since):
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix.strip("/") + "/"}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", []) if o["Key"].split("/")[1] >= since]
        if not r.get("IsTruncated"):
            break
        token = r["NextContinuationToken"]
    get = lambda k: json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
    with ThreadPoolExecutor(8) as ex:
        return sorted(ex.map(get, keys), key=lambda r: r["ts"])


def short(s, n=110):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET"))
    ap.add_argument("--prefix", default="chatlogs")
    ap.add_argument("--since", default="0000-00-00", help="first day to include, YYYY-MM-DD")
    ap.add_argument("--out")
    ap.add_argument("--show", type=int, default=15)
    a = ap.parse_args()
    if not a.bucket:
        sys.exit("Pass --bucket or set R2_BUCKET")
    recs = fetch(client(), a.bucket, a.prefix, a.since)
    if a.out:
        Path(a.out).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    asks = [r for r in recs if r.get("type") == "ask"]
    print(f"{len(recs)} records: {len(asks)} questions, {len(recs) - len(asks)} searches, "
          f"{len({r.get('session') for r in asks})} sessions")
    if not asks:
        return
    secs = sorted(r.get("seconds") or 0 for r in asks)
    used_backup = [r for r in asks if r.get("fallback")]
    if used_backup:
        print(f"backup model used on {len(used_backup)} of {len(asks)} questions (reasons: "
              f"{sorted({r['fallback'].get('reason') for r in used_backup})}); their `cost.usd` leaves out the backup's calls")
    print(f"seconds: median {secs[len(secs) // 2]:.1f}, p90 {secs[int(len(secs) * 0.9)]:.1f}, max {secs[-1]:.1f}")
    for line in usage_summary(recs):
        print(line)
    costs = [r["cost"] for r in asks if r.get("cost") and r["cost"].get("priced_calls")]
    if costs:  # model spend per answer (priced models only; see billrag/calltrace.py PRICES)
        usd = sorted(c["usd"] for c in costs)
        print(f"model cost ({len(costs)} answers with a priced model): total ${sum(usd):.4f}, median ${usd[len(usd) // 2]:.5f}, "
              f"p90 ${usd[int(len(usd) * 0.9)]:.5f}, max ${usd[-1]:.5f}; cache-hit input "
              f"{sum(c['cache_hit'] for c in costs) / max(1, sum(c['in'] for c in costs)):.0%}")
    rows = calltrace.summary([r.get("calls") for r in asks])
    if rows:
        print(f"\nmodel/API calls by step ({sum(1 for r in asks if r.get('calls'))} questions with call records; s = seconds per call)")
        print(f"  {'step':<15}{'kind':<11}{'model':<42}{'n':>4}{'med s':>8}{'max s':>8}{'med out':>9}{'out/s':>7}{'retry':>6}")
        for st, kind, model, n, ms, mx, mo, rate, retr in rows:
            print(f"  {st:<15}{kind:<11}{model[-41:]:<42}{n:>4}{ms:>8.2f}{mx:>8.2f}{mo if mo is not None else '-':>9}"
                  f"{rate if rate is not None else '-':>7}{retr:>6}")

    def show(title, rows):
        print(f"\n{title} ({len(rows)})")
        for r in rows[: a.show]:
            print(f"  {r['ts'][:19]}  {short(r.get('question'), 70)!r}  ->  {short(r.get('answer') or r.get('error'))}")

    show("errors", [r for r in asks if r.get("error")])
    show("found nothing", [r for r in asks if not r.get("evidence") and not r.get("error")])
    show("evidence judged insufficient", [r for r in asks if r.get("evidence") and r.get("enough") is False])
    show("slowest", sorted(asks, key=lambda r: -(r.get("seconds") or 0)))


if __name__ == "__main__":
    main()
