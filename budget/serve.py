"""HTTP server for the Budget container (stdlib only): python -m budget.serve

GET  /healthz    {"ok": true, "build": "...", "chunks": N}
POST /api/ask    {"question": str} -> {"answer": str, "sources": [str], "warnings": [str]}   (every question stands alone: no history)
                 Header X-Ask-Trace = {"build", "seconds", "model", "evidence", "calls", "cost", "fallback", "queue_wait_s", "concurrent"}:
                 the Worker moves it into the question's log record and removes it, so tokens and costs never reach the browser.

Every request must carry X-Proxy-Secret (only the login Worker has it). The container holds no Cloudflare token: the backup chat model, used when
DeepSeek fails, is the Worker's Workers AI binding (BUDGET_FALLBACK_URL). The index is baked into the image (public documents only) and opened
read-only, one SQLite connection per thread. One JSON line per question goes to stdout (container logs): questions are private.
BUDGET_MAX_CONCURRENT (2) questions run at once and BUDGET_MAX_QUEUE (6) wait, each at most BUDGET_QUEUE_WAIT seconds (60); beyond that 503.
"""
import argparse
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from billrag import calltrace
from billrag.gate import Busy, Gate
from billrag.llm import FallbackLLM, Reply

from . import index as ix

MAX_QUESTION = 600
MAX_BODY = 8_000
USER_AGENT = "citizenlk-budget/1.0"  # Cloudflare blocks Python's default User-Agent (error 1010) before the Worker sees it
INDEX = "data/budget/index.sqlite"


def build_id(index_path=INDEX) -> str:
    """Hash of the running code and the index it answers from: the `build` field of every log record says which container answered."""
    h = hashlib.sha1()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        if p.name not in ("evaluate.py", "__main__.py"):
            h.update(p.read_bytes())
    try:
        h.update(str(os.path.getsize(index_path)).encode())
    except OSError:
        pass
    return h.hexdigest()[:8]


class WorkerChat:
    """The backup chat model, through the Worker's Workers AI binding (this container holds no Cloudflare token)."""

    def __init__(self, url, secret, model="@cf/openai/gpt-oss-120b", timeout=120):
        self.url, self.secret, self.model, self.timeout = url, secret, model, timeout

    def chat(self, system, messages, tools=(), max_tokens=2048, effort=None, on_token=None):
        body = json.dumps({"system": system, "messages": messages, "max_tokens": max_tokens, "effort": effort}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Proxy-Secret": self.secret, "User-Agent": USER_AGENT})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.load(r)
        except Exception as e:
            calltrace.record("chat", time.time() - t, model=self.model, error=type(e).__name__, max_tokens=max_tokens, effort=effort)
            raise
        calltrace.record("chat", time.time() - t, model=self.model, stop=out.get("stop") or None, max_tokens=max_tokens, effort=effort,
                         **calltrace.usage_fields(out.get("usage")))
        return Reply(out.get("text") or "", [], out.get("stop") or "")


class App:
    def __init__(self, index_path, llm, secret, gate=None):
        self.index_path, self.llm, self.secret = index_path, llm, secret
        self.local = threading.local()  # a SQLite connection is not shared between threads (Procurement/Bill Assistant lesson)
        self.build = build_id(index_path)
        env = os.environ.get
        self.gate = gate or Gate(int(env("BUDGET_MAX_CONCURRENT") or 2), int(env("BUDGET_MAX_QUEUE") or 6), float(env("BUDGET_QUEUE_WAIT") or 60), 0, 0)

    def db(self):
        c = getattr(self.local, "db", None)
        if c is None:
            c = self.local.db = ix.connect(self.index_path, readonly=True)
        return c

    def chunks(self) -> int:
        return self.db().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def ask(self, question):
        """-> (reply for the browser, trace for the Worker's log)."""
        from .ask import ask
        waited, concurrent = self.gate.acquire()  # Busy -> 503; /healthz still answers meanwhile
        try:
            with calltrace.tracing() as trace:
                r = ask(self.db(), self.llm, question)
        finally:
            self.gate.release()
        info = {"build": self.build, "seconds": r.seconds, "model": getattr(self.llm, "model", None), "evidence": [u["label"] for u in r.evidence],
                "calls": trace.calls or None, "cost": calltrace.cost_summary(trace.calls), "fallback": calltrace.fallback_summary(trace.calls),
                "queue_wait_s": round(waited, 2), "concurrent": concurrent}
        print(json.dumps({"event": "ask", "question": question, "answer": r.answer, "warnings": r.warnings, "sources": r.sources(), **info}, ensure_ascii=False), flush=True)
        return {"answer": r.answer, "sources": r.sources(), "warnings": r.warnings}, info


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj, trace=None):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if code == 503:
                self.send_header("Retry-After", "30")
            if trace:
                self.send_header("X-Ask-Trace", json.dumps(trace))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            if hmac.compare_digest(self.headers.get("X-Proxy-Secret", "").encode(), app.secret.encode()):
                return True
            self.send(403, {"error": "forbidden"})
            return False

        def do_GET(self):
            if not self.allowed():
                return
            if self.path == "/healthz":
                return self.send(200, {"ok": True, "build": app.build, "chunks": app.chunks()})
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.allowed():
                return
            if self.path != "/api/ask":
                return self.send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > MAX_BODY:
                    return self.send(413, {"error": "question too long"})
                body = json.loads(self.rfile.read(n) or b"{}")
                question = str(body.get("question", "")).strip()
                if not question:
                    return self.send(400, {"error": "empty question"})
                if len(question) > MAX_QUESTION:
                    return self.send(413, {"error": f"keep the question under {MAX_QUESTION} characters"})
                reply, trace = app.ask(question)
                self.send(200, reply, trace)
            except Busy as e:
                self.send(503, {"error": str(e), "busy": True})
            except Exception as e:
                print(json.dumps({"event": "error", "error": f"{type(e).__name__}: {e}"}), flush=True)
                self.send(500, {"error": "The answer could not be produced. Please try again."})

    return Handler


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--index", default=os.environ.get("BUDGET_INDEX") or INDEX)
    a = ap.parse_args(argv)
    secret = os.environ.get("BUDGET_PROXY_SECRET", "")
    if len(secret) < 16:
        sys.exit("BUDGET_PROXY_SECRET (>= 16 characters) is required")
    if not os.path.exists(a.index):
        sys.exit(f"index not found: {a.index} (python -m budget build, or scripts/budget_index.py fetch)")
    from billrag.config import load_settings
    from billrag.llm import make_llm
    llm = make_llm(load_settings())
    backup = os.environ.get("BUDGET_FALLBACK_URL")
    if backup:  # fail over quickly: see billrag.llm.make_llm
        llm.client = llm.client.with_options(max_retries=1, timeout=60)
        llm = FallbackLLM(llm, WorkerChat(backup, secret))
    app = App(a.index, llm, secret)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    print(f"budget server on {a.host}:{a.port}, build={app.build}, chunks={app.chunks()}, fallback={'on' if backup else 'off'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
