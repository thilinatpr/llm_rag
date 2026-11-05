"""Minimal web UI (stdlib only): python -m billrag serve [--host H] [--port P] [--mode pipeline|agent]

GET  /            single-page chat UI
POST /api/ask     {"session": str, "question": str} -> {"answer": str}   (needs a chat model/API key)
POST /api/search  {"query": str, "k": int}          -> {"results": [str]} (retrieval only, no key needed)
"""
import json
import threading
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_SESSIONS = 50

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sri Lanka Bills Chat</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#1a1a1a; --mut:#6b6b6b; --line:#ddd; --me:#e8f0fe; --bot:#f5f5f5; --acc:#1a56db; }
  @media (prefers-color-scheme: dark) { :root { --bg:#121212; --fg:#eee; --mut:#999; --line:#333; --me:#1e2a44; --bot:#1e1e1e; --acc:#6ea0ff; } }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui,sans-serif; background:var(--bg); color:var(--fg); display:flex; flex-direction:column; height:100dvh; }
  header { padding:12px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:12px; }
  header h1 { font-size:1rem; margin:0; }
  label { font-size:.85rem; color:var(--mut); }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:12px; }
  .msg { max-width:46rem; padding:10px 14px; border-radius:10px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .me { background:var(--me); align-self:flex-end; }
  .bot { background:var(--bot); align-self:flex-start; }
  .err { color:#d33; }
  form { display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--line); }
  input[type=text] { flex:1; padding:10px 12px; font:inherit; border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--fg); }
  button { padding:10px 16px; font:inherit; border:0; border-radius:8px; background:var(--acc); color:#fff; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .hint { color:var(--mut); font-size:.9rem; }
</style></head>
<body>
<header>
  <h1>Sri Lanka Bills Chat</h1>
  <label><input type="checkbox" id="searchOnly"> Search only (no LLM)</label>
</header>
<div id="log"><div class="hint">Ask about bills from documents.gov.lk, e.g. "How does the 2026 terrorism bill limit detention?".
Answers explain the text and are not legal advice.</div></div>
<form id="f"><input type="text" id="q" placeholder="Ask a question..." autocomplete="off" autofocus>
<button id="send">Send</button></form>
<script>
const log = document.getElementById('log'), q = document.getElementById('q'),
      btn = document.getElementById('send'), so = document.getElementById('searchOnly');
let session = null;
try { session = localStorage.getItem('billrag-session'); } catch (e) {}
if (!session) {
  session = Math.random().toString(36).slice(2);
  try { localStorage.setItem('billrag-session', session); } catch (e) {}
}
function add(text, cls) {
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = q.value.trim(); if (!text) return;
  q.value = ''; btn.disabled = true; add(text, 'me');
  const wait = add(so.checked ? 'Searching...' : 'Thinking...', 'bot');
  try {
    const r = await fetch(so.checked ? '/api/search' : '/api/ask', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(so.checked ? {query: text, k: 6} : {session, question: text})
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    wait.textContent = so.checked ? j.results.join('\\n\\n---\\n\\n') : j.answer;
  } catch (err) { wait.textContent = 'Error: ' + err.message; wait.classList.add('err'); }
  btn.disabled = false; q.focus(); log.scrollTop = log.scrollHeight;
});
</script></body></html>
"""


class App:
    def __init__(self, settings, mode="pipeline"):
        from .store import Store
        self.s, self.mode = settings, mode
        self.store = Store(settings.db_path)
        self.lock = threading.Lock()
        self.sessions = OrderedDict()  # session id -> responder; capped so a small container's memory can't grow without bound
        self._llm = None
        self._search = None

    def search(self, query, k=6):
        from .agent import fmt_passage
        from .llm import make_embedder, make_reranker
        from .retrieve import Retriever
        with self.lock:
            if self._search is None:
                self._search = Retriever(self.store, make_embedder(self.s), self.s.vectors_path, None,
                                         self.s.rerank, make_reranker(self.s))
            return [fmt_passage(c, 700) for c in self._search.search(query, k)]

    def _new_responder(self):
        """One conversation: pipeline (default: retrieve -> evidence grading -> summary) or the free-form agent."""
        from .llm import make_embedder, make_llm, make_reranker
        from .retrieve import Retriever
        if self._llm is None:
            self._llm = make_llm(self.s)
        r = Retriever(self.store, make_embedder(self.s), self.s.vectors_path, self._llm,
                      self.s.rerank, make_reranker(self.s))
        if self.mode == "agent":
            from .agent import Agent
            return Agent(self.store, r, self._llm).ask
        from .pipeline import Pipeline
        pipe = Pipeline(self.store, r, self._llm, grade_effort=self.s.grade_effort, answer_effort=self.s.answer_effort)

        def answer(q):
            res = pipe.ask(q)
            out = res.answer
            if res.warnings:
                out += "\n\n\u26a0 " + "\n\u26a0 ".join(res.warnings)
            if res.evidence:
                out += "\n\nSources:\n" + "\n".join("  " + x for x in res.sources())
            return out
        return answer

    def ask(self, session, question):
        with self.lock:
            if session not in self.sessions:
                self.sessions[session] = self._new_responder()
                while len(self.sessions) > MAX_SESSIONS:
                    self.sessions.popitem(last=False)
            self.sessions.move_to_end(session)
            responder = self.sessions[session]
            return responder(question)


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html")
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
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
    app = App(settings, mode)
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    print(f"Serving on http://{host}:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
