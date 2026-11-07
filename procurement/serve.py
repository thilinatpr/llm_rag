"""HTTP server for the Procurement Rules container (stdlib only): python -m procurement.serve

GET  /healthz    {"ok": true}
POST /api/ask    {"question": str, "history": [{"question": str, "answer": str}]} -> {"answer": str, "sources": [str], "warnings": [str],
                 "standalone": str}   (history is optional: the page sends its last turns; `standalone` = how a follow-up was read)
                 Header X-Ask-Trace = {"seconds", "model", "calls", "cost", "fallback", "queue_wait_s", "concurrent"}: the Worker moves it into
                 the question's log record and removes it, so tokens and costs do not reach the browser.

Every request must carry X-Proxy-Secret (only the login Worker has it). The container holds no Cloudflare token: reranking goes
through the Worker's Workers AI binding (PROCUREMENT_RERANK_URL), and so does the backup chat model when DeepSeek fails
(PROCUREMENT_FALLBACK_URL). The server keeps no conversation state. One JSON line per question goes to stdout (container logs): questions are
private, so read those logs only to debug.

PROCUREMENT_MAX_CONCURRENT (2) questions run at once and PROCUREMENT_MAX_QUEUE (6) wait, each at most PROCUREMENT_QUEUE_WAIT seconds (60); beyond
that the reply is 503. Most of a question is spent waiting on the model, so they can overlap even on 1/16 vCPU (see AGENTS.md "Load test").
"""
import argparse
import hmac
import json
import os
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from billrag import calltrace
from billrag.gate import Busy, Gate
from billrag.llm import FallbackLLM, Reply

MAX_QUESTION = 1000
MAX_BODY = 16_000
USER_AGENT = "citizenlk-procurement/1.0"  # Cloudflare blocks Python's default User-Agent (error 1010) before the Worker sees it


class WorkerReranker:
    """index.Index's reranker, through the Worker. Failures raise: Index.search then keeps the BM25 order."""

    def __init__(self, url, secret, max_chars=1500, timeout=30):
        self.url, self.secret, self.max_chars, self.timeout = url, secret, max_chars, timeout

    def scores(self, query, texts):
        body = json.dumps({"query": query, "contexts": [{"text": (t or " ")[: self.max_chars]} for t in texts]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Proxy-Secret": self.secret,
                                              "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                items = json.load(r)["response"]
        except Exception as e:
            print(json.dumps({"event": "rerank_failed", "error": f"{type(e).__name__}: {e}"}), flush=True)
            raise
        out = [0.0] * len(texts)
        for item in items:
            out[item["id"]] = float(item["score"])
        return out


class WorkerChat:
    """The backup chat model, through the Worker's Workers AI binding (this container holds no Cloudflare token)."""

    def __init__(self, url, secret, model="@cf/openai/gpt-oss-120b", timeout=120):
        self.url, self.secret, self.model, self.timeout = url, secret, model, timeout

    def chat(self, system, messages, tools=(), max_tokens=2048, effort=None, on_token=None):
        body = json.dumps({"system": system, "messages": messages, "max_tokens": max_tokens, "effort": effort}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Proxy-Secret": self.secret,
                                              "User-Agent": USER_AGENT})
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
    def __init__(self, index, llm, secret, gate=None):
        self.index, self.llm, self.secret = index, llm, secret
        env = os.environ.get
        self.gate = gate or Gate(int(env("PROCUREMENT_MAX_CONCURRENT") or 2), int(env("PROCUREMENT_MAX_QUEUE") or 6),
                                 float(env("PROCUREMENT_QUEUE_WAIT") or 60), 0, 0)

    def ask(self, question, history=None):
        """-> (reply for the browser, trace for the Worker's log)."""
        from .ask import ask
        waited, concurrent = self.gate.acquire()  # Busy -> 503; /healthz still answers meanwhile
        try:
            with calltrace.tracing() as trace:
                r = ask(self.index, self.llm, question, history=history)
        finally:
            self.gate.release()
        info = {"seconds": r.seconds, "model": getattr(self.llm, "model", None), "calls": trace.calls or None,
                "cost": calltrace.cost_summary(trace.calls), "fallback": calltrace.fallback_summary(trace.calls),
                "queue_wait_s": round(waited, 2), "concurrent": concurrent}
        print(json.dumps({"event": "ask", "question": question, "standalone": r.standalone, "answer": r.answer, "warnings": r.warnings,
                          "sources": r.sources(), **info}, ensure_ascii=False), flush=True)
        return {"answer": r.answer, "sources": r.sources(), "warnings": r.warnings, "standalone": r.standalone}, info


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
            self.send(200, {"ok": True}) if self.path == "/healthz" else self.send(404, {"error": "not found"})

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
                reply, trace = app.ask(question, body.get("history"))
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
    a = ap.parse_args(argv)
    secret = os.environ.get("PROCUREMENT_PROXY_SECRET", "")
    if len(secret) < 16:
        sys.exit("PROCUREMENT_PROXY_SECRET (>= 16 characters) is required")
    from billrag.config import load_settings
    from billrag.llm import make_llm
    from .index import Index
    url = os.environ.get("PROCUREMENT_RERANK_URL")
    index = Index(reranker=WorkerReranker(url, secret) if url else None)
    llm = make_llm(load_settings())
    backup = os.environ.get("PROCUREMENT_FALLBACK_URL")
    if backup:  # fail over quickly: see billrag.llm.make_llm
        llm.client = llm.client.with_options(max_retries=1, timeout=60)
        llm = FallbackLLM(llm, WorkerChat(backup, secret))
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(App(index, llm, secret)))
    print(f"procurement server on {a.host}:{a.port}, rerank={'on' if url else 'off'}, fallback={'on' if backup else 'off'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
