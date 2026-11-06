"""HTTP server for the Procurement Rules container (stdlib only): python -m procurement.serve

GET  /healthz    {"ok": true}
POST /api/ask    {"question": str} -> {"answer": str, "sources": [str], "warnings": [str]}

Every request must carry X-Proxy-Secret (only the login Worker has it). The container holds no Cloudflare token: reranking goes
through the Worker's Workers AI binding (PROCUREMENT_RERANK_URL). Each question is standalone. One JSON line per question goes to
stdout (container logs): questions are private, so read those logs only to debug.
"""
import argparse
import hmac
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_QUESTION = 1000
MAX_BODY = 16_000


class WorkerReranker:
    """index.Index's reranker, through the Worker. Failures raise: Index.search then keeps the BM25 order."""

    def __init__(self, url, secret, max_chars=1500, timeout=30):
        self.url, self.secret, self.max_chars, self.timeout = url, secret, max_chars, timeout

    def scores(self, query, texts):
        body = json.dumps({"query": query, "contexts": [{"text": (t or " ")[: self.max_chars]} for t in texts]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "X-Proxy-Secret": self.secret})
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


class App:
    def __init__(self, index, llm, secret):
        self.index, self.llm, self.secret = index, llm, secret
        self.lock = threading.Lock()  # one question at a time; /healthz still answers meanwhile

    def ask(self, question):
        from .ask import ask
        with self.lock:
            r = ask(self.index, self.llm, question)
        print(json.dumps({"event": "ask", "question": question, "answer": r.answer, "warnings": r.warnings, "seconds": r.seconds,
                          "sources": r.sources()}, ensure_ascii=False), flush=True)
        return {"answer": r.answer, "sources": r.sources(), "warnings": r.warnings}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
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
                question = str(json.loads(self.rfile.read(n) or b"{}").get("question", "")).strip()
                if not question:
                    return self.send(400, {"error": "empty question"})
                if len(question) > MAX_QUESTION:
                    return self.send(413, {"error": f"keep the question under {MAX_QUESTION} characters"})
                self.send(200, app.ask(question))
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
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(App(index, make_llm(load_settings()), secret)))
    print(f"procurement server on {a.host}:{a.port}, rerank={'on' if url else 'off'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
