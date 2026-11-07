#!/usr/bin/env python3
"""How many people at once can a server take? Closed loop: each simulated person asks, waits for the whole answer, asks again.

  python scripts/load_test.py --url http://127.0.0.1:8000 --secret "$SECRET" --levels 1,2,4,8 --rounds 2 [--docker NAME]
  python scripts/load_test.py --url https://bills.citizenlk.com --cookie "session=..." --levels 1,2 --rounds 1      # the real container
  python scripts/load_test.py --stub-model --port 9099 --grade-s 2.5 --answer-s 3     # a stand-in for the chat model (no spend)
  python scripts/load_test.py --stub-model --port 9099 --fail 402                     # ... that is out of credit, for the fallback

--app bills uses /api/ask/stream (time to first word is measured); --app procurement uses /api/ask. --lane public sends X-Lane: public, as the
Worker does for visitors (only with --secret: direct to a container). Every question costs real model money unless the server uses --stub-model,
and on a live site each one is a real question in the chat logs. Questions come from the eval sets. A person who is refused (503, or a `busy`
event) counts as `busy`. Memory and CPU come from /api/stats (bills) and `docker stats` (--docker).
"""
import argparse
import json
import random
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UA = "citizenlk-load-test/1.0"


def questions(app):
    if app == "procurement":
        rows = [json.loads(x) for x in (ROOT / "evals/procurement_eval.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        return [r["question"] for r in rows if "history" not in r]
    rows = [json.loads(x) for x in (ROOT / "evals/retrieval_eval.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r["question"] for r in rows if r["source"]["year"] >= 2025]


def headers(a):
    h = {"Content-Type": "application/json", "User-Agent": UA}
    if a.secret:
        h["X-Proxy-Secret"] = a.secret
    if a.cookie:
        h["Cookie"] = a.cookie
    if a.lane == "public":
        h["X-Lane"] = "public"
    return h


def ask_once(a, session, question):
    """-> {outcome: ok|busy|failed, s, first, detail}"""
    t = time.time()
    body = {"session": session, "question": question}
    if a.answer:
        body["answer"] = a.answer
    path = "/api/ask/stream" if a.app == "bills" else "/api/ask"
    req = urllib.request.Request(a.url + path, data=json.dumps(body).encode(), headers=headers(a), method="POST")
    first = None
    try:
        with urllib.request.urlopen(req, timeout=a.timeout) as r:
            if a.app != "bills":
                j = json.load(r)
                return {"outcome": "ok" if j.get("answer") else "failed", "s": time.time() - t, "first": None}
            kind = None
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    if kind == "token" and first is None:
                        first = time.time() - t
                    elif kind == "done":
                        return {"outcome": "ok", "s": time.time() - t, "first": first}
                    elif kind == "error":
                        d = json.loads(line[6:])
                        return {"outcome": "busy" if d.get("busy") else "failed", "s": time.time() - t, "first": first, "detail": d.get("error")}
            return {"outcome": "failed", "s": time.time() - t, "first": first, "detail": "stream ended without an answer"}
    except urllib.error.HTTPError as e:
        return {"outcome": "busy" if e.code == 503 else "failed", "s": time.time() - t, "first": first, "detail": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001 - a dropped connection or timeout is a result here
        return {"outcome": "failed", "s": time.time() - t, "first": first, "detail": f"{type(e).__name__}: {e}"}


class Sampler(threading.Thread):
    """Polls /api/stats and `docker stats` while a level runs. Keeps the highest values seen."""

    def __init__(self, a):
        super().__init__(daemon=True)
        self.a, self.stop_flag, self.peak = a, threading.Event(), {}

    def note(self, k, v):
        if v is not None:
            self.peak[k] = max(self.peak.get(k, v), v)

    def run(self):
        while not self.stop_flag.wait(2):
            if self.a.app == "bills" and self.a.stats:
                try:
                    with urllib.request.urlopen(urllib.request.Request(self.a.url + "/api/stats", headers=headers(self.a)), timeout=5) as r:
                        s = json.load(r)
                    self.note("rss_mb", s.get("rss_mb"))
                    self.note("running", s.get("running"))
                    self.note("queued", s.get("queued"))
                except Exception:  # noqa: BLE001 - not answering in 5 s is itself shown by the times
                    self.note("stats_missed", self.peak.get("stats_missed", 0) + 1)
            if self.a.docker:
                out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}} {{.MemUsage}}", self.a.docker],
                                     capture_output=True, text=True, check=False).stdout
                m = re.match(r"([\d.]+)%\s+([\d.]+)(MiB|GiB)", out)
                if m:
                    self.note("docker_cpu_pct", float(m[1]))
                    self.note("docker_mem_mb", float(m[2]) * (1024 if m[3] == "GiB" else 1))


def run_level(a, level, pool):
    results, lock = [], threading.Lock()
    run_id = f"{int(time.time())}"

    def person(n):
        rng = random.Random(n + level * 1000)
        for turn in range(a.rounds):
            r = ask_once(a, f"load-{run_id}-{level}-{n}", rng.choice(pool))
            with lock:
                results.append(r)
            if turn + 1 < a.rounds:
                time.sleep(a.think)

    sampler = Sampler(a)
    sampler.start()
    started = time.time()
    people = [threading.Thread(target=person, args=(n,)) for n in range(level)]
    for p in people:
        p.start()
    for p in people:
        p.join()
    sampler.stop_flag.set()
    ok = sorted(r["s"] for r in results if r["outcome"] == "ok")
    firsts = sorted(r["first"] for r in results if r["outcome"] == "ok" and r["first"] is not None)
    row = {"people": level, "asked": len(results), "ok": len(ok), "busy": sum(r["outcome"] == "busy" for r in results),
           "failed": sum(r["outcome"] == "failed" for r in results), "wall_s": round(time.time() - started, 1),
           "median_s": round(statistics.median(ok), 1) if ok else None, "p90_s": round(ok[int(len(ok) * 0.9)], 1) if ok else None,
           "max_s": round(ok[-1], 1) if ok else None, "first_word_s": round(statistics.median(firsts), 1) if firsts else None, **sampler.peak}
    details = sorted({r["detail"] for r in results if r.get("detail")})
    return row, details


def client(a):
    pool = questions(a.app)
    print(f"{a.url} app={a.app} lane={a.lane} rounds={a.rounds}, {len(pool)} questions in the pool", flush=True)
    for level in (int(x) for x in a.levels.split(",")):
        row, details = run_level(a, level, pool)
        print(json.dumps(row), flush=True)
        for d in details[:5]:
            print("   ", d[:200], flush=True)
        if a.out:
            with open(a.out, "a", encoding="utf-8") as f:
                f.write(json.dumps({"label": a.label, "app": a.app, "lane": a.lane, **row}) + "\n")
        time.sleep(a.rest)


def stub_model(a):
    """A chat model with realistic delays, answering in the shapes the pipeline expects. Only for measuring the server itself: the answers mean nothing."""

    class Stub(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *x):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if a.fail:
                out = json.dumps({"error": {"message": "Insufficient Balance" if a.fail == 402 else "stub failure"}}).encode()
                self.send_response(a.fail)
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)
                return
            sys_msg = body["messages"][0]["content"]
            user = body["messages"][-1]["content"]
            if "valid JSON" in sys_msg and "scores" in user:
                n = len(re.findall(r"^\[(\d+)\]", user, re.M))
                text, wait = json.dumps({"scores": [{"i": i, "s": 3 if i < 4 else 0} for i in range(n)], "enough": True, "missing": "", "queries": []}), a.grade_s
            elif "valid JSON" in sys_msg or "language" in sys_msg[:300].lower():
                text, wait = json.dumps({"language": "en", "english": user[:300]}), 1.0
            elif "rewrite questions" in sys_msg:
                text, wait = user[-120:], 1.0
            else:
                cites = re.findall(r"^(\[[^\]\n]*(?:§|Art\.|FR )[^\]\n]*\])", user, re.M)[:3] or re.findall(r"^(\[[^\]\n]+\])", user, re.M)[:1]
                text, wait = "This is a stand-in answer for a load test. " + " ".join(f"It relies on {c}." for c in cites), a.answer_s
            wait *= random.uniform(0.7, 1.3)
            usage = {"prompt_tokens": len(sys_msg + user) // 4, "completion_tokens": len(text) // 4, "total_tokens": 1}
            base = {"id": "stub", "object": "chat.completion", "created": 0, "model": body.get("model")}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                words = re.findall(r"\S+\s*", text)
                for w in words:
                    time.sleep(wait / max(1, len(words)))
                    d = {**base, "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
                    self.wfile.flush()
                d = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}
                self.wfile.write(f"data: {json.dumps(d)}\n\ndata: [DONE]\n\n".encode())
                self.close_connection = True
                return
            time.sleep(wait)
            out = json.dumps({**base, "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    print(f"stub model on :{a.port} fail={a.fail} grade={a.grade_s}s answer={a.answer_s}s", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), Stub).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--secret", help="X-Proxy-Secret, when talking to a container directly")
    ap.add_argument("--cookie", help="a signed-in session cookie, when going through the Worker")
    ap.add_argument("--app", choices=["bills", "procurement"], default="bills")
    ap.add_argument("--lane", choices=["member", "public"], default="member")
    ap.add_argument("--answer", choices=["fast", "thorough"])
    ap.add_argument("--levels", default="1,2,4,8", help="people at once, comma-separated")
    ap.add_argument("--rounds", type=int, default=2, help="questions per person")
    ap.add_argument("--think", type=float, default=2, help="seconds a person waits between questions")
    ap.add_argument("--rest", type=float, default=5, help="seconds between levels")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--docker", help="container name, to sample `docker stats`")
    ap.add_argument("--stats", action=argparse.BooleanOptionalAction, default=True, help="poll /api/stats (off for visitors, who may not read it)")
    ap.add_argument("--out", help="append the result rows to this JSONL file")
    ap.add_argument("--label", default="")
    ap.add_argument("--stub-model", action="store_true")
    ap.add_argument("--port", type=int, default=9099)
    ap.add_argument("--grade-s", type=float, default=2.5)
    ap.add_argument("--answer-s", type=float, default=3.0)
    ap.add_argument("--fail", type=int, help="stub: answer every call with this HTTP status")
    a = ap.parse_args()
    a.url = a.url.rstrip("/")
    stub_model(a) if a.stub_model else client(a)


if __name__ == "__main__":
    main()
