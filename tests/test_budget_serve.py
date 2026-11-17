"""Budget container server: auth, health, one real HTTP ask with a scripted model, one SQLite connection per thread. Offline."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from budget import ask as askmod
from budget import index, serve

SECRET = "x" * 32


class Scripted:
    model = "scripted"

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None, on_token=None):
        from billrag.llm import Reply
        return Reply(text="Rs. 78,439,000,000 is the capital allocation [Bill Head 118].")


@pytest.fixture()
def server(tmp_path):
    path = tmp_path / "index.sqlite"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(index.SCHEMA)
    index.add_chunk(db, askmod.BILL, "estimate", "head 118", "Head 118 Minister of Agriculture", "23",
                    "Head 118: Minister of Agriculture. Programme 02 Development Activities: recurrent Rs. 55,587,000,000, capital Rs. 78,439,000,000.")
    db.commit()
    db.close()
    app = serve.App(str(path), Scripted(), SECRET)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def call(url, path, body=None, secret=SECRET):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(), method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json", **({"X-Proxy-Secret": secret} if secret else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e), dict(e.headers)


def test_every_request_needs_the_proxy_secret(server):
    _, url = server
    assert call(url, "/healthz", secret=None)[0] == 403
    assert call(url, "/api/ask", {"question": "q"}, secret="wrong")[0] == 403
    assert call(url, "/healthz")[0] == 200


def test_health_says_which_build_answers(server):
    app, url = server
    _, body, _ = call(url, "/healthz")
    assert body == {"ok": True, "build": app.build, "chunks": 1} and len(app.build) == 8


def test_ask_over_http_with_trace_header(server):
    app, url = server
    code, body, headers = call(url, "/api/ask", {"question": "Head 118 capital allocation"})
    assert code == 200 and body["warnings"] == [] and "[Bill Head 118]" in body["answer"]
    assert body["sources"] and body["sources"][0].startswith("[Bill Head 118]")
    trace = json.loads(headers["X-Ask-Trace"])
    assert trace["build"] == app.build and trace["evidence"] == ["[Bill Head 118]"] and trace["model"] == "scripted"
    assert "history" not in body and "standalone" not in body    # every question stands alone


def test_bad_questions(server):
    _, url = server
    assert call(url, "/api/ask", {"question": "   "})[0] == 400
    assert call(url, "/api/ask", {"question": "x" * 601})[0] == 413
    assert call(url, "/nope", {"question": "q"})[0] == 404


def test_one_sqlite_connection_per_thread(server):
    """Regression for the shared-connection bug (Bill Assistant, 2026-11-07): threads must not share one connection."""
    app, _ = server
    seen = {}

    def grab(name):
        seen[name] = app.db()

    ts = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len({id(c) for c in seen.values()}) == 3
    assert app.db() is app.db()                          # but one thread keeps its own
    with pytest.raises(sqlite3.OperationalError):          # and the baked index is opened read-only
        app.db().execute("INSERT INTO docs VALUES('a','b','c','d','e','f')")


def test_parallel_questions_all_answer(server):
    _, url = server
    out = []
    ts = [threading.Thread(target=lambda: out.append(call(url, "/api/ask", {"question": "Head 118 capital"})[0])) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out == [200] * 6


def test_build_id_changes_with_the_index(tmp_path):
    a, b = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    a.write_bytes(b"1"), b.write_bytes(b"22")
    assert serve.build_id(str(a)) != serve.build_id(str(b))
