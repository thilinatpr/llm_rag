"""Minimal web UI (stdlib only): python -m billrag serve [--host H] [--port P] [--mode pipeline|agent]

Set BILLRAG_PROXY_SECRET to require an `X-Proxy-Secret` header on every request (the login Worker adds it); serving on
a non-loopback host without it is refused.

GET  /            public/index.html when present (local use); in production the Worker serves it
GET  /healthz     {"ok": true} (the page polls it while a sleeping container starts)
POST /api/ask     {"session": str, "question": str} -> {"answer": str}   (needs a chat model/API key)
POST /api/search  {"query": str, "k": int}          -> {"results": [str]} (retrieval only, no key needed)
"""
import hmac
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .chatlog import Timer, log_turn

MAX_SESSIONS = 50

def load_page():
    """The UI is a static file (public/index.html). In production the Worker serves it; this is for local runs."""
    f = Path(__file__).resolve().parent.parent / "public" / "index.html"
    return f.read_text(encoding="utf-8") if f.exists() else None




class App:
    def __init__(self, settings, mode="pipeline", secret=None):
        from .store import Store
        self.s, self.mode, self.secret = settings, mode, secret
        self.store = Store(settings.db_path)
        self.lock = threading.Lock()
        self.turns = {}  # session id -> number of questions so far (for the log)
        self.sessions = OrderedDict()  # session id -> responder; capped so a small container's memory can't grow without bound
        self._llm = None
        self._retr = None

    def _retriever(self):
        """One Retriever for everyone: it holds the vectors (~66 MB) and checks the index fingerprint, so build it once."""
        from .llm import make_embedder, make_llm, make_reranker
        from .retrieve import Retriever
        if self._retr is None:
            llm = None
            if self.s.rerank == "llm":
                self._llm = self._llm or make_llm(self.s)
                llm = self._llm
            self._retr = Retriever(self.store, make_embedder(self.s), self.s.vectors_path, llm,
                                   self.s.rerank, make_reranker(self.s))
        return self._retr

    def warm(self):
        """Load vectors and verify the index now, so the first user request isn't the one that pays for it."""
        with self.lock:
            self._retriever()

    def search(self, query, k=6):
        from .agent import fmt_passage
        with self.lock, Timer() as tm:
            out = [fmt_passage(c, 700) for c in self._retriever().search(query, k)]
        log_turn(type="search", question=query, k=k, results=len(out), seconds=tm.seconds)
        return out

    def _new_responder(self):
        """One conversation: pipeline (default: retrieve -> evidence grading -> summary) or the free-form agent."""
        from .llm import make_answer_llm, make_llm
        if self._llm is None:
            self._llm = make_llm(self.s)
        r = self._retriever()
        if self.mode == "agent":
            from .agent import Agent
            agent = Agent(self.store, r, self._llm)
            return lambda q: (agent.ask(q), {"mode": "agent"})
        from .pipeline import Pipeline
        from .translate import make_translator
        events = []
        pipe = Pipeline(self.store, r, self._llm, grade_effort=self.s.grade_effort, answer_effort=self.s.answer_effort,
                        answer_llm=make_answer_llm(self.s), translate_mode=getattr(self.s, "translate", "script"),
                        translator=make_translator(self.s, self._llm),
                        on_event=lambda name, data: events.append({"event": name, **(data if isinstance(data, dict) else {"data": data})}))

        def answer(q):
            events.clear()
            res = pipe.ask(q)
            out = res.answer
            if res.warnings:
                out += "\n\n\u26a0 " + "\n\u26a0 ".join(res.warnings)
            if res.evidence:
                out += "\n\nSources:\n" + "\n".join("  " + x for x in res.sources())
            meta = {
                "mode": "pipeline", "queries": res.queries, "enough": res.enough, "warnings": res.warnings,
                "evidence": [{"bill": e.key, "section": e.section, "score": e.score} for e in res.evidence],
                "candidates_graded": len(res.graded), "llm_calls": res.llm_calls,
                "step_seconds": {k: round(v, 2) for k, v in res.seconds.items()}, "events": list(events),
                "model": getattr(self.s, "chat_model", None), "lang": res.lang,
                "official": [f"{k} \u00a7{n}" for k, n in res.official],
                "path": res.path, "answer_en": res.answer_en or None, "direct_fallback": res.direct_fallback or None,
                "quote_fixes": res.quote_fixes or None, "focus": res.focus,
                "direct_rejected": res.direct_rejected or None,
            }
            return out, meta
        return answer

    def ask(self, session, question):
        with self.lock:
            if session not in self.sessions:
                self.sessions[session] = self._new_responder()
                while len(self.sessions) > MAX_SESSIONS:
                    self.sessions.popitem(last=False)
            self.sessions.move_to_end(session)
            responder = self.sessions[session]
            self.turns[session] = self.turns.get(session, 0) + 1
            turn = self.turns[session]
            err, text, meta = None, None, {}
            with Timer() as tm:
                try:
                    text, meta = responder(question)
                    return text
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    raise
                finally:
                    log_turn(type="ask", session=session, turn=turn, question=question, answer=text, error=err,
                             seconds=round(time.time() - tm.t, 2), **meta)


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self):
            """With BILLRAG_PROXY_SECRET set, only requests carrying it (i.e. via the login Worker) are served."""
            secret = getattr(app, "secret", None)
            if not secret:
                return True
            got = self.headers.get("X-Proxy-Secret", "")
            if hmac.compare_digest(got.encode(), secret.encode()):
                return True
            self._send(403, json.dumps({"error": "forbidden"}))
            return False

        def do_GET(self):
            if not self._allowed():
                return
            if self.path in ("/", "/index.html"):
                page = load_page()
                if page is None:
                    return self._send(404, json.dumps({"error": "UI is served by the Worker (public/index.html)."}))
                self._send(200, page, "text/html")
            elif self.path == "/healthz":
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if not self._allowed():
                return
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/search":
                    q = str(body.get("query", "")).strip()
                    if not q:
                        return self._send(400, json.dumps({"error": "empty query"}))
                    k = max(1, min(int(body.get("k", 6)), 15))
                    return self._send(200, json.dumps({"results": app.search(q, k)}))
                if self.path == "/api/ask":
                    q = str(body.get("question", "")).strip()
                    if not q:
                        return self._send(400, json.dumps({"error": "empty question"}))
                    sid = str(body.get("session") or "default")[:64]
                    return self._send(200, json.dumps({"answer": app.ask(sid, q)}))
                self._send(404, json.dumps({"error": "not found"}))
            except Exception as e:  # surface config problems (missing API key, stale vectors) in the UI
                self._send(500, json.dumps({"error": f"{type(e).__name__}: {e}"}))

        def log_message(self, *a):
            pass

    return H


def serve(settings, host="127.0.0.1", port=8000, mode="pipeline"):
    secret = os.environ.get("BILLRAG_PROXY_SECRET") or None
    if secret and len(secret) < 16:
        sys.exit("BILLRAG_PROXY_SECRET must be at least 16 characters.")
    if host not in ("127.0.0.1", "localhost", "::1") and not secret:
        sys.exit(f"Refusing to listen on {host} without BILLRAG_PROXY_SECRET: the UI has no login of its own.")
    app = App(settings, mode, secret)
    if os.environ.get("BILLRAG_WARM", "1") != "0":
        def _warm():
            try:
                app.warm()
            except Exception as e:  # surfaced again, with the real error, on the first request
                print(f"warm-up failed: {type(e).__name__}: {e}", file=sys.stderr)
        threading.Thread(target=_warm, daemon=True).start()
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Serving on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
