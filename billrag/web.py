"""Minimal web UI (stdlib only): python -m billrag serve [--host H] [--port P] [--mode pipeline|agent]

Set BILLRAG_PROXY_SECRET to require an `X-Proxy-Secret` header on every request (the login Worker adds it); serving on
a non-loopback host without it is refused.

GET  /            public/index.html when present (local use); in production the Worker serves it
GET  /healthz     {"ok": true, "coverage": {bills, first, last, recent[]}} (the page polls it while a sleeping container starts)
GET  /api/stats   CPU/memory snapshot (billrag/resources.py) + sessions, turns, build; behind the login like all /api/*
POST /api/ask     {"session": str, "question": str, "answer"?: "fast"|"thorough"} -> {"answer": str}   (needs a chat model)
POST /api/ask/stream  same body -> text/event-stream: `status` {step}, `token` {text}, `reset` {}, then `done` {answer}
                  (the checked final answer, which replaces the streamed draft) or `error` {error}. `unanswered` {} before `done`
                  (header X-Unanswered on /api/ask) = the reply is only an apology: the Worker gives a visitor's question back.
POST /api/search  {"query": str, "k": int}          -> {"results": [str]} (retrieval only, no key needed)

Concurrency (billrag/gate.py): up to BILLRAG_MAX_CONCURRENT (default 8) questions run at once; up to BILLRAG_MAX_QUEUE (20) wait
in arrival order (a streamed reply gets `queue` {position} events), each at most BILLRAG_QUEUE_WAIT seconds (90). Beyond that the
reply is HTTP 503 + Retry-After (a streamed one: an `error` event). One conversation runs one question at a time.
Anonymous visitors arrive with `X-Lane: public` (set by the Worker only): they get BILLRAG_PUBLIC_SLOTS (2) of the slots and a
BILLRAG_PUBLIC_QUEUE (6) long queue of their own, so they cannot crowd out signed-in users.
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

from . import calltrace
from .chatlog import Timer, build_id, log_turn
from .gate import Busy, Gate
from .resources import Usage, snapshot

MAX_SESSIONS = 50
_INIT = threading.Lock()
_TL = threading.local()  # per request thread: did the turn just answered cost us nothing (free_turn)? read by the HTTP handler
# The page's "Answer" choice. Only these names are accepted from the browser; they map to the answer step's reasoning effort:
# fast = "low" (first word ~1-7 s instead of ~3-27 s, measured 2026-11-06), thorough = the server default (BILLRAG_ANSWER_EFFORT,
# unset = the model's own default). Grading and translation are not affected.
ANSWER_MODES = ("fast", "thorough")

PUBLIC_ERROR = "The answer could not be produced just now. Please try again."  # visitors never see provider errors; the log has them


def unanswered(text):
    """The model failed twice and the reply is only an apology (Pipeline._llm), so a visitor should not pay for it."""
    from .pipeline import UNUSABLE
    return UNUSABLE in (text or "")


def free_turn(res):
    """A turn that used no search and no model call of ours (greeting, "which bill?", a translation of the last answer): the Worker does not
    count it against a free account's allowance. A failed answer is given back there too (the `unanswered` event)."""
    return bool(res.clarify or res.path == "language_switch" or (not res.evidence and not res.llm_calls and not res.queries))


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

    def _ensure(self):
        """Gate and per-conversation locks, created on first use (tests build App without __init__)."""
        with _INIT:
            if getattr(self, "_gate", None) is None:
                env = os.environ.get
                self._gate = Gate(int(env("BILLRAG_MAX_CONCURRENT") or 8), int(env("BILLRAG_MAX_QUEUE") or 20),
                                  float(env("BILLRAG_QUEUE_WAIT") or 90), int(env("BILLRAG_PUBLIC_SLOTS") or 2),
                                  int(env("BILLRAG_PUBLIC_QUEUE") or 6))
                self._slocks = {}

    def _session_lock(self, session):
        self._ensure()
        with self.lock:
            return self._slocks.setdefault(session, threading.Lock())

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
        self._ensure()
        self._gate.acquire()
        try:
            with Timer() as tm, Usage() as use, calltrace.tracing() as trace:
                with self.lock:
                    retriever = self._retriever()
                out = [fmt_passage(c, 700) for c in retriever.search(query, k)]
        finally:
            self._gate.release()
        log_turn(type="search", question=query, k=k, results=len(out), seconds=tm.seconds, usage=use.fields(),
                 calls=trace.calls or None, cost=calltrace.cost_summary(trace.calls))
        return out

    def coverage(self):
        """GET /healthz -> `coverage`: computed once per process (the index does not change while the server runs)."""
        c = getattr(self, "_coverage", None)
        if c is None:
            c = self._coverage = self.store.coverage()
        return c

    def stats(self):
        """GET /api/stats: resource snapshot plus what this server holds. Does not take the lock (answers mid-question)."""
        self._ensure()
        out = snapshot()
        gate = self._gate.counts()
        out.update(build=build_id(), mode=self.mode, sessions=len(self.sessions), max_sessions=MAX_SESSIONS,
                   turns=sum(self.turns.values()), busy=gate["running"] > 0, retriever_loaded=self._retr is not None, **gate)
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
            return lambda q, emit=None, mode=None: (agent.ask(q), {"mode": "agent"})  # no streaming or answer modes
        from .pipeline import Pipeline
        from .translate import make_translator
        events = []
        pipe = Pipeline(self.store, r, self._llm, grade_effort=self.s.grade_effort, answer_effort=self.s.answer_effort,
                        answer_llm=make_answer_llm(self.s), translate_mode=getattr(self.s, "translate", "script"),
                        translator=make_translator(self.s, self._llm),
                        on_event=lambda name, data: events.append({"event": name, **(data if isinstance(data, dict) else {"data": data})}))

        def answer(q, emit=None, mode=None):
            events.clear()
            # "fast" only when the server sends reasoning effort at all (BILLRAG_GRADE_EFFORT empty = model without it)
            effort = "low" if mode == "fast" and self.s.grade_effort else self.s.answer_effort
            pipe.answer_effort, pipe.emit = effort, emit
            try:
                res = pipe.ask(q)
            finally:
                pipe.emit = None
            out = res.answer
            if res.warnings:
                out += "\n\n\u26a0 " + "\n\u26a0 ".join(res.warnings)
            from .translate import machine_note
            note = machine_note(res.lang, res.path, res.answer)
            if note:
                out += "\n\n*" + note + "*"
            if res.base_note:  # fixed: an Act as originally enacted, later amendments not included
                out += "\n\n*" + res.base_note + "*"
            if res.scan_note:  # fixed, not left to the model: the text came from a scan (OCR + manual review)
                out += "\n\n*" + res.scan_note + "*"
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
                "glossary_hints": res.glossary_hints or None, "glossary_terms": res.glossary_terms or None,
                "added_sections": [f"{e.key} \u00a7{e.section}" for e in res.added_sections] or None,
                "acts": res.acts or None, "amend_list": res.amend_list or None,
                "direct_rejected": res.direct_rejected or None,
                "transcribed": sorted(res.transcribed) or None, "base_note": bool(res.base_note) or None,
                "answer_mode": mode or "default", "answer_effort": effort,
                "free": free_turn(res),
            }
            return out, meta
        return answer

    def ask(self, session, question, emit=None, mode=None, lane="member"):
        """emit(kind, data), optional: progress events for a streamed reply (see /api/ask/stream).
        mode: one of ANSWER_MODES (the page's Answer choice) or None for the server default.
        lane: "member" (signed in) or "public" (anonymous, marked by the Worker): see gate.py."""
        self._ensure()
        slock = self._session_lock(session)
        # one question per conversation at a time: a follow-up sent early waits for the previous answer (its history)
        if not slock.acquire(timeout=self._gate.max_wait):
            raise Busy("The previous question in this chat is still being answered. Please wait for it.")
        try:
            on_wait = (lambda pos: emit("queue", pos)) if emit else None
            waited, concurrent = self._gate.acquire(on_wait, lane)
            try:
                with self.lock:
                    if session not in self.sessions:
                        self.sessions[session] = self._new_responder()
                        while len(self.sessions) > MAX_SESSIONS:
                            # anonymous chats ("anon:<id>", named by the Worker) are evicted first, so public traffic
                            # cannot push a signed-in user's conversation out
                            old = next((k for k in self.sessions if k.startswith("anon:")), None) or next(iter(self.sessions))
                            del self.sessions[old]
                            self._slocks.pop(old, None)
                    self.sessions.move_to_end(session)
                    responder = self.sessions[session]
                    self.turns[session] = self.turns.get(session, 0) + 1
                    turn = self.turns[session]
                err, text, meta = None, None, {}
                with Timer() as tm, Usage() as use, calltrace.tracing() as trace:
                    try:
                        kw = {k: v for k, v in (("emit", emit), ("mode", mode)) if v}
                        text, meta = responder(question, **kw)
                        _TL.free = bool(meta.get("free"))
                        return text
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                        raise
                    finally:
                        # cpu_s/cpu_pct are process-wide: with `concurrent` > 1 they include the other running turns
                        log_turn(type="ask", session=session, turn=turn, question=question, answer=text, error=err,
                                 seconds=round(time.time() - tm.t, 2), usage=use.fields(), calls=trace.calls or None,
                                 cost=calltrace.cost_summary(trace.calls), fallback=calltrace.fallback_summary(trace.calls),
                                 queue_wait_s=round(waited, 2),
                                 concurrent=concurrent, lane=lane, **meta)
            finally:
                self._gate.release(lane)
        finally:
            slock.release()


def answer_mode(body):
    """The request's `answer` field if it is a known mode, else None (server default). Never passed through raw."""
    m = body.get("answer")
    return m if m in ANSWER_MODES else None


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json", headers=()):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            if code == 503:
                self.send_header("Retry-After", "30")
            for name, value in headers:
                self.send_header(name, value)
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
                out = {"ok": True}
                cov = getattr(app, "coverage", None)
                if cov:
                    try:
                        out["coverage"] = cov()  # what the index holds, for the page's coverage line (no secrets, no counts of chunks)
                    except Exception:
                        pass  # the health check must never fail because of this
                self._send(200, json.dumps(out))
            elif self.path == "/api/stats":
                self._send(200, json.dumps(app.stats()))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def _lane_kw(self):
            """Only the Worker (which holds the proxy secret) can set X-Lane; it marks anonymous visitors."""
            return {"lane": "public"} if self.headers.get("X-Lane") == "public" else {}

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
                if self.path == "/api/ask/stream":
                    q = str(body.get("question", "")).strip()
                    if not q:
                        return self._send(400, json.dumps({"error": "empty question"}))
                    return self._stream(str(body.get("session") or "default")[:64], q, answer_mode(body), self._lane_kw())
                if self.path == "/api/ask":
                    q = str(body.get("question", "")).strip()
                    if not q:
                        return self._send(400, json.dumps({"error": "empty question"}))
                    sid = str(body.get("session") or "default")[:64]
                    mode = answer_mode(body)
                    kw = self._lane_kw()
                    if mode:
                        kw["mode"] = mode
                    _TL.free = False
                    text = app.ask(sid, q, **kw)
                    flags = ([("X-Unanswered", "1")] if unanswered(text) else []) + ([("X-Free", "1")] if getattr(_TL, "free", False) else [])
                    return self._send(200, json.dumps({"answer": text}), headers=flags)
                self._send(404, json.dumps({"error": "not found"}))
            except Busy as e:
                self._send(503, json.dumps({"error": str(e), "busy": True}))
            except Exception as e:  # surface config problems (missing API key, stale vectors) to signed-in users, not to visitors
                self._send(500, json.dumps({"error": PUBLIC_ERROR if self._lane_kw() else f"{type(e).__name__}: {e}"}))

        def _stream(self, sid, question, mode=None, lane_kw=None):
            """Server-sent events over this response; the connection closes at the end (HTTP/1.0 handler)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            alive = [True]

            def send(kind, data):
                if not alive[0]:
                    return  # the browser went away: finish the answer anyway (it is logged), just stop writing
                try:
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    alive[0] = False

            try:
                emit = (lambda k, d: send(k, {"step": d} if k == "status" else {"text": d} if k == "token"
                                            else {"position": d} if k == "queue" else {}))
                kw = dict(lane_kw or {})
                if mode:
                    kw["mode"] = mode
                _TL.free = False
                text = app.ask(sid, question, emit=emit, **kw)
                if unanswered(text):
                    send("unanswered", {})
                elif getattr(_TL, "free", False):
                    send("free", {})
                send("done", {"answer": text})
            except Busy as e:
                send("error", {"error": str(e), "busy": True})
            except Exception as e:  # headers are sent already: report the error as an event
                send("error", {"error": PUBLIC_ERROR if lane_kw else f"{type(e).__name__}: {e}"})

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
