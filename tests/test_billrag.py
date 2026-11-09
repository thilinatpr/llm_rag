import json
import os
import threading

import pytest

# Chat logs are written on background threads and uploaded to R2 whenever R2_* variables are set, and the cloud sandbox has
# real ones: on 2026-11-06 test runs put ~25 fake records ("hi", session s1) into the production chatlogs/. Remove them for the
# whole test process (a per-test monkeypatch is undone before those threads run). Tests that need R2 stub chatlog._r2.
for _k in ("R2_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
    os.environ.pop(_k, None)

from billrag.agent import Agent, TOOLS
from billrag.ingest import build_chunks, classify
ROOT_FOR_SCRIPTS = __import__("pathlib").Path(__file__).resolve().parent.parent
from billrag.llm import Reply, ToolCall, to_anthropic_messages, to_openai_messages
from billrag.parse import match_section, parse_cover, parse_pages, split_text
from billrag.retrieve import Retriever, rrf
from billrag.store import Store, fts_query, normalize_ref

COVER = """TRUSTS (AMENDMENT)
A
BILL
to amend the Trusts Ordinance
(Chapter 87)
————————
Presented by the Minister of Justice
 on 06th of October, 2026
(Published in the Gazette on September 28, 2026)
[Bill No. 82]"""


def body(extra=""):
    return f"""2
Trusts (Amendment)
PL 15965 - 337 (10/2026)
L.D.- O 29/2026
An Act to amend the Trusts Ordinance.
BE it enacted by the Parliament of the Democratic Socialist
Republic of Sri Lanka as follows:-
1.1. This Act may be cited as the Trusts
(Amendment) Act, No.     of 2026.
PART I
GENERAL PROVISIONS
22. Section 5 of the Trusts Ordinance is amended by the
substitution of the words "fifteen days" for "ten days".
(1) A trustee shall keep accounts{extra}
5
10
Short title
Amendment of
section 5
"""


def make_store(tmp_path, with_gs=True):
    st = Store(tmp_path / "i.sqlite")
    specs = [("086/2026", 86, "Trusts (Amendment) - Bill", "Bill", "2026-10-21", body())]
    if with_gs:
        specs.append(("081/2026", 81, "Trusts (Amendment) - GS", "GS", "2026-10-14",
                      body().replace("fifteen days", "ten days")))
    for key, no, title, kind, date, pg in specs:
        parsed = parse_pages([COVER, pg], title)
        _, tnorm = classify(title)
        row = {"key": key, "bill_no": no, "year": 2026, "date": date + "T00:00:00Z", "title": title,
               "title_norm": tnorm, "kind": kind, "long_title": parsed.cover.get("long_title"),
               "presented_by": None, "presented_on": None, "gazette_date": None, "printed_no": None,
               "n_pages": 2, "n_sections": 2, "pdf_path": "x", "summary": None}
        st.replace_bill(row, build_chunks(row, parsed))
    st.commit()
    return st


# ---------------------------------------------------------------- parsing
def test_cover_regular_and_gs():
    c = parse_cover(COVER)
    assert c["title"] == "TRUSTS (AMENDMENT)"
    assert c["long_title"] == "to amend the Trusts Ordinance (Chapter 87)"
    assert c["presented_by"] == "the Minister of Justice" and c["printed_no"] == "82"
    gs = parse_cover("PRINTED AT THE DEPARTMENT\nA\nBILL\nto amend the Animals Act\nANIMALS (AMENDMENT)\n"
                     "Part II of June 05, 2026\nOrdered to be published by the Minister of Agriculture,\n Lands\n")
    assert gs["long_title"] == "to amend the Animals Act" and gs["title"] == "ANIMALS (AMENDMENT)"
    assert gs["gazette_date"] == "June 05, 2026"


def test_match_section_handles_doubled_numbers():
    assert match_section("1.1. This Act may", 1) == "This Act may"
    assert match_section("33. In the event", 3) == "In the event"
    assert match_section("12. Section 4", 12) == "Section 4"
    assert match_section("12. section lowercase", 12) == "section lowercase"  # lower-cased bills
    assert match_section("12. 5 per cent", 12) is None
    assert match_section("2 (1) Income tax", 2) == "(1) Income tax"  # old style, no full stop
    assert match_section("13 . (1) No person", 13) == "(1) No person"  # stray space before the full stop
    assert match_section("3. Text", 4) is None


def test_parse_pages_structure():
    p = parse_pages([COVER, body()], "Trusts (Amendment) - Bill")
    secs = [s for s in p.sections if s.kind == "section"]
    assert [s.no for s in secs] == [1, 2]
    assert "as follows" not in secs[0].text and secs[0].text.startswith("This Act may be cited")
    assert secs[1].part == "PART I — GENERAL PROVISIONS"
    assert "fifteen days" in secs[1].text and "(1) A trustee" in secs[1].text
    assert "PL 15965" not in secs[1].text and "Trusts (Amendment)" not in secs[0].text.split("Act")[0]
    assert "Short title" not in secs[1].text  # margin notes split off


def test_split_text_packs_and_windows():
    assert split_text("short") == ["short"]
    long_ = "\n".join(f"({i}) " + "word " * 80 for i in range(1, 8))
    parts = split_text(long_, max_chars=900)
    assert all(len(x) <= 900 for x in parts) and len(parts) > 2
    assert all(len(x) <= 900 for x in split_text("x " * 2000, max_chars=900))


# ---------------------------------------------------------------- store / retrieval
def test_refs_and_fts_query():
    assert normalize_ref("77/2026") == "077/2026" and normalize_ref("bill 077 of 2026") == "077/2026"
    assert normalize_ref("Sri Lanka Cricket") is None
    assert fts_query("What is the penalty for the tax?") == '"penalty" OR "tax"'


def test_bm25_filters(tmp_path):
    st = make_store(tmp_path)
    assert st.bm25("fifteen")  # only the Bill version says fifteen
    assert all(st.chunks_by_ids([i])[i]["bill_key"] == "086/2026" for i in st.bm25("fifteen"))
    assert st.bm25("trustee accounts", keys=["081/2026"]) and not st.bm25("trustee", year=1999)
    assert st.versions("trusts amendment")[0]["key"] == "081/2026"


def test_rrf_prefers_consensus():
    assert set(rrf([[1, 2, 3], [3, 2, 9]])[:2]) == {2, 3}
    assert rrf([[7], [7], [8]])[0] == 7


def test_retriever_collapses_identical_versions(tmp_path):
    st = make_store(tmp_path)
    res = Retriever(st).search("Act may be cited", k=5)
    hits = [c for c in res if c["kind"] == "section" and "may be cited" in c["text"]]
    assert len(hits) == 1 and hits[0]["also_in"] in (["081/2026"], ["086/2026"])
    # sections that differ between versions are NOT collapsed
    assert len([c for c in Retriever(st).search("trustee shall keep accounts", k=5) if "trustee" in c["text"]]) == 2


# ---------------------------------------------------------------- llm adapters
MSGS = [{"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "t", "args": {"x": 1}},
                                                            {"id": "b", "name": "t", "args": {}}]},
        {"role": "tool", "tool_call_id": "a", "name": "t", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "name": "t", "content": "rb"}]


def test_anthropic_conversion_merges_tool_results():
    out = to_anthropic_messages(MSGS)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["type"] for b in out[1]["content"]] == ["tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]


def test_openai_conversion():
    out = to_openai_messages("SYS", MSGS)
    assert out[0] == {"role": "system", "content": "SYS"}
    assert json.loads(out[2]["tool_calls"][0]["function"]["arguments"]) == {"x": 1}
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]
    assert out[2]["content"] == ""  # never null: some OpenAI-compatible servers (Workers AI) reject null


# ---------------------------------------------------------------- agent
class FakeLLM:
    def __init__(self, script):
        self.script, self.seen = list(script), []

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.script.pop(0)


def test_agent_tool_loop_and_citations(tmp_path):
    st = make_store(tmp_path)
    llm = FakeLLM([
        Reply("", [ToolCall("1", "search_bills", {"query": "fifteen days", "year": 2026}),
                   ToolCall("2", "get_section", {"bill": "086/2026", "section": 2})]),
        Reply("The Bill sets fifteen days [086/2026 §2]."),
    ])
    events = []
    ag = Agent(st, Retriever(st), llm, on_event=lambda n, a: events.append(n))
    assert "§2" in ag.ask("How many days?")
    tool_msgs = [m for m in llm.seen[1] if m["role"] == "tool"]
    assert "fifteen days" in tool_msgs[0]["content"] and "[086/2026 §2" in tool_msgs[1]["content"]
    assert events == ["search_bills", "get_section"]


def test_agent_tools_diff_overview_errors(tmp_path):
    st = make_store(tmp_path)
    ag = Agent(st, Retriever(st), FakeLLM([]))
    d = ag.run_tool("diff_bills", {"bill_a": "081/2026", "bill_b": "086/2026"})
    assert "1 changed [2]" in d and "+" in d and "fifteen" in d
    o = ag.run_tool("get_bill_overview", {"bill": "86/2026"})
    assert "Related versions: 081/2026" in o and "Outline" in o
    assert ag.run_tool("get_bill_overview", {"bill": "trusts"}).startswith("ERROR: ValueError: Ambiguous")
    assert ag.run_tool("get_section", {"bill": "086/2026", "section": 99}).startswith("ERROR")
    assert ag.run_tool("nope", {}).startswith("ERROR")
    assert ag.run_tool("list_bills", {"year": 2026}).count("\n") == 1


def test_agent_step_limit_forces_answer(tmp_path):
    st = make_store(tmp_path)
    loop = Reply("", [ToolCall("x", "list_bills", {})])
    llm = FakeLLM([loop, loop, Reply("final")])
    assert Agent(st, Retriever(st), llm, max_steps=2).ask("q") == "final"


def test_tool_specs_valid():
    assert {t["name"] for t in TOOLS} == {"find_bills", "search_bills", "list_bills", "get_bill_overview", "get_section", "diff_bills"}
    assert all(t["parameters"]["type"] == "object" for t in TOOLS)


def test_schedule_mode_exits_at_next_section():
    pages = [COVER, "BE it enacted by the Parliament as follows:-\n" + "\n".join(
        [f"{i}. Section {i} text here." for i in range(1, 7)] +
        ["FIRST SCHEDULE", "1. Item one", "Column I Column II", "7. Section seven resumes the Act."])]
    nos = [s.no for s in parse_pages(pages, "X").sections if s.kind == "section"]
    assert nos == [1, 2, 3, 4, 5, 6, 7]
    # short bill: numbered schedule items must NOT become sections
    pages = [COVER, "BE it enacted by the Parliament as follows:-\n1. Short title text.\n2. Another clause here.\n"
             "SCHEDULE\n3. Item three of the schedule"]
    assert [s.no for s in parse_pages(pages, "X").sections if s.kind == "section"] == [1, 2]


# ---------------------------------------------------------------- evaluation metrics
def test_eval_rank_and_summary():
    from billrag.evaluate import first_hit_rank, summarize
    clause = {"level": "clause", "type": "detail", "gold": [{"bill": "A/1", "section": 3}]}
    res = [{"bill_key": "B/1", "section_no": 3}, {"bill_key": "B/1", "section_no": 4},
           {"bill_key": "C/1", "section_no": 3, "also_in": ["A/1"]}]  # identical text also in the gold bill
    assert first_hit_rank(clause, res) == 3
    assert first_hit_rank(clause, res[:2]) is None  # right section number, wrong bill
    bill = {"level": "bill", "type": "bill-purpose", "gold": [{"bill": "B/1"}]}
    assert first_hit_rank(bill, [{"bill_key": "Z/1", "section_no": None}, {"bill_key": "B/1", "section_no": None}]) == 2
    s = summarize([{"item": clause, "rank": 3}, {"item": bill, "rank": 1}, {"item": bill, "rank": None}])
    assert s["all"]["n"] == 3 and s["all"]["r@1"] == 1 / 3 and s["all"]["r@5"] == 2 / 3
    assert abs(s["all"]["mrr"] - (1 / 3 + 1) / 3) < 1e-9 and s["by_type"]["bill-purpose"]["n"] == 2


def test_eval_file_is_well_formed():
    import pathlib
    from billrag.evaluate import load_items
    items = load_items(pathlib.Path(__file__).parent.parent / "evals" / "retrieval_eval.jsonl")
    assert len(items) >= 40 and len({i["id"] for i in items}) == len(items)
    for i in items:
        assert i["level"] in ("clause", "bill") and i["question"].strip() and i["gold"]
        assert all("section" in g for g in i["gold"]) if i["level"] == "clause" else all("bill" in g for g in i["gold"])


# ---------------------------------------------------------------- cloudflare adapters (HTTP mocked)
def test_cloudflare_embedder_batches_in_order():
    from billrag.llm import CloudflareEmbedder
    e = CloudflareEmbedder("m", "acc", "tok", batch=2, workers=3)
    e._post = lambda model, body: {"data": [[float(len(t))] for t in body["text"]]}
    out = e.embed(["a", "bb", "ccc", "dddd", "eeeee"])
    assert out.shape == (5, 1) and out[:, 0].tolist() == [1, 2, 3, 4, 5]  # order preserved across threads


def test_cloudflare_reranker_aligns_scores_and_truncates():
    from billrag.llm import CloudflareReranker
    rr = CloudflareReranker("m", "acc", "tok", max_chars=5)
    seen = {}
    def fake(model, body):
        seen["ctx"] = [c["text"] for c in body["contexts"]]
        return {"response": [{"id": 2, "score": 0.9}, {"id": 0, "score": 0.1}, {"id": 1, "score": 0.5}]}
    rr._post = fake
    assert rr.scores("q", ["aaaaaaaa", "b", "c"]) == [0.1, 0.5, 0.9] and seen["ctx"][0] == "aaaaa"


def test_retriever_cross_rerank_modes(tmp_path):
    st = make_store(tmp_path)

    class Rev:  # prefers the LAST first-stage candidate
        def scores(self, q, texts):
            return [float(i) for i in range(len(texts))]

    base = [c["id"] for c in Retriever(st).search("trustee accounts section", k=6)]
    pure = [c["id"] for c in Retriever(st, rerank="cloudflare", reranker=Rev()).search("trustee accounts section", k=6)]
    fused = [c["id"] for c in Retriever(st, rerank="cloudflare-fused", reranker=Rev()).search("trustee accounts section", k=6)]
    assert pure != base and set(pure) == set(base) and len(fused) == len(base)

    class Boom:
        def scores(self, q, texts):
            raise RuntimeError("api down")
    assert [c["id"] for c in Retriever(st, rerank="cloudflare", reranker=Boom()).search("trustee accounts section", k=6)] == base


# ---------------------------------------------------------------- defaults and loud failure
def test_settings_default_to_cloudflare_only_with_credentials(monkeypatch):
    from billrag.config import load_settings
    for k in ("BILLRAG_EMBED_PROVIDER", "BILLRAG_RERANK", "CLOUDFLARE_API_TOKEN", "BILLRAG_CF_ACCOUNT_ID",
              "CLOUDFLARE_ACCOUNT_ID", "R2_ACCOUNT_ID"):
        monkeypatch.delenv(k, raising=False)
    s = load_settings()
    assert (s.embed_provider, s.rerank) == ("none", "none")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    assert load_settings().embed_provider == "none"  # token alone is not enough: needs an account id
    monkeypatch.setenv("R2_ACCOUNT_ID", "a")
    s = load_settings()
    assert (s.embed_provider, s.rerank) == ("cloudflare", "cloudflare")
    monkeypatch.setenv("BILLRAG_EMBED_PROVIDER", "none")  # explicit setting wins
    assert load_settings().embed_provider == "none"


def test_fingerprint_tracks_index_content(tmp_path):
    a = make_store(tmp_path / "a")
    b = make_store(tmp_path / "b")
    assert a.fingerprint() == b.fingerprint()  # deterministic
    a.db.execute("UPDATE chunks SET ctx = ctx || ' x' WHERE id = 1")
    assert a.fingerprint() != b.fingerprint()


def test_dense_retrieval_fails_loudly_on_missing_or_stale_vectors(tmp_path):
    import numpy as np
    from billrag.retrieve import VectorsError

    class E:
        def embed(self, texts):
            return np.ones((len(texts), 2), dtype="float32")

    st = make_store(tmp_path)
    path = tmp_path / "v.npz"
    with pytest.raises(VectorsError, match="does not exist"):
        Retriever(st, E(), path)
    ids = np.asarray([r["id"] for r in st.all_chunks()])
    np.savez(path, ids=ids, vectors=np.ones((len(ids), 2), dtype="float32"))  # no fingerprint
    with pytest.raises(VectorsError, match="stale or unstamped"):
        Retriever(st, E(), path)
    np.savez(path, ids=ids, vectors=np.ones((len(ids), 2), dtype="float32"), fingerprint=np.asarray(st.fingerprint()))
    assert Retriever(st, E(), path).mat is not None  # valid vectors load
    st.db.execute("UPDATE chunks SET ctx = 'changed' WHERE id = 1")
    with pytest.raises(VectorsError):
        Retriever(st, E(), path)  # index changed after embedding -> refuse
    Retriever(st)  # keyword-only retriever (no embedder) is unaffected


# ---------------------------------------------------------------- incremental embeddings + summaries
class CountingEmbedder:
    model = "fake"

    def __init__(self):
        self.seen = []

    def embed(self, texts):
        import numpy as np
        self.seen += list(texts)
        return np.asarray([[float(len(t)), 1.0] for t in texts], dtype="float32")


def test_embed_all_is_incremental_and_stamped(tmp_path):
    import numpy as np
    from billrag.enrich import embed_all
    st, path, emb = make_store(tmp_path), tmp_path / "v.npz", CountingEmbedder()
    n_all = len(st.all_chunks())
    assert embed_all(st, emb, path) == n_all and len(emb.seen) == n_all
    before = np.load(path)["vectors"].copy()
    emb.seen.clear()
    assert embed_all(st, emb, path) == 0 and not emb.seen  # nothing changed -> nothing embedded
    st.update_overview("086/2026", "Summary: new text about trusts")
    assert embed_all(st, emb, path) == 1 and emb.seen == ["Summary: new text about trusts"]
    after = np.load(path)
    changed = [i for i in range(n_all) if not np.allclose(before[i], after["vectors"][i])]
    assert len(changed) == 1  # only the edited overview chunk moved
    assert str(after["fingerprint"]) == st.fingerprint()
    Retriever(st, emb, path)  # fresh vectors validate against the edited index


def test_embed_all_reuses_legacy_file_matching_current_index(tmp_path):
    import numpy as np
    from billrag.enrich import embed_all
    st, path, emb = make_store(tmp_path), tmp_path / "v.npz", CountingEmbedder()
    rows = st.all_chunks()
    np.savez(path, ids=np.asarray([r["id"] for r in rows]), vectors=np.ones((len(rows), 2), dtype="float32"),
             fingerprint=np.asarray(st.fingerprint()))  # old format: no chunk_hashes
    assert embed_all(st, emb, path) == 0 and "chunk_hashes" in np.load(path).files


def test_summarize_all_updates_overview_and_is_resumable(tmp_path):
    from billrag.enrich import summarize_all
    st = make_store(tmp_path)
    good = "Allows trustees more time to keep accounts and lengthens a deadline. " * 3 + "\nKeywords: trust, accounts, deadline"
    llm = FakeLLM([Reply(good), Reply(good)])
    assert summarize_all(st, llm, workers=1) == 2
    assert st.bill("086/2026")["summary"] == good.strip()
    ov = st.db.execute("SELECT text FROM chunks WHERE bill_key='086/2026' AND kind='overview'").fetchone()[0]
    assert ov.startswith("Bill 086/2026") and "Summary (auto-generated): Allows trustees" in ov and "Keywords: trust" in ov and "Outline:" in ov
    assert st.bm25("deadline", kinds=("overview",))  # now findable through the summary text
    assert summarize_all(st, FakeLLM([]), workers=1) == 0  # nothing left to do; the LLM is not called


def test_summarize_survives_bad_model_output(tmp_path):
    from billrag.enrich import summarize_all
    st = make_store(tmp_path, with_gs=False)
    assert summarize_all(st, FakeLLM([Reply("too short"), Reply("too short")]), workers=1) == 0
    assert st.bill("086/2026")["summary"] is None  # nothing stored; retried on the next run


def test_find_bills_returns_one_entry_per_bill_with_versions(tmp_path):
    st = make_store(tmp_path)
    ag = Agent(st, Retriever(st), FakeLLM([]))
    out = ag.run_tool("find_bills", {"query": "amend the Trusts Ordinance", "top_k": 5})
    assert "086/2026" in out and "Long title: to amend the Trusts Ordinance" in out
    assert "Other versions: 081/2026" in out or "Other versions: 086/2026" in out
    assert out.count("Long title:") == len(set(l.split(" | ")[0] for l in out.splitlines() if " | " in l))  # no clause chunks
    assert ag.run_tool("find_bills", {"query": "zzzqqq"}) == "No matching bills."


def test_versions_are_limited_to_the_same_bill_by_date(tmp_path):
    st = make_store(tmp_path)
    old = {"key": "010/2019", "bill_no": 10, "year": 2019, "date": "2019-03-01T00:00:00Z", "title": "Trusts (Amendment) - Bill",
           "title_norm": "trusts amendment", "kind": "Bill", "long_title": "x", "presented_by": None, "presented_on": None,
           "gazette_date": None, "printed_no": None, "n_pages": 1, "n_sections": 1, "pdf_path": "x", "summary": None}
    st.replace_bill(old, [{"kind": "overview", "text": "old", "ctx": "old"}])
    assert {v["key"] for v in st.versions("trusts amendment")} == {"010/2019", "081/2026", "086/2026"}  # same title, 3 bills
    near = {v["key"] for v in st.versions("trusts amendment", "2026-10-21T00:00:00Z")}
    assert near == {"081/2026", "086/2026"}  # the 2019 bill is a different bill
    ag = Agent(st, Retriever(st), FakeLLM([]))
    assert "010/2019" not in ag.run_tool("get_bill_overview", {"bill": "086/2026"})


def test_search_bills_returns_clauses_only_and_find_bills_returns_overviews(tmp_path):
    st = make_store(tmp_path)
    ag = Agent(st, Retriever(st), FakeLLM([]))
    st.update_overview("086/2026", "Summary (auto-generated): trustees must keep accounts for fifteen days. Keywords: trustee accounts")
    clauses = ag.run_tool("search_bills", {"query": "trustee accounts summary"})
    assert "overview" not in clauses and "auto-generated" not in clauses  # summaries never crowd out clauses
    assert "Long title:" in ag.run_tool("find_bills", {"query": "trustee accounts"})


def test_agent_recovers_from_leaked_tool_call_text(tmp_path):
    st = make_store(tmp_path)
    llm = FakeLLM([
        Reply("analysis ... <|end|><|start|>assistant to=functions search_b billscommentaryjson{ \"query\": \"x\"}<|call|>"),
        Reply("", [ToolCall("1", "list_bills", {})]),
        Reply("Done: 086/2026 exists."),
    ])
    ag = Agent(st, Retriever(st), llm)
    assert ag.ask("q") == "Done: 086/2026 exists."
    assert any("malformed tool-call text" in m["content"] for m in llm.seen[1] if m["role"] == "user")
    assert not any("<|call|>" in (m.get("content") or "") for m in ag.messages)  # leak not kept in history
    # a model that keeps leaking is cut off with the text before the leak, never the raw tokens
    llm2 = FakeLLM([Reply("Partial answer <|call|> junk")] * 3)
    assert Agent(st, Retriever(st), llm2).ask("q") == "Partial answer"


# ---------------------------------------------------------------- two-step pipeline (retrieve -> grade -> summarise)
class ScriptLLM:
    """Answers each pipeline prompt by inspecting it, like a (very literal) model would."""

    def __init__(self, enough=True, queries=(), bad_eval=False, cite="[086/2026 \u00a72]"):
        self.calls, self.efforts, self.enough, self.queries, self.bad_eval, self.cite = [], [], enough, list(queries), bad_eval, cite
        self.rounds = 0

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        user = messages[-1]["content"]
        self.efforts.append(effort)
        self.calls.append(user)
        if "CANDIDATES:" in user:
            if self.bad_eval:
                return Reply("sorry, no json")
            cands = re.findall(r"^\[(\d+)\] (.*?)(?=\n\n\[\d+\] |\Z)", user.split("CANDIDATES:")[1].strip(), re.S | re.M)
            scores = [{"i": int(i), "s": 3 if ("fifteen days" in body or "Zebra" in body) else 0} for i, body in cands]
            self.rounds += 1
            enough = self.enough or self.rounds > 1
            return Reply(json.dumps({"scores": scores, "enough": enough, "missing": "the rest", "queries": self.queries}))
        if "FOLLOW-UP:" in user:
            return Reply("What does the Trusts Amendment Bill 086/2026 say about fifteen days?")
        return Reply(f"The Bill replaces ten days with fifteen days {self.cite}.")


import re  # noqa: E402  (used by ScriptLLM)
from billrag.pipeline import Pipeline, parse_json  # noqa: E402


def test_parse_json_tolerates_chatter():
    assert parse_json('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert parse_json("no json here") is None
    assert parse_json('analysis: use {braces} freely, then {"scores": [1], "enough": true} done') == {"scores": [1], "enough": True}
    assert parse_json('{"a": 1} and later {"b": 2}') == {"a": 1}


def test_overview_question_on_named_bill_adds_overview(tmp_path):
    st = make_store(tmp_path, with_gs=False)
    st.db.execute("UPDATE chunks SET text = text || ' Summary (auto-generated): amends the Trusts Ordinance.' "
                  "WHERE kind='overview' AND bill_key='086/2026'")
    st.commit()
    llm = ScriptLLM(enough=False)
    p = Pipeline(st, Retriever(st), llm, on_event=lambda n, d: None)
    r = p.ask("What are the key provisions of bill 086/2026?")
    assert r.added_overview == "086/2026" and r.enough
    assert r.evidence[0].chunk["kind"] == "overview"
    named = Pipeline(st, Retriever(st), ScriptLLM(enough=False)).ask("Summarise the Trusts Amendment bill")
    assert named.added_overview == "086/2026" and not named.clarify  # named by title, no number
    vague = Pipeline(st, Retriever(st), ScriptLLM(enough=False)).ask("Summarise the amendment bill")
    assert vague.added_overview == ""  # one generic word is not a title
    other = Pipeline(st, Retriever(st), ScriptLLM()).ask("How many days does bill 086/2026 allow?")
    assert other.added_overview == ""  # a specific question is left to the ranked clauses


def test_ambiguous_title_asks_which_bill(tmp_path):
    st = make_store(tmp_path, with_gs=False)
    llm = ScriptLLM(enough=False)
    p = Pipeline(st, Retriever(st), llm, on_event=lambda n, d: None)
    p.titled_bills = lambda q, ev: [("086/2026", 1, "2026-10-21", "Trusts (Amendment) - Bill"),
                                    ("081/2026", 1, "2026-10-14", "Trusts (Special) - GS")]
    r = p.ask("Summarise the trusts bill")
    assert r.clarify and "Which did you mean?" in r.answer and "(086/2026, 2026-10-21)" in r.answer
    assert "Trusts (Special) (081/2026" in r.answer and r.evidence == []
    assert len(llm.calls) == 1 and not p.history  # only the grading call; nothing kept for the next turn


def _store_with_titles(tmp_path):
    st = make_store(tmp_path, with_gs=False)
    extra = [("090/2026", "Alpha Fisheries Foundation (Incorporation)"), ("091/2026", "Beta Fisheries Foundation (Incorporation)"),
             ("092/2026", "Gamma Cricket Authority")]
    for key, title in extra:
        parsed = parse_pages([COVER, body()], title)
        _, tnorm = classify(title)
        st.replace_bill({"key": key, "bill_no": int(key[:3]), "year": 2026, "date": "2026-10-01T00:00:00Z", "title": title,
                         "title_norm": tnorm, "kind": "Bill", "long_title": parsed.cover.get("long_title"), "presented_by": None,
                         "presented_on": None, "gazette_date": None, "printed_no": None, "n_pages": 2, "n_sections": 2,
                         "pdf_path": "x", "summary": None}, build_chunks(
            {"key": key, "bill_no": int(key[:3]), "year": 2026, "date": "2026-10-01T00:00:00Z", "title": title, "title_norm": tnorm,
             "kind": "Bill", "long_title": None, "n_sections": 2, "summary": None}, parsed))
    st.commit()
    from billrag import titles
    titles.build(st)
    return st


def test_title_ranker_one_many_none(tmp_path):
    from billrag import titles
    rk = titles.Ranker(_store_with_titles(tmp_path))
    d, c = rk.rank("key provisions of the Gamma Cricket Authority bill")
    assert d == "one" and c[0]["key"] == "092/2026"
    d, c = rk.rank("summarise the Gamma Crikcet Autority bill")  # close spellings
    assert d == "one" and c[0]["key"] == "092/2026"
    d, c = rk.rank("tell me about the fisheries foundation bill")  # two titles fit equally
    assert d == "many" and {r["key"] for r in c} == {"090/2026", "091/2026"}
    assert rk.rank("tell me about the new bill")[0] == "none"


def test_pipeline_uses_title_index(tmp_path):
    st = _store_with_titles(tmp_path)
    llm = ScriptLLM(enough=False)
    p = Pipeline(st, Retriever(st), llm, on_event=lambda n, d: None)
    r = p.ask("tell me about the fisheries foundation bill")
    assert r.clarify and "090/2026" in r.answer and "091/2026" in r.answer and not llm.calls
    r2 = Pipeline(st, Retriever(st), ScriptLLM(enough=False)).ask("Summarise the Gamma Cricket Authority bill")
    assert r2.added_overview == "092/2026" and not r2.clarify


def test_long_section_is_shown_as_a_window_around_the_match(tmp_path):
    from billrag.pipeline import Evidence
    paras = "\n".join(f"({chr(97 + i)}) clause {chr(97 + i)} " + ("word " * 100) + ("ZEBRA unique marker." if i == 8 else "") for i in range(12))
    page = "BE it enacted by the Parliament of the Democratic Socialist\nRepublic of Sri Lanka as follows:-\n1. Short title text.\n2. Long section.\n" + paras
    st = Store(tmp_path / "w.sqlite")
    parsed = parse_pages([COVER, page], "Long Act")
    row = {"key": "095/2026", "bill_no": 95, "year": 2026, "date": "2026-10-01T00:00:00Z", "title": "Long Act", "title_norm": "long act",
           "kind": "Bill", "long_title": None, "presented_by": None, "presented_on": None, "gazette_date": None, "printed_no": None,
           "n_pages": 2, "n_sections": 2, "pdf_path": "x", "summary": None}
    st.replace_bill(row, build_chunks(row, parsed))
    st.commit()
    parts = st.section_parts("095/2026", 2)
    assert len(parts) >= 5 and sum(len(p["text"]) for p in parts) > 3000
    hit = next(p for p in parts if "ZEBRA" in p["text"])
    p = Pipeline(st, Retriever(st), ScriptLLM())
    ev = Evidence(st.chunks_by_ids([hit["id"]])[hit["id"]], 3)
    text = p.expand(ev).chunk["text"]
    assert "ZEBRA" in text and len(text) < 3400 and "are not shown" in text  # the matched part is kept, the gap is announced
    assert "clause a" not in text  # the opening was left out, not the match
    whole = p.expand(Evidence(st.chunks_by_ids([parts[0]["id"]])[parts[0]["id"]], 3)).chunk["text"]
    assert "clause a" in whole and "not shown" not in whole  # a match inside the first 3000 characters: the old cut, no notes
    short = Pipeline(st, Retriever(st), ScriptLLM(), ).expand(Evidence(st.chunks_by_ids([st.section_parts("095/2026", 1)[0]["id"]])[st.section_parts("095/2026", 1)[0]["id"]], 3)).chunk["text"]
    assert "not shown" not in short  # a short section is shown whole


def test_pipeline_happy_path_two_llm_calls(tmp_path):
    st, llm = make_store(tmp_path), ScriptLLM()
    events = []
    p = Pipeline(st, Retriever(st), llm, on_event=lambda n, d: events.append(n))
    r = p.ask("How many days does the amendment allow?")
    assert r.llm_calls == 2 and len(llm.calls) == 2  # grade + summarise, nothing else
    assert "fifteen days" in r.answer and not r.warnings and r.enough
    assert [e.key for e in r.evidence] == ["086/2026"] and r.evidence[0].section == 2
    assert "fifteen days" in r.evidence[0].chunk["text"]  # the full section text went to the summariser
    assert r.sources() == ["[086/2026 \u00a72, p.2] Trusts (Amendment) - Bill (Bill, 2026-10-21)"] or r.sources()[0].startswith("[086/2026 \u00a72")
    assert "EVIDENCE:" in llm.calls[1] and "[086/2026 \u00a72]" in llm.calls[1]
    assert events == ["retrieve", "evaluate"]


def test_pipeline_flags_citations_not_in_evidence(tmp_path):
    st = make_store(tmp_path)
    r = Pipeline(st, Retriever(st), ScriptLLM(cite="[099/2026 \u00a73]")).ask("How many days?")
    assert any("099/2026 \u00a73" in w for w in r.warnings)
    ok = Pipeline(st, Retriever(st), ScriptLLM(cite="[086/2026 \u00a72(1)]")).ask("How many days?")  # sub-clause cite is fine
    assert not ok.warnings


def test_pipeline_runs_one_followup_round_when_evidence_is_insufficient(tmp_path):
    st = make_store(tmp_path)
    extra = {"key": "090/2026", "bill_no": 90, "year": 2026, "date": "2026-10-30T00:00:00Z", "title": "Zebra Protection - Bill",
             "title_norm": "zebra protection", "kind": "Bill", "long_title": "to protect zebras", "presented_by": None,
             "presented_on": None, "gazette_date": None, "printed_no": None, "n_pages": 1, "n_sections": 1, "pdf_path": "x",
             "summary": None}
    st.replace_bill(extra, [{"kind": "section", "section_no": 1, "page": 1, "text": "Zebra grazing quota applies to herds.",
                             "ctx": "Bill 090/2026 zebra grazing quota applies to herds"}])
    st.commit()
    llm = ScriptLLM(enough=False, queries=["zebra quota"])
    r = Pipeline(st, Retriever(st), llm).ask("How many days?")
    assert r.llm_calls == 3  # grade, grade the new candidates, summarise
    assert r.queries == ["How many days?", "zebra quota"] and r.enough
    grade_prompts = [c for c in llm.calls if "CANDIDATES:" in c]
    assert len(grade_prompts) == 2 and "Zebra" in grade_prompts[1] and "Zebra" not in grade_prompts[0]  # round two grades only NEW passages
    assert {e.key for e in r.evidence} == {"086/2026", "090/2026"}  # evidence merged across rounds


def test_pipeline_stops_when_followup_finds_nothing_new_and_says_evidence_is_incomplete(tmp_path):
    st, llm = make_store(tmp_path), ScriptLLM(enough=False, queries=["trustee accounts deadline"])
    r = Pipeline(st, Retriever(st), llm).ask("How many days?")
    assert r.llm_calls == 2 and not r.enough  # nothing new to grade -> no wasted call
    assert "INCOMPLETE" in llm.calls[-1]  # the summariser is told the evidence is incomplete
    r2 = Pipeline(st, Retriever(st), ScriptLLM(enough=False, queries=["x"]), extra_rounds=0).ask("How many days?")
    assert r2.llm_calls == 2 and not r2.enough


def test_pipeline_survives_unusable_grader_output(tmp_path):
    st = make_store(tmp_path)
    r = Pipeline(st, Retriever(st), ScriptLLM(bad_eval=True)).ask("How many days?")
    assert r.answer and any("unusable" in w for w in r.warnings) and r.evidence


def test_pipeline_no_candidates_means_no_llm_calls(tmp_path):
    st, llm = make_store(tmp_path), ScriptLLM()
    r = Pipeline(st, Retriever(st), llm).ask("zzzqqq")
    assert llm.calls == [] and "could not find" in r.answer and r.evidence == []


def test_pipeline_condenses_followups_and_honours_explicit_bill_numbers(tmp_path):
    st, llm = make_store(tmp_path), ScriptLLM()
    p = Pipeline(st, Retriever(st), llm)
    p.ask("How many days?")
    n = len(llm.calls)
    r = p.ask("and what about the GS version 081/2026?")
    assert "FOLLOW-UP:" in llm.calls[n] and r.llm_calls == 3  # condense + grade + summarise
    assert all(e.key == "086/2026" or e.key == "081/2026" for e in r.evidence)
    r2 = Pipeline(st, Retriever(st), ScriptLLM()).ask("what does 081/2026 say about trustee accounts?")
    assert {c.key for c in r2.evidence} <= {"081/2026"}  # an explicit bill number restricts retrieval to that bill


def test_pipeline_uses_low_effort_for_grading_only_and_checks_fullwidth_citations(tmp_path):
    st, llm = make_store(tmp_path), ScriptLLM(cite="\u3010099/2026 \u00a73\u3011")
    r = Pipeline(st, Retriever(st), llm, grade_effort="low", answer_effort=None).ask("How many days?")
    assert llm.efforts == ["low", None]  # grade cheaply, answer at the model's default
    assert any("099/2026 \u00a73" in w for w in r.warnings)  # full-width brackets are verified too


def test_openai_adapter_sends_effort_as_extra_body():
    from billrag.llm import OpenAILLM

    class Msg:
        content, tool_calls = "ok", None

    class Resp:
        choices = [type("C", (), {"message": Msg})()]

    seen = {}

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return Resp()

    llm = object.__new__(OpenAILLM)
    llm.client, llm.model = Client, "m"
    assert llm.chat("s", [{"role": "user", "content": "q"}], effort="low").text == "ok"
    assert seen["extra_body"] == {"reasoning_effort": "low"}
    seen.clear()
    llm.chat("s", [{"role": "user", "content": "q"}])
    assert "extra_body" not in seen  # omitted unless asked for


# ---------------------------------------------------------------- answer-level eval helpers
def test_answer_eval_scoring_helpers():
    from billrag import answer_eval as ae
    clause = {"level": "clause", "gold": [{"bill": "001/2026", "section": 3}, {"bill": "002/2026", "section": 3}]}
    assert ae.cites_gold(clause, "See [002/2026 §3(2)] for details") and ae.cites_gold(clause, "【001/2026 §3】")
    assert not ae.cites_gold(clause, "[001/2026 §4] and [003/2026 §3]")  # right bill wrong section; wrong bill right section
    bill = {"level": "bill", "gold": [{"bill": "001/2026"}]}
    assert ae.cites_gold(bill, "It is [001/2026 §2]") and not ae.cites_gold(bill, "It is [009/2026]")
    chunk = {"bill_key": "003/2026", "section_no": 3, "also_in": ["001/2026"]}
    assert ae.chunk_has_gold(clause, chunk) and not ae.chunk_has_gold(clause, {"bill_key": "003/2026", "section_no": 4})
    items = [{"id": i} for i in range(10)]
    assert [i["id"] for i in ae.select(items, 5)] == [0, 2, 4, 6, 8] and ae.select(items, 0) == items
    rows = [{"level": "clause", "cited_gold": True, "verdict": "correct", "seconds": 4, "cand_gold": True, "kept_gold": False, "llm_calls": 2},
            {"level": "bill", "cited_gold": False, "verdict": "partial", "seconds": 6, "cand_gold": True, "kept_gold": True, "llm_calls": 3}]
    s = ae.summarize(rows)
    assert s["all"]["correct"] == 0.5 and s["all"]["correct_or_partial"] == 1.0 and s["all"]["avg_seconds"] == 5
    assert s["all"]["kept_gold"] == 0.5 and s["clause"]["n"] == 1 and s["bill"]["cited_gold"] == 0.0


def test_grader_retries_once_when_the_first_reply_is_prose(tmp_path):
    st = make_store(tmp_path)

    class Flaky(ScriptLLM):
        def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
            user = messages[-1]["content"]
            if "CANDIDATES:" in user and not any("CANDIDATES:" in c for c in self.calls):
                self.calls.append(user); self.efforts.append(effort)
                return Reply("analysis: we need to produce a summary of the bill ...")  # prose, no JSON
            return super().chat(system, messages, tools, max_tokens, effort)

    llm = Flaky()
    r = Pipeline(st, Retriever(st), llm, grade_effort="low").ask("How many days?")
    assert r.llm_calls == 3 and not r.warnings  # prose reply -> one firmer retry -> real grades, no fallback
    assert llm.efforts[:2] == ["low", None] and "JSON object ONLY" in llm.calls[1]
    assert r.evidence and r.evidence[0].score == 3
def test_web_ui_endpoints(tmp_path):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler

    class StubApp:
        def search(self, q, k=6):
            return [f"hit:{q}:{k}"]

        def ask(self, sid, q):
            if q == "boom":
                raise RuntimeError("no key")
            return f"{sid}:{q}"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubApp()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, body):
        req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        with urllib.request.urlopen(base + "/") as r:
            assert b"Bill Assistant" in r.read()
        assert post("/api/ask", {"session": "s", "question": "hi"}) == (200, {"answer": "s:hi"})
        assert post("/api/search", {"query": "x", "k": 99}) == (200, {"results": ["hit:x:15"]})
        assert post("/api/ask", {"question": " "})[0] == 400
        code, body = post("/api/ask", {"question": "boom"})
        assert code == 500 and "no key" in body["error"]
    finally:
        srv.shutdown()


def test_float16_vectors_rank_like_float32():
    import numpy as np
    from billrag import retrieve
    rng = np.random.default_rng(1)
    m = rng.normal(size=(300, 16)).astype("float32")
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    q = m[7] + rng.normal(0, 0.01, 16).astype("float32")
    q /= np.linalg.norm(q)
    r32 = retrieve.Retriever.__new__(retrieve.Retriever)
    r16 = retrieve.Retriever.__new__(retrieve.Retriever)
    r32.mat, r16.mat = m, m.astype("float16")
    old = retrieve.SCAN_BLOCK
    retrieve.SCAN_BLOCK = 64  # force several blocks
    try:
        s32, s16 = r32._scores(q), r16._scores(q)
    finally:
        retrieve.SCAN_BLOCK = old
    assert s16.dtype == np.float32 and len(s16) == 300
    assert np.argmax(s32) == np.argmax(s16) == 7
    assert abs(s32 - s16).max() < 5e-3


def test_web_sessions_are_capped(monkeypatch):
    from billrag import web
    app = web.App.__new__(web.App)
    app.lock = __import__("threading").Lock()
    app.sessions, app.turns = web.OrderedDict(), {}
    app._new_responder = lambda: (lambda q: ("ok", {}))
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    monkeypatch.setattr(web, "MAX_SESSIONS", 3)
    for i in range(6):
        assert app.ask(f"s{i}", "hi") == "ok"
    assert list(app.sessions) == ["s3", "s4", "s5"]


def test_web_requires_proxy_secret_when_configured():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler

    class StubApp:
        secret = "s3cret-s3cret-s3cret"

        def ask(self, sid, q):
            return "ok"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubApp()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(path, secret=None, post=False):
        h = {"Content-Type": "application/json", **({"X-Proxy-Secret": secret} if secret else {})}
        req = urllib.request.Request(base + path, b'{"question":"q"}' if post else None, h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        assert call("/") == 403 and call("/api/ask", post=True) == 403
        assert call("/", "wrong-wrong-wrong-wrong") == 403
        assert call("/", StubApp.secret) == 200 and call("/api/ask", StubApp.secret, post=True) == 200
    finally:
        srv.shutdown()


def test_serve_refuses_public_bind_without_secret(monkeypatch):
    import pytest
    from billrag import web
    monkeypatch.delenv("BILLRAG_PROXY_SECRET", raising=False)
    with pytest.raises(SystemExit, match="Refusing"):
        web.serve(None, host="0.0.0.0")
    monkeypatch.setenv("BILLRAG_PROXY_SECRET", "short")
    with pytest.raises(SystemExit, match="16 characters"):
        web.serve(None, host="127.0.0.1")


def test_web_app_shares_one_retriever(monkeypatch):
    import threading
    from billrag import web
    built = []
    app = web.App.__new__(web.App)
    app.lock, app._retr, app._llm = threading.Lock(), None, None

    class S:
        rerank, vectors_path = "none", None
    app.s, app.store = S(), None
    monkeypatch.setattr("billrag.llm.make_embedder", lambda s: None)
    monkeypatch.setattr("billrag.llm.make_reranker", lambda s: None)
    monkeypatch.setattr("billrag.retrieve.Retriever", lambda *a: built.append(a) or object())
    first = app._retriever()
    app.warm()
    assert app._retriever() is first and len(built) == 1


def test_web_healthz_and_page_has_wakeup_handling():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(type("A", (), {})()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/healthz") as r:
            assert json.loads(r.read()) == {"ok": True}
        with urllib.request.urlopen(base + "/") as r:
            page = r.read().decode()
        assert "waitUntilUp" in page and "HTTP ${r.status}" in page
    finally:
        srv.shutdown()


def test_chatlog_records_a_turn_and_can_be_disabled(tmp_path, monkeypatch):
    import time
    from billrag import chatlog
    monkeypatch.setenv("BILLRAG_LOG_DIR", str(tmp_path))
    for k in ("R2_ACCOUNT_ID", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    chatlog.log_turn(type="ask", session="abc-123", turn=2, question="Summarise PTSA 2026", answer="x",
                     error=None, queries=["q1", "q2"], enough=False, evidence=[{"bill": "070/2026", "section": 3, "score": 3}])
    for _ in range(50):  # the write happens on a background thread
        files = list(tmp_path.glob("*.jsonl"))
        if files and files[0].read_text().strip():
            break
        time.sleep(0.05)
    rec = json.loads(files[0].read_text().splitlines()[0])
    assert rec["question"] == "Summarise PTSA 2026" and rec["turn"] == 2 and rec["queries"] == ["q1", "q2"]
    assert rec["enough"] is False and rec["evidence"][0]["bill"] == "070/2026" and "ts" in rec
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    before = files[0].read_text()
    chatlog.log_turn(type="ask", question="should not be written")
    time.sleep(0.2)
    assert files[0].read_text() == before


def test_chatlog_r2_key_layout(monkeypatch, tmp_path):
    from billrag import chatlog
    puts = []

    class S3:
        def put_object(self, **kw):
            puts.append(kw)
    monkeypatch.setattr(chatlog, "_r2", lambda: S3())
    monkeypatch.setenv("R2_BUCKET", "b")
    (tmp_path / "afile").write_text("x")
    monkeypatch.setenv("BILLRAG_LOG_DIR", str(tmp_path / "afile" / "sub"))  # local sink fails; R2 must still be written
    chatlog._write({"question": "q"}, "2026-11-05", "123456789-abc-1")
    assert puts[0]["Bucket"] == "b" and puts[0]["Key"] == "chatlogs/2026-11-05/123456789-abc-1.json"
    assert json.loads(puts[0]["Body"])["question"] == "q"


BILLS_FOR_RESOLVE = [
    {"key": "070/2026", "title": "Protection of the State from Terrorism - GS", "year": 2026},
    {"key": "078/2026", "title": "Protection of the State from Terrorism - Bill", "year": 2026},
    {"key": "085/2026", "title": "Prevention of Foreign Terrorist Fighters - Bill", "year": 2026},
    {"key": "171/2022", "title": "Prevention of Terrorism (Temporary Provisions) (Amendment) - GS", "year": 2022},
    {"key": "100/2020", "title": "Pradeshiya Sabha (Amendment) - Bill", "year": 2020},
]


def test_acronym_matching_allows_transposition_and_act_suffix():
    from billrag import resolve
    assert resolve.acronym_score("PSTA", "PSTA") == 1.0
    assert resolve.acronym_score("PTSA", "PSTA") == 0.9  # letters transposed
    assert "PSTA" in resolve.acronym_variants("Protection of the State from Terrorism - Bill")
    assert {"PT", "PTA"} <= resolve.acronym_variants("Prevention of Terrorism (Temporary Provisions) (Amendment) - GS")
    top = resolve.match_acronym("PTSA", BILLS_FOR_RESOLVE, year=2026)[0]
    assert top[1] == "Protection of the State from Terrorism" and top[2] == ["070/2026", "078/2026"]
    assert resolve.match_acronym("ZZQX", BILLS_FOR_RESOLVE) == []


def test_resolve_question_cases(monkeypatch):
    from billrag import resolve
    monkeypatch.setattr(resolve, "all_bills", lambda store: BILLS_FOR_RESOLVE)
    monkeypatch.setattr(resolve, "known_in_corpus", lambda store, tok: tok in ("GS",))
    r = resolve.resolve_question(None, "Hi")
    assert r["action"] == "reply" and "bills of the Parliament" in r["text"]
    assert "welcome" in resolve.resolve_question(None, "thanks!")["text"]
    r = resolve.resolve_question(None, "Summarise PTSA 2026")
    assert r["action"] == "answer" and "070/2026, 078/2026" in r["question"] and "PTSA" not in r["question"]
    assert "PTSA" in r["notes"][0]
    r = resolve.resolve_question(None, "Summarise ZZQX 2026")
    assert r["action"] == "reply" and "don't recognise" in r["text"]
    r = resolve.resolve_question(None, "Summarise PTSA")  # no year: PTA-style titles compete -> ask
    assert r["action"] == "reply" and "Did you mean" in r["text"] and "Protection of the State" in r["text"]
    assert resolve.resolve_question(None, "What does the GS version change?")["action"] == "answer"
    assert resolve.resolve_question(None, "help me understand section 4")["action"] == "answer"


def test_pipeline_replies_without_calling_the_model_for_greetings(tmp_path):
    from billrag.pipeline import Pipeline
    st = make_store(tmp_path)
    pipe = Pipeline(st, Retriever(st), FakeLLM([]))  # an empty script raises if the model is called
    res = pipe.ask("Hi")
    assert "bills of the Parliament" in res.answer and res.llm_calls == 0 and not res.evidence
    assert pipe.history[-1][0] == "Hi"


def test_strip_reasoning_cases():
    from billrag.pipeline import strip_reasoning
    assert strip_reasoning("**Answer** fine") == ("**Answer** fine", False)
    assert strip_reasoning("analysis: thinking... assistantfinal **Answer** ok") == ("**Answer** ok", False)
    assert strip_reasoning("Analysis We need X. Final answer: It says Y [1/2026 §2].") == ("It says Y [1/2026 §2].", False)
    assert strip_reasoning("analysis: only reasoning, no answer") == ("analysis: only reasoning, no answer", True)
    assert strip_reasoning("An analysis of the Act shows") == ("An analysis of the Act shows", False)  # not a prefix leak


def test_degenerate_answers_are_detected():
    from billrag.pipeline import degenerate
    assert degenerate("!" * 400) and degenerate("") and degenerate("... --- ... ...")
    assert not degenerate("Yes [078/2026 §3].") and not degenerate("No.")
    assert not degenerate("The Act applies from 1 January 2027 [078/2026 §3(2)].")
    assert not degenerate("**Answer**\n\n| Section | Effect |\n|---|---|\n| 3 | Offence of terrorism |")


def test_pipeline_retries_once_when_the_model_leaks_reasoning(tmp_path):
    from billrag.pipeline import LEAK_FALLBACK, Pipeline, Result
    st = make_store(tmp_path)
    p = Pipeline(st, Retriever(st), FakeLLM([Reply("analysis: we need to think"), Reply("**Answer** done")]))
    res = Result(question="q", answer="")
    assert p._llm("sys", "user", 100, res) == "**Answer** done" and res.llm_calls == 2 and not res.warnings
    p = Pipeline(st, Retriever(st), FakeLLM([Reply("analysis: a"), Reply("analysis: b")]))
    res = Result(question="q", answer="")
    assert p._llm("sys", "user", 100, res) == LEAK_FALLBACK and res.llm_calls == 2 and "reasoning" in res.warnings[0]
    # a run of "!!!!" is retried too
    p = Pipeline(st, Retriever(st), FakeLLM([Reply("!" * 300), Reply("**Answer** fine")]))
    res = Result(question="q", answer="")
    assert p._llm("sys", "user", 100, res) == "**Answer** fine" and res.llm_calls == 2
    # grader calls (json_out) are left to evaluate(), which has its own retry
    p = Pipeline(st, Retriever(st), FakeLLM([Reply('analysis: hmm {"scores": [3]}')]))
    res = Result(question="q", answer="")
    assert '"scores"' in p._llm("sys", "user", 100, res, json_out=True) and res.llm_calls == 1


def test_chatlog_records_carry_a_build_id(tmp_path, monkeypatch):
    import time
    from billrag import chatlog
    monkeypatch.setenv("BILLRAG_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("R2_BUCKET", raising=False)
    assert len(chatlog.build_id()) == 8 and chatlog.build_id() == chatlog.build_id()
    chatlog.log_turn(type="ask", question="q")
    for _ in range(50):
        f = list(tmp_path.glob("*.jsonl"))
        if f and f[0].read_text().strip():
            break
        time.sleep(0.05)
    assert json.loads(f[0].read_text().splitlines()[0])["build"] == chatlog.build_id()


# ---------------------------------------------------------------- translation layer (Sinhala / Tamil)
from billrag import translate  # noqa: E402

SI_Q = "\u0db1\u0dd2\u0dc0\u0dd0\u0dbb\u0daf\u0dd2 \u0daf\u0dd2\u0db1 \u0d9a\u0dd3\u0dba\u0daf?"  # Sinhala text
TA_ANS = "\u0baa\u0ba4\u0bbf\u0ba9\u0bc8\u0ba8\u0bcd\u0ba4\u0bc1 \u0ba8\u0bbe\u0b9f\u0bcd\u0b95\u0bb3\u0bcd"  # Tamil text


class TranslatingLLM(ScriptLLM):
    def __init__(self, lang="si", drop_cites=False, **kw):
        super().__init__(**kw)
        self.lang, self.drop_cites = lang, drop_cites

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        if system.startswith("Translate the user's question"):
            self.calls.append("TO_EN")
            return Reply("How many days does the amendment allow?")
        if system.startswith("Translate the answer"):
            self.calls.append("FROM_EN")
            cite = "" if self.drop_cites else " [086/2026 \u00a72]"
            return Reply((SI_Q if self.lang == "si" else TA_ANS) + cite)
        return super().chat(system, messages, tools, max_tokens, effort)


def test_detect_language_by_script():
    assert translate.detect("How many days?") == "en" and translate.detect("Bill 077/2026 \u00a712") == "en"
    assert translate.detect(SI_Q) == "si" and translate.detect(TA_ANS) == "ta"
    assert translate.detect("Does the bill mention " + SI_Q[:3]) == "en"  # a stray character is not a language switch


def test_english_questions_make_no_translation_calls(tmp_path):
    st, llm = make_store(tmp_path), TranslatingLLM()
    r = Pipeline(st, Retriever(st), llm).ask("How many days does the amendment allow?")
    assert "TO_EN" not in llm.calls and "FROM_EN" not in llm.calls and r.lang == "en" and r.llm_calls == 2


def test_sinhala_question_is_translated_in_and_out(tmp_path):
    st, llm = make_store(tmp_path), TranslatingLLM()
    p = Pipeline(st, Retriever(st), llm)
    r = p.ask(SI_Q)
    assert llm.calls[0] == "TO_EN" and llm.calls[-1] == "FROM_EN" and r.llm_calls == 4
    assert r.lang == "si" and r.question == SI_Q and translate.detect(r.answer) == "si" and "[086/2026 \u00a72]" in r.answer
    assert "fifteen days" in r.answer_en and not r.warnings
    assert p.history[-1][0].startswith("How many days")  # follow-ups are condensed in English


def test_translation_that_alters_citations_falls_back_to_english(tmp_path):
    st = make_store(tmp_path)
    r = Pipeline(st, Retriever(st), TranslatingLLM(drop_cites=True)).ask(SI_Q)
    assert r.answer == r.answer_en and "fifteen days" in r.answer and any("translate" in w for w in r.warnings)


class AILangLLM(TranslatingLLM):
    """Also answers the one-call detect+translate prompt, like a model would."""

    def __init__(self, reply, **kw):
        super().__init__(**kw)
        self.reply = reply

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        if system.startswith("You prepare questions"):
            self.calls.append("DETECT")
            return Reply(self.reply)
        return super().chat(system, messages, tools, max_tokens, effort)


FR_Q = "Combien de jours l'amendement accorde-t-il ?"


def test_ai_mode_detects_and_translates_any_language_in_one_call(tmp_path):
    st = make_store(tmp_path)
    llm = AILangLLM('analysis first... {"language": "French", "english": "How many days does the amendment allow?"}')
    llm.lang = "fr"
    r = Pipeline(st, Retriever(st), llm, translate_mode="ai").ask(FR_Q)
    assert llm.calls[0] == "DETECT" and llm.calls[-1] == "FROM_EN" and r.lang == "french" and r.llm_calls == 4
    assert "fifteen days" in r.answer_en and r.answer not in ("", r.answer_en) and not r.warnings


def test_ai_mode_english_costs_one_extra_call_and_script_mode_none(tmp_path):
    st = make_store(tmp_path)
    reply = '{"language": "English", "english": "x"}'
    llm = AILangLLM(reply)
    r = Pipeline(st, Retriever(st), llm, translate_mode="ai").ask("How many days does the amendment allow?")
    assert r.lang == "en" and r.llm_calls == 3 and "FROM_EN" not in llm.calls
    llm2 = AILangLLM(reply)
    assert Pipeline(st, Retriever(st), llm2, translate_mode="script").ask(FR_Q).lang == "en" and "DETECT" not in llm2.calls


def test_ai_mode_unusable_detection_falls_back_to_english_with_warning(tmp_path):
    st = make_store(tmp_path)
    r = Pipeline(st, Retriever(st), AILangLLM("sorry, no json"), translate_mode="ai").ask("How many days?")
    assert r.lang == "en" and any("language detection failed" in w for w in r.warnings)


def test_off_mode_never_translates(tmp_path):
    st, llm = make_store(tmp_path), TranslatingLLM()
    r = Pipeline(st, Retriever(st), llm, translate_mode="off").ask(SI_Q)
    assert "TO_EN" not in llm.calls and r.lang == "en"


def test_settings_translate_switch(monkeypatch):
    from billrag.config import load_settings
    monkeypatch.delenv("BILLRAG_TRANSLATE", raising=False)
    assert load_settings().translate == "script"
    monkeypatch.setenv("BILLRAG_TRANSLATE", "AI")
    assert load_settings().translate == "ai"


# ---------------------------------------------------------------- dedicated translation model (BILLRAG_TRANSLATE_MODEL)
class TrModel:
    """A translation-only chat model (e.g. Gemma / Llama): answers just the two translate prompts."""

    def __init__(self, fail=False):
        self.model, self.fail, self.calls = "@cf/google/gemma-3-12b-it", fail, []

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        self.calls.append((system[:30], effort))
        if self.fail:
            raise RuntimeError("HTTP 400 model not found")
        if system.startswith("Translate the user's question"):
            return Reply("How many days does the amendment allow?")
        return Reply(SI_Q + " [086/2026 §2]")


def test_dedicated_translation_chat_model_is_used_without_reasoning_effort(tmp_path):
    st, main, tm = make_store(tmp_path), TranslatingLLM(), TrModel()
    p = Pipeline(st, Retriever(st), main, grade_effort="low", translator=translate.Translator(main, "low", model_llm=tm))
    r = p.ask(SI_Q)
    assert "TO_EN" not in main.calls and "FROM_EN" not in main.calls  # main model only graded + answered
    assert len(tm.calls) == 2 and all(e is None for _, e in tm.calls)  # no reasoning_effort sent to the translation model
    assert translate.detect(r.answer) == "si" and not r.warnings and r.llm_calls == 4
    assert r.seconds["translate_in"] < 5  # only the translation itself, not the whole pipeline


def test_translation_model_outage_falls_back_to_main_model_and_reports_reasons(tmp_path):
    st, main = make_store(tmp_path), TranslatingLLM(drop_cites=True)
    p = Pipeline(st, Retriever(st), main, translator=translate.Translator(main, model_llm=TrModel(fail=True)))
    r = p.ask(SI_Q)
    assert "TO_EN" in main.calls  # question translated by the main model after the translation model failed
    assert r.answer == r.answer_en  # main model dropped citations twice -> English shown
    w = " ".join(r.warnings)
    assert "gemma-3-12b-it: error" in w and "citations changed" in w


class FakeCF:
    def __init__(self):
        self.bodies = []

    def _post(self, model, body):
        self.bodies.append(body)
        return {"translated_text": f"<{body['target_lang']}:{body['text'].strip()}>"}


def test_m2m100_keeps_citations_bullets_and_skips_non_text():
    cf = FakeCF()
    mt = translate.M2M("@cf/meta/m2m100-1.2b", None, None, client=cf)
    out = mt.translate_keeping_cites("It is **fifteen** days [086/2026 §2(1)].\n- longer 【086/2026 §3】, then 2 [086/2026]", "en", "si")
    assert out == "<si:It is fifteen days> [086/2026 §2(1)].\n- <si:longer> [086/2026 §3] <si:, then 2> [086/2026]"
    assert translate.cites(out) == translate.cites("[086/2026 §2(1)] [086/2026 §3] [086/2026]")
    assert all("086/2026" not in b["text"] for b in cf.bodies)  # the model never sees a citation


def test_make_translator_picks_the_right_kind_of_model():
    import dataclasses
    from billrag.config import load_settings
    s = dataclasses.replace(load_settings(), chat_provider="openai", chat_model="@cf/openai/gpt-oss-120b", chat_api_key="x",
                            cf_account_id="acct", cf_api_token="tok")
    main = object()
    t = translate.make_translator(dataclasses.replace(s, translate_model=None), main)
    assert t.model_llm is None and t.mt is None and t.llm is main
    t = translate.make_translator(dataclasses.replace(s, translate_model="@cf/meta/m2m100-1.2b"), main)
    assert t.mt and t.mt.model == "@cf/meta/m2m100-1.2b" and t.model_llm is None
    t = translate.make_translator(dataclasses.replace(s, translate_model="@cf/google/gemma-3-12b-it"), main)
    assert t.model_llm.model == "@cf/google/gemma-3-12b-it" and t.mt is None


def test_translation_never_adds_bill_numbers():
    assert translate.drop_invented_refs("q", 'What criteria define a "terrorist act" in this new bill 077/2026?') == \
        'What criteria define a "terrorist act" in this new bill?'
    assert translate.drop_invented_refs("q", "What does Bill No. 77/2026 say?") == "What does Bill say?"
    assert translate.drop_invented_refs("\u0db8\u0dd9\u0db8 086/2026", "What does 086/2026 say?") == "What does 086/2026 say?"
    assert "077" not in translate.TO_EN + translate.DETECT_AND_TRANSLATE  # no example numbers for models to copy


# ---------------------------------------------------------------- official Sinhala text (parse_si, local_sections)
from billrag import parse_si  # noqa: E402

ENACT_SI = "ශ්‍රී ලංකා ප්‍රජාතාන්ත්‍රික සමාජවාදී ජනරජයේ පාර්ලිමේන්තුව විසින් මෙසේ පනවනු ලැබේ:-"


def test_clean_collapses_doubled_marks_and_drops_junk():
    assert parse_si.clean("ආරක්ෂාා කිරීමේȼ") == "ආරක්ෂා කිරීමේ"
    assert parse_si.clean("හෝ�ෝ˪") == "හෝ"                       # extracted as හෝ + junk + ෝ
    assert parse_si.clean("රාාජ්‍යයය ලංංකා දෙෙස්්") == "රාජ්‍යය ලංකා දෙස්"
    assert parse_si.clean("3(2)(ඒ) වන") == "3(2)(ඒ) වන"                     # digits/brackets untouched


def test_page_lines_drops_header_and_line_numbers_and_keeps_margin_notes():
    blocks = [(60, 39, 261, 52, "6\nධාවන ශීර්ෂය"),                         # running header (top 10%)
              (60, 76, 302, 153, "(1) මේ පනතේ වගන්තිය\nදෙවන පේළිය"),
              (49, 140, 54, 154, "5\n"),                                      # line number
              (306, 170, 355, 227, "සිංහල\nපාඨය\n")]                         # margin note right of the column
    lines, notes = parse_si.page_lines(blocks, 420, 595)
    assert lines == ["(1) මේ පනතේ වගන්තිය", "දෙවන පේළිය"] and notes == ["සිංහල පාඨය"]


def si_pages():
    return [(1, ["1. කෙටි නාමය", "2. අර්ථ නිරූපණය"], []),                    # table of contents: ignored
            (2, ["විසින් මෙසේ පනව", "නු ලැබේ:-", "1.", "1. මේ පනත ... යනුවෙන් හඳුන්වනු ලැබේ."], ["කෙටි නාමය"]),
            (3, ["2.2. (1) යම් තැනැත්තකු විසින්", "‍3. 6 වන හෝ 7 වන වගන්තිය යටතේ වරදක්"], []),
            (4, ["පළමුවන උපලේඛනය", "1. ගිවිසුම"], [])]


def test_parse_pages_handles_wrapped_enacting_clause_duplicates_digits_zwj_and_schedules():
    pages = [(p, [s.strip("‍") for s in ls], ns) for p, ls, ns in si_pages()]
    secs = parse_si.parse_pages(pages, 3)
    assert [s.no for s in secs] == [1, 2, 3]
    assert secs[0].text == "මේ පනත ... යනුවෙන් හඳුන්වනු ලැබේ." and secs[0].heading == "කෙටි නාමය"  # "1." line de-duplicated
    assert secs[1].text.startswith("(1) යම්") and secs[2].text.startswith("6 වන")           # '3. 6 වන' starts with a digit
    assert "ගිවිසුම" not in secs[2].text                                                     # schedule cut off
    assert parse_si.check(secs, 3) is None
    assert parse_si.check(secs, 4) == "found 3 sections, English has 4"
    assert parse_si.parse_pages([(1, ["1. no enacting clause"], [])], 1) == []


def test_visual_order_fonts_are_rejected():
    assert parse_si.visual_order("ෙමෙසේ පනවනු ලැෙබ්: ෙම් පනත ශී ලංකා " * 3)
    assert not parse_si.visual_order(ENACT_SI + " මේ පනත යනුවෙන් හඳුන්වනු ලැබේ")


def test_local_sections_store_roundtrip_and_fingerprint_unchanged(tmp_path):
    st = make_store(tmp_path)
    fp = st.fingerprint()
    st.replace_local("086/2026", "si", "ok", [(2, "සංශෝධනය", 2, "දින දහය වෙනුවට දින පහළොව")], "x.pdf", 2)
    assert st.local_section("086/2026", 2)["text"] == "දින දහය වෙනුවට දින පහළොව"
    assert st.local_section("086/2026", 2, "ta") is None and st.local_section("086/2026", 3) is None
    assert st.local_stats("si") == [{"status": "ok", "n": 1, "kept": 1}]
    assert st.fingerprint() == fp  # vectors.npz stays valid: local text is not part of the search index
    st.replace_local("086/2026", "si", "ok", [], "x.pdf", 2)  # re-ingest replaces, does not duplicate
    assert st.local_section("086/2026", 2) is None


class OfficialSeeingLLM(TranslatingLLM):
    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        if system.startswith("Translate the answer"):
            self.translate_out = (system, messages[-1]["content"])
        return super().chat(system, messages, tools, max_tokens, effort)


def test_sinhala_answer_translation_gets_the_official_sinhala_text(tmp_path):
    st, llm = make_store(tmp_path), OfficialSeeingLLM()
    st.replace_local("086/2026", "si", "ok", [(2, None, 2, "දින දහය වෙනුවට දින පහළොව ආදේශ කිරීම")], "x.pdf", 2)
    st.commit()
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    system, user = llm.translate_out
    assert "OFFICIAL Sinhala TEXT" in system and "දින දහය වෙනුවට දින පහළොව" in user
    assert "086/2026 §2:" in user and "[086/2026 §2]:" not in user  # reference labels are not citations
    assert r.official == [("086/2026", 2)] and "official Sinhala text used" in r.sources()[0]
    assert not r.warnings and translate.detect(r.answer) == "si"


def test_no_official_text_means_plain_translation(tmp_path):
    st, llm = make_store(tmp_path), OfficialSeeingLLM()
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert "OFFICIAL" not in llm.translate_out[0] and r.official == []


# ---------------------------------------------------------------- one-step Sinhala answer + garble check
LIVE_GARBLE = "ආන්ඩ්‍රාත්‍රාත්‍රාත්‍රාය‑අධිකාරී අපරාධය"  # from the first live Sinhala answer (2026-11-05)
SI_ANS = "සංශෝධනය මගින් දින දහය වෙනුවට දින පහළොව ආදේශ කරයි"


def test_sinhala_text_is_not_mistaken_for_punctuation_junk():
    from billrag.pipeline import degenerate
    assert not degenerate("ත්‍රස්තවාදී ක්‍රියාවක් [070/2026 §3(1)] [070/2026 §3(2)] - 2026.") and degenerate("!" * 50)


def test_garble_check_rejects_the_live_repetition_and_other_degenerate_output():
    assert "repeated text" in translate.garbled(LIVE_GARBLE)
    assert translate.check_out(SI_ANS + " " + LIVE_GARBLE + " [086/2026 §2]", "x [086/2026 §2]", "si").startswith("repeated")
    assert translate.garbled("ලලලලලල") and translate.garbled("ත" * 3 + "ා" * 6)
    assert "overlong" in translate.garbled("අ" + "කුසල" * 0 + "ම්බරස්තවාදීප්‍රාණඇපකරුවන්භීෂණකාරීත්‍රස්තවාදයපිළිබඳවරදසහඅපරාධ" * 2)
    assert "another script" in translate.check_script(SI_ANS + " 意图", "si")
    assert "another script" in translate.check_script(SI_ANS + " (උදාහරණként දඬුවම්)", "si")  # seen live 2026-11-06
    assert translate.check_script(SI_ANS + " Sri Lanka Cricket Act [072/2026] §3", "si") is None  # ASCII titles are fine
    assert "U+0DCD" in translate.garbled("මාතෘකා අනුව සමානම පෙත\u0dcdසම්")  # seen live: an unassigned Sinhala code point
    # real text passes: dotted leaders, citations, markdown, numbers, a normal answer
    for ok in (SI_ANS + " [086/2026 §2(1)(a)]", "වේලාව:......................................", "| --- | --- |", "1.1.1. 2026/2026",
               "- **දඩය** රුපියල් 100,000 [070/2026 §3]", "The Bill replaces ten days with fifteen days."):
        assert translate.garbled(ok) is None, ok


class DirectLLM(TranslatingLLM):
    """Writes the answer in Sinhala when the answer prompt asks for it (garble=True: a degenerate Sinhala answer)."""

    def __init__(self, garble=False, **kw):
        super().__init__(**kw)
        self.garble, self.direct_prompt = garble, None

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        if "LANGUAGE: the user asked in Sinhala" in system:
            self.calls.append("DIRECT")
            self.direct_prompt = messages[-1]["content"]
            return Reply(SI_ANS + (" " + LIVE_GARBLE if self.garble else "") + " [086/2026 §2].")
        return super().chat(system, messages, tools, max_tokens, effort)


def _with_official(st):
    st.replace_local("086/2026", "si", "ok", [(2, None, 2, "දින දහය වෙනුවට දින පහළොව ආදේශ කිරීම")], "x.pdf", 2)
    st.commit()


def test_sinhala_question_with_official_text_is_answered_in_sinhala_in_one_step(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.path == "direct" and "FROM_EN" not in llm.calls and llm.calls[-1] == "DIRECT"
    assert r.llm_calls == 3  # question -> English, grade, Sinhala answer
    assert "OFFICIAL SINHALA TEXT" in llm.direct_prompt and "දින දහය වෙනුවට දින පහළොව" in llm.direct_prompt
    assert SI_Q in llm.direct_prompt and "fifteen days" in llm.direct_prompt  # the user's words + the English clause
    assert r.answer.startswith(SI_ANS) and r.official == [("086/2026", 2)] and not r.warnings and r.answer_en == ""
    assert "answer_direct" in r.seconds and "translate_out" not in r.seconds


def test_garbled_direct_answer_falls_back_to_english_answer_and_translation(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM(garble=True)
    _with_official(st)
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.path == "translated" and "repeated text" in r.direct_fallback and llm.calls[-1] == "FROM_EN"
    assert "fifteen days" in r.answer_en and translate.detect(r.answer) == "si" and r.official == [("086/2026", 2)]


def test_direct_answer_with_unbacked_citation_falls_back(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    llm.chat_orig = llm.chat
    llm.chat = lambda system, messages, **kw: (Reply(SI_ANS + " [999/2026 §9].") if "LANGUAGE:" in system
                                                else llm.chat_orig(system, messages, **kw))
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.path == "translated" and "999/2026" in r.direct_fallback and "999/2026" not in r.answer


def test_direct_path_can_be_switched_off_and_english_is_unaffected(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    assert Pipeline(st, Retriever(st), llm, direct_local=False).ask(SI_Q).path == "translated" and "DIRECT" not in llm.calls
    r = Pipeline(st, Retriever(st), llm).ask("How many days does the amendment allow?")
    assert r.path == "en" and r.llm_calls == 2


def test_web_meta_logs_path_and_english_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from billrag import web
    st = make_store(tmp_path)
    app = web.App.__new__(web.App)
    app.s = SimpleNamespace(chat_model="m", answer_model=None, translate_model=None, grade_effort=None, answer_effort=None,
                            translate="script")
    app.mode, app.store, app._llm, app._retr = "pipeline", st, TranslatingLLM(), Retriever(st)
    _text, meta = app._new_responder()(SI_Q)
    assert meta["path"] == "translated" and "fifteen days" in meta["answer_en"] and meta["direct_fallback"] is None


def test_join_marks_repairs_vowel_signs_wrapped_onto_the_next_line():
    assert parse_si.join_marks("ත්‍රස්තවා\nාදී සටන්කරු\nදෙවන පේළිය") == "ත්‍රස්තවාදී සටන්කරු\nදෙවන පේළිය"
    assert parse_si.join_marks("කො\nොමිෂන් සභාව") == "කොමිෂන් සභාව" and parse_si.join_marks("නා\nාගාරය") == "නාගාරය"


def test_fix_quotes_restores_official_spelling_inside_quotations_only():
    ref = "(අ) ත්‍රස්තවාදී ක්‍රියා සිදු කිරීමේ, සැලසුම් කිරීමේ, එවැනි ක්‍රියාවලට සූදානම් වීමේ හෝ සහභාගී\nවීමේ කාර්යය සඳහා; හෝ"
    ans = ("ථ්‍රස්තවාදී සටන්කරු: “ථ්‍රස්තවාදී ක්‍රියා සිදු කිරීමේ, සැලසුම් කිරීමේ, එවැනි ක්‍රියාවලට සූදානම් වීමේ හෝ සහභාගී "
           "වීමේ කාර්යය සඳහා” [085/2026 §3] සහ “මෙය කිසිසේත් නිල පාඨයේ නැති වාක්‍යයකි” [085/2026 §3]")
    out, fixes = translate.fix_quotes(ans, [ref])
    assert len(fixes) == 1 and "“ත්‍රස්තවාදී ක්‍රියා සිදු" in out
    assert out.startswith("ථ්‍රස්තවාදී සටන්කරු")                      # text outside quotes is never touched
    assert "“මෙය කිසිසේත් නිල පාඨයේ නැති වාක්‍යයකි”" in out and translate.cites(out) == translate.cites(ans)
    assert translate.fix_quotes("“" + ref.split("\n")[0][4:] + "”", [ref])[1] == []  # exact quote: unchanged
    out, fixes = translate.fix_quotes("“ථ්‍රස්තවාදී ක්‍රියා සිදු කිරීමේ, සැලසුම් කිරීමේ … සහභාගී වීමේ කාර්යය සඳහා”", [ref])
    assert out == "“ත්‍රස්තවාදී ක්‍රියා සිදු කිරීමේ, සැලසුම් කිරීමේ … සහභාගී වීමේ කාර්යය සඳහා”" and len(fixes) == 1


def test_direct_answer_quotes_are_aligned_to_the_official_text(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    llm.chat_orig = llm.chat
    llm.chat = lambda system, messages, **kw: (Reply("නියමය “දින දහය වෙනුවට දින පහළොව ආදේශ කිරිම” වේ [086/2026 §2].")
                                                if "LANGUAGE:" in system else llm.chat_orig(system, messages, **kw))
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.path == "direct" and "“දින දහය වෙනුවට දින පහළොව ආදේශ කිරීම”" in r.answer and len(r.quote_fixes) == 1


# ---------------------------------------------------------------- "this new bill": name the best-scored bill
def _ev(key, sec, score, title, date="2026-10-01T00:00:00Z"):
    from billrag.pipeline import Evidence
    return Evidence({"bill_key": key, "section_no": sec, "kind": "section", "title": title, "bill_kind": "Bill", "date": date,
                     "page": 1, "text": f"text of {key} §{sec}"}, score)


def test_focus_bill_groups_versions_and_picks_the_best_scored_bill(tmp_path):
    st = make_store(tmp_path)
    p = Pipeline(st, Retriever(st), ScriptLLM())
    ev = [_ev("085/2026", 14, 2, "Prevention of Foreign Terrorist Fighters - Bill", "2026-10-20T00:00:00Z"),
          _ev("070/2026", 3, 3, "Protection of the State from Terrorism - GS", "2026-09-22T00:00:00Z"),
          _ev("078/2026", None, 2, "Protection of the State from Terrorism - Bill", "2026-10-06T00:00:00Z")]
    q = 'What criteria define a "terrorist act" in this new bill in 2026?'
    f = p.focus_bill(q, ev)
    assert f == {"title": "Protection of the State from Terrorism", "bills": ["070/2026", "078/2026"], "score": 3}
    assert p.focus_bill(q, ev, keys=["085/2026"]) is None                      # the question names a bill
    assert p.focus_bill("What criteria define a terrorist act?", ev) is None    # nothing unnamed to resolve
    assert p.focus_bill(q, ev[1:]) is None                                      # one bill (two versions) anyway
    # ties on the best score: more total score wins
    tie = [_ev("001/2026", 1, 3, "A - Bill"), _ev("002/2026", 1, 3, "B - Bill"), _ev("002/2026", 2, 2, "B - Bill")]
    assert p.focus_bill("what does this bill say", tie)["bills"] == ["002/2026"]


def test_focus_note_reaches_the_answer_prompt_and_evidence_is_reordered(tmp_path, monkeypatch):
    st, llm = make_store(tmp_path), ScriptLLM()
    p = Pipeline(st, Retriever(st), llm)
    other = _ev("070/2026", 3, 2, "Protection of the State from Terrorism - GS")
    real = p.build_evidence
    monkeypatch.setattr(p, "build_evidence", lambda graded: [other] + real(graded))  # a lookalike bill listed first
    r = p.ask("How many days does this new bill allow?")
    assert r.focus["title"] == "Trusts (Amendment)" and "086/2026" in r.focus["bills"] and r.focus["score"] == 3
    assert r.evidence[0].key != "070/2026" and r.evidence[-1].key == "070/2026"
    assert "best-matching evidence is from Trusts (Amendment) (" in llm.calls[-1]
    r2 = p.ask("How many days does the Trusts amendment allow?")
    assert r2.focus is None and "best-matching" not in llm.calls[-1]


def test_cut_off_replies_are_rejected_in_both_sinhala_paths(tmp_path):
    from billrag.llm import Reply as R
    assert R("x", [], "length").truncated and R("x", [], "max_tokens").truncated and not R("x", [], "stop").truncated
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    orig = llm.chat

    def chat(system, messages, **kw):
        r = orig(system, messages, **kw)
        return R(r.text, [], "length") if ("LANGUAGE:" in system or system.startswith("Translate the answer")) else r
    llm.chat = chat
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.direct_fallback == "cut off at the output limit; cut off at the output limit"  # tried twice
    assert r.direct_rejected.startswith(SI_ANS)
    assert r.answer == r.answer_en and any("cut off" in w for w in r.warnings)  # translation cut off too -> English


class OverviewOnlyLLM(DirectLLM):
    """Grades only bill-level candidates as useful, like a 'tell me about the X bill' question."""

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        user = messages[-1]["content"]
        if "CANDIDATES:" in user:
            cands = re.findall(r"^\[(\d+)\] (.*?)(?=\n\n\[\d+\] |\Z)", user.split("CANDIDATES:")[1].strip(), re.S | re.M)
            return Reply(json.dumps({"scores": [{"i": int(i), "s": 3 if b.startswith("[BILL-LEVEL]") else 0} for i, b in cands],
                                     "enough": True, "missing": "", "queries": []}))
        return super().chat(system, messages, tools, max_tokens, effort)


def test_bill_level_only_evidence_gets_the_bills_sections_for_a_sinhala_question(tmp_path):
    st, llm = make_store(tmp_path), OverviewOnlyLLM()
    _with_official(st)
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.added_sections and all(e.key in ("086/2026", "081/2026") for e in r.added_sections)
    assert 2 in [e.section for e in r.added_sections] and r.path == "direct" and ("086/2026", 2) in r.official
    assert "OFFICIAL SINHALA TEXT" in llm.direct_prompt and "bill_sections" in r.seconds
    # English questions are unchanged: bill-level evidence stays bill-level
    r2 = Pipeline(st, Retriever(st), OverviewOnlyLLM()).ask("How many days does the amendment allow?")
    assert r2.added_sections == [] and all(e.section is None for e in r2.evidence)


def test_direct_answer_is_retried_once_before_falling_back(tmp_path):
    st, llm = make_store(tmp_path), DirectLLM()
    _with_official(st)
    replies = iter([SI_ANS + " 枠 [086/2026 §2].", SI_ANS + "\u200b\u200b [086/2026 §2]."])  # seen live: a Japanese character
    orig = llm.chat
    llm.chat = lambda system, messages, **kw: (Reply(next(replies)) if "LANGUAGE:" in system else orig(system, messages, **kw))
    r = Pipeline(st, Retriever(st), llm).ask(SI_Q)
    assert r.path == "direct" and r.answer.startswith(SI_ANS) and "枠" not in r.answer and "\u200b" not in r.answer
    assert r.direct_fallback.startswith("retried after: characters from another script") and r.llm_calls == 4


def test_unverified_sinhala_quotes_lose_their_quotation_marks():
    ref = "පාලක ජනරාල්වරයා විසින්, ස්වකීය පූර්ණ අභිමතය මත වීසා පත්‍රය අවලංගු කරනු ලැබිය හැකි ය."
    ans = ("“පාලක ජනරාල්වරයා විසින්, ස්වකීය පූර්ණ අභිමතය මත” [071/2026 §26]; “පනත නව නීතිමය රාජ්‍ය පද්ධතියක් සෑදේ” [071/2026]; "
           "“Immigration Act” සහ “ක්‍රිකට් පනත … වෙනත් පාඨයක් නොවේ කිසිසේත්”")
    out, fixes = translate.fix_quotes(ans, [ref], code="si")
    assert "“පාලක ජනරාල්වරයා විසින්, ස්වකීය පූර්ණ අභිමතය මත”" in out          # verbatim: kept
    assert "; පනත නව නීතිමය රාජ්‍ය පද්ධතියක් සෑදේ [071/2026]" in out           # paraphrase: marks removed
    assert "“Immigration Act”" in out and "“ක්‍රිකට්" not in out                # English quote untouched
    assert sum(1 for _, o in fixes if o is None) == 2 and translate.cites(out) == translate.cites(ans)
    assert translate.fix_quotes(ans, [ref])[0] == ans                            # without code: nothing is unquoted


# ---------------------------------------------------------------- glossary (titles + defined terms, no model)
from billrag import glossary  # noqa: E402


def test_title_and_term_pairs_from_parallel_sections():
    assert glossary.title_pair("This Act may be cited as the Protection of the State from Terrorism Act, No. of 2026.",
                               "මේ පනත 2026 අංක දරන ත්‍රස්තවාදයෙන් රාජ්‍යය ආරක්ෂා කිරීමේ පනත යනුවෙන් හඳුන්වනු ලැබේ.") == \
        ("Protection of the State from Terrorism Act", "ත්‍රස්තවාදයෙන් රාජ්‍යය ආරක්ෂා කිරීමේ පනත")
    assert glossary.title_pair("this act may be cited as the appropriation act, no.... of 2025.",
                               "මේ පනත 2025 අංක ........ දරන විසර්ජන පනත යනු‍වෙ න් හදුන්වනු ලැබේ.") == ("Appropriation Act", "විසර්ජන පනත")
    en = ('In this Act, unless the context otherwise requires- “bank” means a licensed bank; “written law” includes any Act; '
          'the Trusts Ordinance (hereinafter referred to as the “principal enactment”) is amended')
    si = ('පද සම්බන්ධයෙන් අන්‍යාර්ථයක් අවශ්‍ය වුවහොත් මිස, “බැංකුව” යන්නෙන් බලපත්‍රලාභී බැංකුවක් අදහස් වේ; “ලිඛිත\nනීතිය” යන්නට '
          'ඕනෑම පනතක් ඇතුළත් වේ; (මෙහි මින් මතු “ප්‍රධාන ප්‍රඥප්තිය” යනුවෙන් සඳහන් කරනු ලබන) භාර ආඥාපනත')
    assert glossary.term_pairs(en, si) == [("defined", "bank", "බැංකුව"), ("defined", "written law", "ලිඛිත නීතිය"),
                                           ("referred", "principal enactment", "ප්‍රධාන ප්‍රඥප්තිය")]
    assert glossary.term_pairs("“bank” means x; “person” means y", "“බැංකුව” යන්නෙන් x") == []  # unequal counts: no guessing


def test_double_ya_artefact_fixed_only_when_the_clean_bills_say_so():
    clean = {"අමාත්‍යවරයා", "රාජ්‍යය"}
    assert glossary.fix_double_ya("අමාත්‍යයවරයා විසින්", clean) == "අමාත්‍යවරයා විසින්"
    assert glossary.fix_double_ya("රාජ්‍යය", clean) == "රාජ්‍යය"              # a real ්‍යය stays
    assert glossary.fix_double_ya("ජාත්‍යයන්තර", clean) == "ජාත්‍යයන්තර"      # no evidence either way: unchanged


def _glossary_store(tmp_path):
    st = make_store(tmp_path)
    st.replace_local("086/2026", "si", "ok", [(2, None, 2, "දින දහය වෙනුවට දින පහළොව ආදේශ කිරීම")], "x.pdf", 2)
    st.replace_glossary("si", [("title", "Trusts (Amendment) Act", "භාර (සංශෝධන) පනත", 2, "[]", None),
                               ("defined", "fifteen days", "දින පහළොව", 3, "[]", None),
                               ("defined", "bank", "බැංකුව", 9, "[]", None)])
    st.commit()
    return st


def test_glossary_lookups_for_questions_and_evidence(tmp_path):
    g = glossary.Glossary(_glossary_store(tmp_path))
    assert g.in_question("භාර (සංශෝධන) පනත් කෙටුම්පතේ දින පහළොව ගැන කියන්න") == [
        ("භාර (සංශෝධන)", "Trusts (Amendment)", "title"), ("දින පහළොව", "fifteen days", "defined")]
    assert g.in_question("බැංකුව") == []                                       # one-word terms are too generic for hints
    assert g.for_english("The Bill replaces ten days with Fifteen Days.") == [("fifteen days", "දින පහළොව")]
    assert g.for_english("a bank account") == [("bank", "බැංකුව")]  # one word, but in 9 bills
    assert "- fifteen days = දින පහළොව" in glossary.terms_block(g.for_english("fifteen days")) and "[" not in \
        glossary.terms_block(g.for_english("fifteen days"))


class HintLLM(DirectLLM):
    def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
        if system.startswith("Translate the user's question"):
            self.to_en_prompt = messages[-1]["content"]
            self.calls.append("TO_EN")
            return Reply("How many days does the amendment allow?")  # the bill's name got lost in translation
        return super().chat(system, messages, tools, max_tokens, effort)


def test_glossary_hints_reach_translate_in_and_terms_reach_the_answer(tmp_path):
    st, llm = _glossary_store(tmp_path), HintLLM()
    q = "භාර (සංශෝධන) පනත් කෙටුම්පත දින කීයක් ලබා දෙයිද?"
    r = Pipeline(st, Retriever(st), llm).ask(q)
    assert "භාර (සංශෝධන) = Trusts (Amendment)" in llm.to_en_prompt
    assert r.queries[0].endswith("(Trusts (Amendment))")  # the dropped title is put back for retrieval
    assert r.glossary_hints == ["භාර (සංශෝධන) = Trusts (Amendment)"] and r.path == "direct"
    assert "OFFICIAL SINHALA TERMS" in llm.direct_prompt and "fifteen days = දින පහළොව" in llm.direct_prompt
    assert r.glossary_terms == ["fifteen days"]


def test_glossary_build_on_a_tiny_index(tmp_path):
    st = make_store(tmp_path)
    st.replace_local("086/2026", "si", "ok", [(1, None, 1, "මේ පනත 2026 අංක දරන භාර (සංශෝධන) පනත යනුවෙන් හඳුන්වනු ලැබේ.")], "x", 2)
    st.commit()
    fp = st.fingerprint()
    stats = glossary.build(st)
    assert stats["title"] == 1 and [(r["kind"], r["en"], r["local"]) for r in st.glossary()] == [
        ("title", "Trusts (Amendment) Act", "භාර (සංශෝධන) පනත")]
    assert st.fingerprint() == fp  # the glossary lives outside chunks: vectors.npz stays valid


def test_resources_snapshot_and_usage(tmp_path):
    from billrag import resources
    snap = resources.snapshot()
    assert snap["cpu_s"] >= 0 and snap["peak_rss_mb"] > 0 and snap["uptime_s"] >= 0
    with resources.Usage() as u:
        sum(i * i for i in range(200_000))
        inside = u.fields()  # logging from a finally block happens before __exit__
    f = u.fields()
    assert f["cpu_s"] >= 0 and f["peak_rss_mb"] > 0 and set(inside) == set(f)
    # cgroup v2 files, v1 fallback, "max" = no limit, missing directory = nothing
    (tmp_path / "memory.current").write_text("104857600\n")
    (tmp_path / "memory.max").write_text("max\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 2500000\nnr_throttled 3\nthrottled_usec 400000\n")
    (tmp_path / "cpu.max").write_text("25000 100000\n")
    assert resources.cgroup(str(tmp_path)) == {"mem_mb": 100.0, "usage_s": 2.5, "nr_throttled": 3, "throttled_s": 0.4,
                                               "cpu_limit": 0.25}
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "memory.limit_in_bytes").write_text(str(1 << 30))
    assert resources.cgroup(str(tmp_path))["mem_limit_mb"] == 1024.0
    assert resources.cgroup(str(tmp_path / "nope")) == {}


def test_web_stats_endpoint_and_usage_in_log(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag import web
    monkeypatch.setenv("BILLRAG_LOG_DIR", str(tmp_path))
    app = web.App.__new__(web.App)
    app.lock, app.mode, app._retr, app.secret = threading.Lock(), "pipeline", None, "s3cret-s3cret-s3cret"
    app.sessions, app.turns = web.OrderedDict(), {}
    app._new_responder = lambda: (lambda q: ("ok", {}))
    assert app.ask("s1", "hi") == "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/stats"
    try:
        try:
            urllib.request.urlopen(url)
            raise AssertionError("stats must need the proxy secret")
        except urllib.error.HTTPError as e:
            assert e.code == 403
        with urllib.request.urlopen(urllib.request.Request(url, headers={"X-Proxy-Secret": app.secret})) as r:
            st = json.loads(r.read())
    finally:
        srv.shutdown()
    assert st["sessions"] == 1 and st["turns"] == 1 and st["busy"] is False and st["rss_mb"] is not None
    assert st["build"] and st["max_sessions"] == web.MAX_SESSIONS
    for _ in range(50):  # the log write is on a background thread
        files = list(tmp_path.glob("*.jsonl"))
        if files and files[0].read_text().strip():
            break
        __import__("time").sleep(0.05)
    rec = json.loads(files[0].read_text().splitlines()[0])
    assert rec["usage"]["cpu_s"] >= 0 and rec["usage"]["rss_mb"] > 0


def test_usage_summary_for_read_chatlogs():
    from billrag.resources import usage_summary
    recs = [{"usage": {"cpu_s": 1.0, "cpu_pct": 10, "rss_mb": 120.0, "peak_rss_mb": 150.0}},
            {"usage": {"cpu_s": 3.0, "cpu_pct": 30, "rss_mb": 130.0, "peak_rss_mb": 160.0}}, {"question": "old record"}]
    lines = usage_summary(recs)
    assert "2 records" in lines[0] and "cpu_s" in lines[0] and "max 3 s" in lines[0] and "peak_rss_mb" in lines[1]
    assert "no CPU/memory" in usage_summary([{"question": "q"}])[0]


# ---------------------------------------------------------------- per-call tracing (calltrace)
def test_calltrace_records_only_inside_a_trace_and_restores_the_outer_one():
    from billrag import calltrace
    calltrace.record("chat", 1.0, model="m")  # no trace: no-op
    with calltrace.tracing() as outer:
        calltrace.step("a")
        calltrace.record("chat", 0.5, model="m", out=10, retries=None)
        with calltrace.tracing() as inner:
            calltrace.record("bm25", 0.01)
        calltrace.record("ai_run", 0.2, n=3)
    assert [c["kind"] for c in outer.calls] == ["chat", "ai_run"] and [c["kind"] for c in inner.calls] == ["bm25"]
    assert outer.calls[0]["step"] == "a" and "retries" not in outer.calls[0] and outer.calls[0]["out"] == 10
    assert calltrace._active.get() is None
    assert calltrace.usage_fields({"prompt_tokens": 5, "completion_tokens": 7,
                                   "completion_tokens_details": {"reasoning_tokens": 3}}) == {"in": 5, "out": 7, "reasoning": 3}
    assert calltrace.usage_fields(type("U", (), {"input_tokens": 4, "output_tokens": 2})()) == {"in": 4, "out": 2}
    rows = calltrace.summary([[{"step": "answer_direct", "kind": "chat", "model": "m", "s": 10.0, "out": 500, "retries": 1},
                               {"step": "retrieve0", "kind": "ai_run", "model": "r", "s": 1.0, "attempts": 2}],
                              [{"step": "answer_direct", "kind": "chat", "model": "m", "s": 30.0, "out": 600}], None])
    assert rows[0][:4] == ("answer_direct", "chat", "m", 2) and rows[0][5] == 30.0 and rows[0][8] == 1
    assert rows[1][0] == "retrieve0" and rows[1][8] == 1  # a second attempt counts as a retry


def test_openai_adapter_records_tokens_and_sdk_retries():
    from billrag import calltrace
    from billrag.llm import OpenAILLM

    class Resp:
        choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None}), "finish_reason": "stop"})()]
        usage = {"prompt_tokens": 100, "completion_tokens": 40, "completion_tokens_details": {"reasoning_tokens": 25}}

    class Raw:
        retries_taken = 2

        def parse(self):
            return Resp()

    class Client:
        class chat:
            class completions:
                class with_raw_response:
                    @staticmethod
                    def create(**kw):
                        return Raw()

    llm = object.__new__(OpenAILLM)
    llm.client, llm.model = Client, "gpt-x"
    with calltrace.tracing() as tr:
        calltrace.step("evaluate0")
        assert llm.chat("s", [{"role": "user", "content": "q"}], max_tokens=900, effort="low").text == "ok"
    c = tr.calls[0]
    assert (c["kind"], c["step"], c["model"], c["in"], c["out"], c["reasoning"], c["retries"], c["stop"], c["effort"]) == \
        ("chat", "evaluate0", "gpt-x", 100, 40, 25, 2, "stop", "low")


def test_cloudflare_post_records_attempts(monkeypatch):
    import io
    import urllib.error
    from billrag import calltrace, llm as L
    tries = []

    def fake_urlopen(req, timeout=60):
        tries.append(1)
        if len(tries) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"result": {"response": []}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    rr = L.CloudflareReranker("@cf/rr", "acc", "tok")
    with calltrace.tracing() as tr:
        rr.scores("q", ["a", "b"])
    assert tr.calls[0]["kind"] == "ai_run" and tr.calls[0]["attempts"] == 2 and tr.calls[0]["n"] == 2


def test_web_log_carries_calls_with_pipeline_steps(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from billrag import calltrace, web
    monkeypatch.setenv("BILLRAG_LOG_DIR", str(tmp_path / "logs"))
    st = make_store(tmp_path)

    class TracedLLM(TranslatingLLM):
        def chat(self, system, messages, **kw):
            r = super().chat(system, messages, **kw)
            calltrace.record("chat", 0.01, model="fake", out=len(r.text))
            return r

    app = web.App.__new__(web.App)
    app.s = SimpleNamespace(chat_model="m", answer_model=None, translate_model=None, grade_effort=None, answer_effort=None,
                            translate="script")
    app.mode, app.store, app._llm, app._retr = "pipeline", st, TracedLLM(), Retriever(st)
    app.lock, app.sessions, app.turns = threading.Lock(), web.OrderedDict(), {}
    app.ask("s1", SI_Q)
    for _ in range(50):
        files = list((tmp_path / "logs").glob("*.jsonl"))
        if files and files[0].read_text().strip():
            break
        __import__("time").sleep(0.05)
    calls = json.loads(files[0].read_text().splitlines()[0])["calls"]
    steps = {(c["kind"], c["step"]) for c in calls}
    assert ("bm25", "retrieve0") in steps and ("chat", "evaluate0") in steps
    assert ("chat", "translate_in") in steps and ("chat", "translate_out") in steps


# ---------------------------------------------------------------- streaming (/api/ask/stream)
def _streaming(cls):
    """A fake model class that also streams its reply in small pieces when asked (on_token)."""
    class S(cls):
        def chat(self, system, messages, tools=(), max_tokens=0, effort=None, on_token=None):
            r = super().chat(system, messages, tools, max_tokens, effort)
            if on_token:
                self.streamed = getattr(self, "streamed", 0) + 1
                for i in range(0, len(r.text), 5):
                    on_token(r.text[i:i + 5])
            return r
    return S


def _collect(p, question):
    ev = []
    p.emit = lambda k, d: ev.append((k, d))
    res = p.ask(question)
    return res, ev


def test_english_answer_streams_steps_then_the_answer_text(tmp_path):
    st = make_store(tmp_path)
    res, ev = _collect(Pipeline(st, Retriever(st), _streaming(ScriptLLM)()), "How many days does the amendment allow?")
    steps = [d for k, d in ev if k == "status"]
    assert steps[:3] == ["condense", "retrieve0", "evaluate0"] and steps[-1] == "summarise"
    assert ev.index(("reset", None)) > ev.index(("status", "summarise"))  # a fresh draft for the answer call
    assert "".join(d for k, d in ev if k == "token") == res.answer  # only the answer call is streamed, not grading


def test_leaked_reasoning_is_never_streamed(tmp_path):
    st = make_store(tmp_path)

    class Leaky(ScriptLLM):
        def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
            r = super().chat(system, messages, tools, max_tokens, effort)
            if "CANDIDATES:" not in messages[-1]["content"] and "FOLLOW-UP:" not in messages[-1]["content"]:
                n = getattr(self, "n", 0)
                self.n = n + 1
                if n == 0:
                    return Reply("analysis: the user wants the number of days, I should look at section 2 ...")
            return r

    res, ev = _collect(Pipeline(st, Retriever(st), _streaming(Leaky)()), "How many days does the amendment allow?")
    toks = "".join(d for k, d in ev if k == "token")
    assert "analysis" not in toks and toks == res.answer and "fifteen days" in res.answer  # retry streamed, leak hidden
    assert sum(1 for k, _ in ev if k == "reset") == 2


def test_sinhala_direct_answer_streams_but_translated_answer_does_not(tmp_path):
    st = make_store(tmp_path)
    _with_official(st)
    res, ev = _collect(Pipeline(st, Retriever(st), _streaming(DirectLLM)()), SI_Q)
    assert res.path == "direct" and "".join(d for k, d in ev if k == "token").startswith(SI_ANS[:10])
    assert ("status", "answer_direct") in ev
    st2 = make_store(tmp_path / "b")  # no official text -> English answer + translation: steps only, no English draft
    res2, ev2 = _collect(Pipeline(st2, Retriever(st2), _streaming(TranslatingLLM)()), SI_Q)
    assert res2.path == "translated" and not [d for k, d in ev2 if k == "token"]
    assert ("status", "translate_out") in ev2


def test_openai_adapter_streams_visible_text_and_records_first_token():
    from billrag import calltrace
    from billrag.llm import OpenAILLM
    D = lambda **kw: type("D", (), {"content": None, **kw})()
    chunks = [type("K", (), {"choices": [type("C", (), {"delta": D(reasoning_content="thinking"), "finish_reason": None})()], "usage": None})(),
              type("K", (), {"choices": [type("C", (), {"delta": D(content="Hello "), "finish_reason": None})()], "usage": None})(),
              type("K", (), {"choices": [type("C", (), {"delta": D(content="world"), "finish_reason": "stop"})()], "usage": None})(),
              type("K", (), {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 4}})()]
    seen = {}

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    seen.update(kw)
                    return iter(chunks)

    llm = object.__new__(OpenAILLM)
    llm.client, llm.model = Client, "m"
    pieces = []
    with calltrace.tracing() as tr:
        r = llm.chat("s", [{"role": "user", "content": "q"}], effort="low", on_token=pieces.append)
    assert r.text == "Hello world" and pieces == ["Hello ", "world"] and r.stop == "stop" and seen["stream"] is True
    c = tr.calls[0]
    assert c["stream"] is True and c["out"] == 4 and c["in"] == 9 and isinstance(c["first"], float)


def test_web_stream_endpoint_sends_events_then_the_final_answer():
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler

    class StubApp:
        secret = None

        def ask(self, sid, q, emit=None):
            if q == "boom":
                raise RuntimeError("model down")
            emit("status", "retrieve0")
            emit("token", "dra")
            emit("token", "ft")
            return "final answer"

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubApp()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def events(q):
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/ask/stream",
                                     json.dumps({"question": q}).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert r.headers["Content-Type"].startswith("text/event-stream")
            raw = r.read().decode()
        out = []
        for block in raw.strip().split("\n\n"):
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            out.append((lines["event"], json.loads(lines["data"])))
        return out

    try:
        assert events("q") == [("status", {"step": "retrieve0"}), ("token", {"text": "dra"}), ("token", {"text": "ft"}),
                               ("done", {"answer": "final answer"})]
        assert events("boom") == [("error", {"error": "RuntimeError: model down"})]
    finally:
        srv.shutdown()


def test_answer_mode_from_the_page_sets_only_the_answer_effort(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from billrag import web
    assert web.answer_mode({"answer": "fast"}) == "fast" and web.answer_mode({"answer": "high"}) is None
    assert web.answer_mode({}) is None and web.answer_mode({"answer": ["fast"]}) is None
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    st = make_store(tmp_path)

    def app_with(grade_effort, answer_effort):
        llm = ScriptLLM()
        app = web.App.__new__(web.App)
        app.s = SimpleNamespace(chat_model="m", answer_model=None, translate_model=None, grade_effort=grade_effort,
                                answer_effort=answer_effort, translate="script")
        app.mode, app.store, app._llm, app._retr = "pipeline", st, llm, Retriever(st)
        app.lock, app.sessions, app.turns = threading.Lock(), web.OrderedDict(), {}
        return app, llm

    app, llm = app_with("low", None)
    app.ask("s", "How many days does the amendment allow?", mode="fast")
    assert llm.efforts[-1] == "low" and llm.efforts[0] == "low"  # answer call low; grading keeps its own (low) effort
    app.ask("s", "How many days does the amendment allow?", mode="thorough")
    assert llm.efforts[-1] is None  # server default (model default)
    app.ask("s", "How many days does the amendment allow?")
    assert llm.efforts[-1] is None
    app, llm = app_with(None, None)  # server says: this model takes no reasoning effort -> fast must not send one
    app.ask("s", "How many days does the amendment allow?", mode="fast")
    assert llm.efforts[-1] is None
    _text, meta = app_with("low", "medium")[0]._new_responder()("How many days does the amendment allow?", mode="fast")
    assert meta["answer_mode"] == "fast" and meta["answer_effort"] == "low"


def test_tests_never_upload_chat_logs_to_r2():
    from billrag import chatlog
    assert chatlog._r2() is None and not os.environ.get("R2_BUCKET")


# ---- bill metadata (billrag/meta.py): families, newest instance, bill type, cross-references
def _meta_store(tmp_path):
    from billrag.store import Store
    s = Store(tmp_path / "m.sqlite")
    rows = [("010/2025", 2025, "2025-01-05T00:00:00.000Z", "Appropriation", "appropriation", "Bill"),
            ("069/2026", 2026, "2026-09-01T00:00:00.000Z", "Appropriation", "appropriation", "Bill"),
            ("070/2026", 2026, "2026-09-22T00:00:00.000Z", "Customs (Amendment) - GS", "customs amendment", "GS"),
            ("078/2026", 2026, "2026-10-06T00:00:00.000Z", "Customs (Amendment) - Bill", "customs amendment", "Bill")]
    for key, y, d, title, tn, kind in rows:
        s.replace_bill({"key": key, "bill_no": 1, "year": y, "date": d, "title": title, "title_norm": tn, "kind": kind,
                        "long_title": None, "presented_by": None, "presented_on": None, "gazette_date": None,
                        "printed_no": None, "n_pages": 1, "n_sections": 2, "pdf_path": None, "summary": None},
                       [{"kind": "section", "section_no": 1, "text": "1. Short title.", "ctx": "short title"},
                        {"kind": "section", "section_no": 2, "text": "2. A penalty under section 1 and sections 2 and 1 applies.",
                         "ctx": "penalty"}])
    s.commit()
    return s


def test_meta_families_latest_type_and_refs(tmp_path):
    from billrag import meta
    s = _meta_store(tmp_path)
    out = meta.build(s)
    assert out["bills"] == 4
    assert meta.meta(s, "010/2025")["is_latest"] == 0 and meta.meta(s, "069/2026")["is_latest"] == 1
    assert meta.meta(s, "010/2025")["bill_type"] == "appropriation"
    gs, bill = meta.meta(s, "070/2026"), meta.meta(s, "078/2026")
    assert gs["family"] == bill["family"] and gs["stage"] == "gazette" and bill["stage"] == "bill"
    assert bill["bill_type"] == "amendment" and bill["amends"] == "customs"
    assert meta.keys_where(s, bill_type="appropriation", latest_only=True) == ["069/2026"]
    assert meta.keys_where(s) is None
    assert meta.latest_family_keys(s, "appropriation") == ["069/2026"]
    assert meta.refs_from(s, "069/2026", 2) == [1]          # "section 1", "sections 2 and 1": own section 2 is ignored
    assert meta.refs_in("sections 4 and 5, 7", 4, {4, 5, 7}) == {5, 7}


def test_meta_tags_survive_rebuild(tmp_path):
    from billrag import meta
    s = _meta_store(tmp_path)
    meta.build(s)
    s.db.execute("UPDATE bill_meta SET tags=? WHERE bill_key='069/2026'", ('["public finance"]',))
    meta.build(s)
    assert meta.keys_where(s, tag="public finance") == ["069/2026"]


def test_meta_missing_tables_are_tolerated(tmp_path):
    from billrag import meta
    from billrag.store import Store
    s = Store(tmp_path / "old.sqlite")
    assert meta.keys_where(s, latest_only=True) is None and meta.meta(s, "x") is None and meta.refs_from(s, "x", 1) == []


def test_pipeline_follows_section_refs(tmp_path):
    from billrag import meta
    from billrag.pipeline import Evidence, Pipeline
    s = _meta_store(tmp_path)
    meta.build(s)
    p = Pipeline(s, None, None)
    ch = s.chunks_by_ids([r[0] for r in s.db.execute("SELECT id FROM chunks WHERE bill_key='069/2026' AND section_no=2")])
    ev = [Evidence(next(iter(ch.values())), 3)]
    got = p.follow_section_refs(ev)
    assert [(e.key, e.section) for e in got] == [("069/2026", 1)] and got[0].score == 2
    assert p.follow_section_refs(ev + got) == []          # already in the evidence: nothing added


# ---- which sections an amending instrument changes (meta.amended_sections, Pipeline.amendment_list)
def test_amendment_of_reads_only_the_opening_words():
    from billrag.meta import amendment_of
    assert amendment_of("2. Section 8 of the principal enactment is hereby amended as follows") == ("amended", "section 8", None)
    assert amendment_of("Section 16 A of the principal enactment is hereby amended") == ("amended", "section 16A", None)
    assert amendment_of("Section 6 b of the principal enactment is hereby amended") == ("amended", "section 6B", None)
    assert amendment_of("Se ction 3D of the principal enactment is hereby amended") == ("amended", "section 3D", None)
    assert amendment_of("Sections 5 and 6 of the principal enactment are hereby repealed and the following sections substituted "
                        "therefor") == ("replaced", "sections 5 and 6", None)
    assert amendment_of("Section 81 of the principal enactment is hereby repealed.") == ("repealed", "section 81", None)
    assert amendment_of("Section 2 of the Inland Revenue Act, No. 24 of 2017 (hereinafter referred to as the “principal "
                        "enactment”) is hereby amended") == ("amended", "section 2", None)   # that IS the principal enactment
    assert amendment_of("Section 3 of the Nation Building Tax Act, No. 9 of 2009 is hereby further amended") == \
        ("amended", "section 3", "Nation Building Tax Act, No. 9 of 2009")
    assert amendment_of("The following new section is hereby inserted immediately after section 52 of the principal enactment, "
                        "and shall have effect as section 52A of that enactment: - 52A. (1)") == ("inserted", "section 52A", None)
    assert amendment_of("The following new Chapter is hereby inserted immediately after section 185 of the principal enactment, "
                        "and shall have effect as CHAPTER XVIIA of that enactment") == ("inserted", "CHAPTER XVIIA", None)
    assert amendment_of("Article 154F of the Constitution is hereby amended by the deletion") == ("amended", "Article 154F", "Constitution")
    assert amendment_of("The Second Schedule to the principal enactment is hereby amended") == ("amended", "Second Schedule", None)
    # a clause that only MENTIONS a section changes nothing
    assert amendment_of("(1) The Commissioner-General shall write off interest under section 157 of the principal enactment") is None
    assert amendment_of("This Act may be cited as the Tax (Amendment) Act, No. of 2026.") is None
    # wordings and misprints seen in 2020-2026 bills
    assert amendment_of("Secti on 23w of the principal enactment is hereby amended") == ("amended", "section 23W", None)
    assert amendment_of("Section 54B of the principal enactment is herby amended by") == ("amended", "section 54B", None)
    assert amendment_of("Section 26 of the principal enactment is hereby amend- ed by the insertion") == ("amended", "section 26", None)
    assert amendment_of("(1) Section 1 of the Personal Data Protection Act, No. 9 of 2022 (hereinafter referred to as the "
                        "“principal enactment”) is hereby amended as follows") == ("amended", "section 1", None)
    assert amendment_of("The Civil Procedure Code (Chapter 101) is hereby amended by the repeal of section 205 thereof and substitution "
                        "therefor of the following section") == ("replaced", "section 205", "Civil Procedure Code (Chapter 101)")
    assert amendment_of("The Constitution of the Democratic Socialist Republic of Sri Lanka (hereinafter referred to as the "
                        "“Constitution”) is hereby amended by- (a) amendment of Article 155G by the")[:2] == ("amended", "Article 155G")
    assert amendment_of("The heading appearing immediately before section 2 of Part I of the principal enactment is hereby repealed "
                        "and the following heading is substituted therefor") == \
        ("replaced", "heading immediately before section 2 of Part I", None)
    assert amendment_of("The following new division is hereby inserted immediately after section 130 (SHARE REGISTER) of the principal "
                        "enactment and shall have effect as a new division of that enactment") == \
        ("inserted", "new division after section 130", None)
    assert amendment_of("The sections of the principal enactment speci ﬁ ed in Column I of the Schedule hereto, are hereby amended")[1] \
        == "the sections listed in its Schedule"
    assert amendment_of("It is hereby declared that the amendments made to sections 7, 13, and 20 a shall not apply") is None


def test_margin_notes_on_the_last_clause_are_not_flagged_as_unread():
    from billrag.meta import margin_targets
    last = ("In the event of any inconsistency between the Sinhala and Tamil texts of this Act, the Sinhala text shall prevail. "
            "Amendment of section 478A of the principal enactment. Amendment of section 485 of the principal enactment.")
    assert margin_targets(last) == {"478A", "485"}
    assert margin_targets("Section 8 of the principal enactment is hereby amended") is None


def _amending_store(tmp_path):
    from billrag import meta
    from billrag.store import Store
    s = Store(tmp_path / "am.sqlite")
    clauses = ["This Act may be cited as the Tax (Amendment) Act, No. of 2026.",
               "Section 8 of the Tax Act, No. 1 of 2017 (hereinafter referred to as the “principal enactment”) is hereby amended "
               "by the substitution for the words “ten” of the words “twenty”.",
               "The following new section is hereby inserted immediately after section 52 of the principal enactment, and shall have "
               "effect as section 52A of that enactment: - 52A. A credit is allowed.",
               "Section 16 A of the principal enactment is hereby repealed and the following section is substituted therefor: - 16A. Text.",
               "The First Schedule to the principal enactment is hereby amended in paragraph 1.",
               "The Commissioner-General shall write off interest under section 157 of the principal enactment.",
               "In the event of any inconsistency between the Sinhala and Tamil texts of this Act, the Sinhala text shall prevail. "
               "Amendment of section 90 of the principal enactment."]   # a clause the parser merged with the next one
    s.replace_bill({"key": "090/2026", "bill_no": 90, "year": 2026, "date": "2026-10-01T00:00:00.000Z", "title": "Tax (Amendment) - Bill",
                    "title_norm": "tax amendment", "kind": "Bill", "long_title": "to amend the Tax Act", "presented_by": None,
                    "presented_on": None, "gazette_date": None, "printed_no": None, "n_pages": 3, "n_sections": len(clauses),
                    "pdf_path": None, "summary": None},
                   [{"kind": "overview", "section_no": None, "text": "Bill 090/2026 — Tax (Amendment) - Bill (Bill, 2026-10-01). "
                     "Long title: to amend the Tax Act.", "ctx": "overview"}] +
                   [{"kind": "section", "section_no": i + 1, "text": f"{i + 1}. {t}", "ctx": "clause"} for i, t in enumerate(clauses)])
    s.commit()
    meta.build(s)
    return s


def test_amended_sections_table_and_principal(tmp_path):
    from billrag import meta
    s = _amending_store(tmp_path)
    got = [(r["section_no"], r["action"], r["target"]) for r in meta.amended(s, "090/2026")]
    assert got == [(2, "amended", "section 8"), (3, "inserted", "section 52A"), (4, "replaced", "section 16A"),
                   (5, "amended", "First Schedule")]
    assert meta.principal_of(s, "090/2026") == "Tax Act, No. 1 of 2017"
    assert meta.amended(s, "nope") == []


def test_list_question_gets_the_complete_list_and_its_citations_verify(tmp_path):
    from billrag.pipeline import LIST_NOTE, asks_amended
    from billrag.retrieve import Retriever
    s = _amending_store(tmp_path)
    for q in ("Which sections did the Tax (Amendment) Act, No. 11 of 2026 amend?", "which sections it amended",
              "Which sections of the Tax Act, No. 1 of 2017 were amended by bill 090/2026?", "list all sections amended by it",
              "what does the Tax (Amendment) bill amend?"):
        assert asks_amended(q), q
    for q in ("summarise bill 090/2026", "what is the penalty under section 4", "which section deals with bail?",
              "what changes does the bill make to section 83A",
              # detail questions from evals/retrieval_eval.jsonl that an earlier pattern took for list questions
              "Under the proposed amendment to the Trusts Ordinance, which part of Section 3 is being removed and how is the "
              "definition of “express trust” being changed?",
              "From which assessment date are deductions previously claimed under sections 32, 33 or 34 of the repealed Act excluded?"):
        assert not asks_amended(q), q
    llm = ScriptLLM(cite="[090/2026 §4] [090/2026]")
    r = Pipeline(s, Retriever(s), llm, on_event=lambda n, d: None).ask("Which sections does bill 090/2026 amend?")
    assert r.amend_list == "090/2026" and r.enough
    lst = r.evidence[0].chunk
    assert lst["kind"] == "amendments"
    text = lst["text"]
    assert "7 clauses, of which 4 change the Tax Act, No. 1 of 2017" in text
    assert "section 8 (§2)" in text and "section 52A (§3)" in text and "section 16A (§4)" in text
    assert "157" not in text.split("Clauses that do not")[0]         # mentioned, not amended
    assert "§1, §6" in text and "could not be read automatically" in text
    assert "§7 (its margin notes name section(s) 90" in text     # a margin note names an amendment no clause was read as making
    assert LIST_NOTE.strip()[:40] in llm.calls[-1]
    assert r.warnings == []                                          # §4 is cited from the list: not an unsupported citation
    assert any("list of amending clauses" in x for x in r.sources())
    plain = Pipeline(s, Retriever(s), ScriptLLM(cite="[090/2026]")).ask("What does section 8 become under bill 090/2026?")
    assert plain.amend_list == "" and all(e.chunk["kind"] != "amendments" for e in plain.evidence)


# ---- prompt echo guard, "which bill?" clarification, per-call cost
def test_glossary_block_echo_is_rejected():
    from billrag import translate
    leak = ("පිළිතුර.\n\nනිල සිංහල පද (මෙම වචන භාවිත කරන්න):\n- Board = මණ්ඩලය\n- Minister = අමාත්‍යවරයා\n"
            "- appointed members = පත් කළ සාමාජිකයන්")
    assert "glossary" in translate.check_script(leak, "si")
    assert translate.echoes_prompt("OFFICIAL SINHALA TERMS used in the bills' own Sinhala text")
    assert not translate.echoes_prompt("- මණ්ඩලය [077/2026 §4]\n- ගාස්තුව 5 = 6")   # one list-like line is not an echo


def test_vague_bill_question_is_asked_back(tmp_path):
    from billrag import resolve
    s = _meta_store(tmp_path)
    for q in ("Tell me about the new bill.", "tell me about this bill", "What is the newest act?", "Explain this act please"):
        assert resolve.is_vague_bill_question(q), q
    for q in ("Tell me about the new Cricket bill", "Tell me about the 2026 bill", "Summarise 078/2026", "hello",
              "How long can a person be detained?"):
        assert not resolve.is_vague_bill_question(q), q
    r = resolve.resolve_question(s, "Tell me about the new bill")
    assert r["action"] == "reply" and r.get("clarify") and "069/2026" in r["text"] and "Customs" in r["text"]
    # inside a running chat "this bill" refers to an earlier turn: not asked back
    assert resolve.resolve_question(s, "Tell me about this bill", has_history=True)["action"] == "answer"


def test_call_cost_uses_cache_and_peak_rates():
    from datetime import datetime, timezone
    from billrag import calltrace
    off, peak = datetime(2026, 11, 7, 12, tzinfo=timezone.utc), datetime(2026, 11, 6, 8, tzinfo=timezone.utc)  # Sat / Fri 08:00 UTC
    assert not calltrace.is_peak(off) and calltrace.is_peak(peak)
    usd = calltrace.cost_usd("deepseek-flash", 1_000_000, 1_000_000, 500_000, when=off)
    assert abs(usd - (500_000 * 0.003 + 500_000 * 0.15 + 1_000_000 * 0.6) / 1e6) < 1e-9
    assert abs(calltrace.cost_usd("deepseek-flash", 1000, 100, 0, when=peak)
               - 2 * calltrace.cost_usd("deepseek-flash", 1000, 100, 0, when=off)) < 1e-12
    assert calltrace.cost_usd("@cf/openai/gpt-oss-20b", 1000, 100) is None        # unpriced model: tokens only
    assert calltrace.usage_fields({"prompt_tokens": 40, "completion_tokens": 61, "prompt_cache_hit_tokens": 8,
                                   "completion_tokens_details": {"reasoning_tokens": 56}}) == \
        {"in": 40, "out": 61, "cache_hit": 8, "reasoning": 56}
    with calltrace.tracing() as tr:
        calltrace.record("chat", 1.0, model="deepseek-flash", **{"in": 100, "out": 10, "cache_hit": 0})
        calltrace.record("chat", 1.0, model="@cf/x", **{"in": 100, "out": 10})
    c = calltrace.cost_summary(tr.calls)
    assert c["chat_calls"] == 2 and c["priced_calls"] == 1 and c["usd"] > 0 and c["in"] == 200


def test_cost_prices_can_be_overridden_by_env(monkeypatch):
    from datetime import datetime, timezone
    from billrag import calltrace
    monkeypatch.setenv("BILLRAG_PRICES", '{"my-model": {"hit": 0.1, "miss": 1.0, "out": 2.0}}')
    off = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
    assert abs(calltrace.cost_usd("my-model", 1_000_000, 500_000, 0, when=off) - 2.0) < 1e-9


def test_pipeline_follows_references_back_to_a_penalty_section(tmp_path):
    from billrag import meta
    from billrag.pipeline import Evidence, Pipeline
    from billrag.store import Store
    s = Store(tmp_path / "pen.sqlite")
    for key, d in (("070/2026", "2026-09-22T00:00:00.000Z"), ("444/2024", "2024-05-01T00:00:00.000Z")):
        s.replace_bill({"key": key, "bill_no": 1, "year": int(key[-4:]), "date": d, "title": "Terror - Bill", "title_norm": "terror",
                        "kind": "Bill", "long_title": None, "presented_by": None, "presented_on": None, "gazette_date": None,
                        "printed_no": None, "n_pages": 1, "n_sections": 3, "pdf_path": None, "summary": None},
                       [{"kind": "section", "section_no": 3, "text": "3. Hostage taking is a consequence listed in subsection (2).", "ctx": "offence"},
                        {"kind": "section", "section_no": 4, "text": "4. A person who commits an offence under section 3 shall be liable to imprisonment not exceeding twenty years and a fine.", "ctx": "penalty"},
                        {"kind": "section", "section_no": 5, "text": "5. This Act binds the State; see section 3.", "ctx": "other"}])
    s.commit()
    meta.build(s)
    p = Pipeline(s, None, None)

    def ev(key, no):
        i = s.db.execute("SELECT id FROM chunks WHERE bill_key=? AND section_no=?", (key, no)).fetchone()[0]
        return Evidence(s.chunks_by_ids([i])[i], 3)
    got = p.follow_section_refs([ev("070/2026", 3)], "What is the penalty for hostage taking?")
    assert [(e.key, e.section) for e in got] == [("070/2026", 4)]              # §4 points at §3 and speaks of imprisonment; §5 does not
    assert p.follow_section_refs([ev("070/2026", 3)], "Who is hostage taking defined by?") == []   # not a penalty question
    other = p.follow_section_refs([ev("070/2026", 3), ev("444/2024", 3)], "What is the penalty?")
    assert {e.key for e in other} == {"070/2026"}                              # an older bill's sections are not pulled in


def test_translate_in_prompts_ask_for_literal_translation():
    from billrag import translate
    for prompt in (translate.TO_EN, translate.DETECT_AND_TRANSLATE):
        assert "when you know it" not in prompt                    # that wording made the model invent real-world Act titles
        assert "Translate literally" in prompt and "NEVER add a bill number" in prompt


# ---------------------------------------------------------------- concurrency: gate, queue, per-conversation lock
def test_gate_limits_queues_in_order_and_rejects():
    from billrag.gate import Busy, Gate
    g = Gate(slots=2, max_queue=2, max_wait=5)
    assert g.acquire()[0] == 0.0 and g.acquire()[1] == 2
    order, positions, errors = [], [], []

    def waiter(n):
        try:
            g.acquire(on_wait=lambda p: positions.append((n, p)))
            order.append(n)
        except Busy as e:
            errors.append(str(e))

    ts = []
    for n in (1, 2):
        t = threading.Thread(target=waiter, args=(n,))
        t.start()
        ts.append(t)
        for _ in range(100):  # let it join the line before the next one, so arrival order is defined
            if g.counts()["queued"] == n:
                break
            __import__("time").sleep(0.01)
    with pytest.raises(Busy):  # third waiter: the queue (2) is full
        g.acquire()
    c = g.counts()
    assert (c["running"], c["queued"], c["max_concurrent"], c["max_queue"], c["rejected"]) == (2, 2, 2, 2, 1)
    g.release()
    g.release()
    for t in ts:
        t.join(5)
    assert order == [1, 2] and not errors
    assert (1, 1) in positions and (2, 2) in positions


def test_gate_gives_up_after_max_wait():
    from billrag.gate import Busy, Gate
    g = Gate(slots=1, max_queue=3, max_wait=0.2)
    g.acquire()
    with pytest.raises(Busy):
        g.acquire()
    assert g.counts()["queued"] == 0 and g.counts()["rejected"] == 1


def _concurrent_app(responder, **env):
    from billrag import web
    app = web.App.__new__(web.App)
    app.lock, app.sessions, app.turns = threading.Lock(), web.OrderedDict(), {}
    app.mode, app._retr = "pipeline", None
    app._new_responder = lambda: responder
    return app


def test_web_questions_overlap_but_stay_capped(monkeypatch):
    from billrag import web
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    monkeypatch.setenv("BILLRAG_MAX_CONCURRENT", "3")
    state = {"now": 0, "peak": 0}
    mu = threading.Lock()

    def slow(q, **kw):
        with mu:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        __import__("time").sleep(0.15)
        with mu:
            state["now"] -= 1
        return "ok:" + q, {}

    app = _concurrent_app(slow)
    results = []
    ts = [threading.Thread(target=lambda i=i: results.append(app.ask(f"s{i}", f"q{i}"))) for i in range(7)]
    t0 = __import__("time").time()
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    took = __import__("time").time() - t0
    assert sorted(results) == sorted(f"ok:q{i}" for i in range(7))
    assert state["peak"] == 3          # overlapped, but never above the cap
    assert took < 7 * 0.15             # faster than one at a time
    st = app.stats()
    assert st["running"] == 0 and st["queued"] == 0 and st["max_concurrent"] == 3 and st["busy"] is False


def test_web_one_question_per_conversation_at_a_time(monkeypatch):
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    state = {"now": 0, "peak": 0}
    mu = threading.Lock()

    def slow(q, **kw):
        with mu:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        __import__("time").sleep(0.1)
        with mu:
            state["now"] -= 1
        return q, {}

    app = _concurrent_app(slow)
    ts = [threading.Thread(target=app.ask, args=("same", f"q{i}")) for i in range(3)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert state["peak"] == 1 and app.turns["same"] == 3


def test_web_busy_is_503_and_streams_queue_positions(monkeypatch):
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from billrag import web
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    monkeypatch.setenv("BILLRAG_MAX_CONCURRENT", "1")
    monkeypatch.setenv("BILLRAG_MAX_QUEUE", "1")
    release = threading.Event()

    def blocked(q, **kw):
        release.wait(10)
        return "done:" + q, {}

    app = _concurrent_app(blocked)
    app._ensure()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(path, sid):
        return urlopen(Request(url + path, json.dumps({"session": sid, "question": "x"}).encode(),
                               {"Content-Type": "application/json"}), timeout=15)

    try:
        out = {}
        t1 = threading.Thread(target=lambda: out.setdefault("a", post("/api/ask", "a").read()))
        t1.start()
        for _ in range(200):
            if app._gate.counts()["running"] == 1:
                break
            __import__("time").sleep(0.01)
        t2 = threading.Thread(target=lambda: out.setdefault("b", post("/api/ask/stream", "b").read().decode()))
        t2.start()
        for _ in range(200):
            if app._gate.counts()["queued"] == 1:
                break
            __import__("time").sleep(0.01)
        with pytest.raises(HTTPError) as e:  # slot taken, queue full
            post("/api/ask", "c")
        assert e.value.code == 503 and e.value.headers["Retry-After"] and json.loads(e.value.read())["busy"] is True
        release.set()
        t1.join(10)
        t2.join(10)
        assert json.loads(out["a"])["answer"] == "done:x"
        assert "event: queue" in out["b"] and '"position": 1' in out["b"] and "event: done" in out["b"]
    finally:
        release.set()
        srv.shutdown()


def test_calltrace_is_per_thread_and_follows_copied_context():
    import contextvars
    from billrag import calltrace
    seen = {}

    def turn(name, delay):
        with calltrace.tracing() as tr:
            calltrace.step(name)
            __import__("time").sleep(delay)
            calltrace.record("chat", 0.1, model=name)
            ctx = contextvars.copy_context()
            t = threading.Thread(target=lambda: ctx.run(calltrace.record, "ai_run", 0.1, model=name + "-sub"))
            t.start()
            t.join()
        seen[name] = [(c["step"], c["model"]) for c in tr.calls]

    ts = [threading.Thread(target=turn, args=(n, d)) for n, d in (("a", 0.05), ("b", 0.0))]
    [t.start() for t in ts]
    [t.join(5) for t in ts]
    assert seen["a"] == [("a", "a"), ("a", "a-sub")] and seen["b"] == [("b", "b"), ("b", "b-sub")]


def test_gate_public_lane_cannot_take_member_capacity():
    from billrag.gate import Busy, Gate
    g = Gate(slots=3, max_queue=5, max_wait=5, public_slots=1, public_queue=1)
    g.acquire(lane="public")                      # the one public slot
    waited = []
    t = threading.Thread(target=lambda: waited.append(g.acquire(lane="public")))  # next public: queues
    t.start()
    for _ in range(100):
        if g.counts()["public_queued"] == 1:
            break
        __import__("time").sleep(0.01)
    with pytest.raises(Busy):                     # public queue (1) is full
        g.acquire(lane="public")
    g.acquire(lane="member")
    g.acquire(lane="member")                      # members still get the other two slots
    c = g.counts()
    assert c["running"] == 3 and c["public_running"] == 1 and c["public_queued"] == 1
    g.release("member")                           # a free slot, but the public lane is at its own cap: it keeps waiting
    __import__("time").sleep(0.2)
    assert g.counts()["public_queued"] == 1 and not waited
    g.release("public")                           # now the queued public request may start
    t.join(5)
    assert waited and g.counts()["public_running"] == 1


def test_web_public_header_sets_the_lane_and_anon_chats_are_evicted_first(monkeypatch):
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from billrag import web
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    monkeypatch.setattr(web, "MAX_SESSIONS", 2)
    seen = []
    app = _concurrent_app(lambda q, **kw: ("ok", {}))
    real_ask = app.ask
    app.ask = lambda s, q, **kw: (seen.append(kw.get("lane")), real_ask(s, q, **kw))[1]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/api/ask"
    try:
        for lane, sid in ((None, "member1"), ("public", "anon:a"), ("member", "anon:b-spoof-no"), (None, "member2")):
            hdr = {"Content-Type": "application/json", **({"X-Lane": lane} if lane else {})}
            urlopen(Request(url, json.dumps({"session": sid, "question": "x"}).encode(), hdr), timeout=10).read()
    finally:
        srv.shutdown()
    assert seen == [None, "public", None, None]          # only the exact value "public" counts
    assert list(app.sessions) == ["member1", "member2"]   # the anonymous chat went first


# ---------------------------------------------------------------- "answer that in Tamil" follow-ups + machine-translation note
def test_language_switch_only_when_the_message_just_names_a_language():
    sw = translate.language_switch
    assert sw("In tamil") == "ta" and sw("can you say this in Tamil?") == "ta" and sw("english please") == "en"
    assert sw("sinhalen kiyanna") == "si" and sw("\u0ba4\u0bae\u0bbf\u0bb4\u0bbf\u0bb2\u0bcd") == "ta"
    assert sw("\u0db4\u0dd2\u0dc5\u0dd2\u0dad\u0dd4\u0dbb \u0dc3\u0dd2\u0d82\u0dc4\u0dbd\u0dd9\u0db1\u0dca \u0daf\u0dd9\u0db1\u0dca\u0db1") == "si"
    for q in ("tamil bill", "Is English an official language?", "what does the tamil nadu bill say", "english or tamil",
              "in tamil what is the penalty for hostage taking", "How many days?", "", "in"):
        assert sw(q) is None, q


def test_machine_note_only_for_machine_translated_answers():
    assert translate.machine_note("ta", "translated", TA_ANS) == translate.MACHINE_NOTE["ta"]
    assert translate.machine_note("ta", "language_switch", TA_ANS)
    assert translate.machine_note("ta", "translated", "English text (translation failed)") is None
    assert translate.machine_note("si", "direct", SI_Q) is None      # written from the official Sinhala text
    assert translate.machine_note("en", "en", "English") is None


def test_in_tamil_after_an_english_answer_rerenders_without_searching(tmp_path):
    st, llm = make_store(tmp_path), TranslatingLLM(lang="ta")
    p = Pipeline(st, Retriever(st), llm)
    first = p.ask("How many days does the amendment allow?")
    n = len(llm.calls)
    r = p.ask("in tamil")
    assert llm.calls[n:] == ["FROM_EN"]                      # one translation call: no grading, no search, no condense
    assert r.path == "language_switch" and r.lang == "ta" and translate.detect(r.answer) == "ta"
    assert "[086/2026 \u00a72]" in r.answer and r.answer_en == first.answer and not r.warnings
    assert [e.key for e in r.evidence] == [e.key for e in first.evidence]
    back = p.ask("english")
    assert back.answer == first.answer and llm.calls[n:] == ["FROM_EN"]   # English needs no call at all
    assert p.ask("how many days does the amendment allow?").path == "en"  # a real question is searched again


def test_language_switch_needs_a_previous_answer_and_survives_a_failed_translation(tmp_path):
    st = make_store(tmp_path)
    llm = TranslatingLLM(lang="ta")
    p = Pipeline(st, Retriever(st), llm)
    p.ask("in tamil")                                         # nothing to re-render yet: an ordinary (searched) question
    assert p.last.path != "language_switch"
    st2, llm2 = make_store(tmp_path), TranslatingLLM(lang="ta", drop_cites=True)
    p2 = Pipeline(st2, Retriever(st2), llm2)
    first = p2.ask("How many days does the amendment allow?")
    r = p2.ask("in tamil")
    assert r.answer == first.answer and any("translate" in w for w in r.warnings)   # English kept, with a warning


def test_web_adds_the_machine_translation_note_to_translated_answers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from billrag import web
    monkeypatch.setenv("BILLRAG_CHATLOG", "0")
    st = make_store(tmp_path)
    app = web.App.__new__(web.App)
    app.s = SimpleNamespace(chat_model="m", answer_model=None, translate_model=None, grade_effort=None, answer_effort=None,
                            translate="script")
    app.mode, app.store, app._llm, app._retr = "pipeline", st, TranslatingLLM(lang="ta"), Retriever(st)
    app.lock, app.sessions, app.turns = threading.Lock(), web.OrderedDict(), {}
    ta = app.ask("s", TA_ANS + " " + TA_ANS + " " + TA_ANS)
    assert "*" + translate.MACHINE_NOTE["ta"] + "*" in ta and ta.index(translate.MACHINE_NOTE["ta"]) < ta.index("Sources:")
    en = app.ask("s2", "How many days does the amendment allow?")
    assert translate.MACHINE_NOTE["ta"] not in en and translate.MACHINE_NOTE["si"] not in en


# ---------------------------------------------------------------- coverage line + "Recently added" list
def test_store_coverage_counts_versions_once_and_lists_recent_bills_by_plain_title(tmp_path):
    st = make_store(tmp_path)                                  # a presented Bill (086/2026) and its Gazette text (081/2026)
    c = st.coverage()
    assert c["bills"] == 1 and c["first"] == "2026-10-14" and c["last"] == "2026-10-21"
    assert c["recent"] == [{"key": "086/2026", "title": "Trusts (Amendment)", "date": "2026-10-21"}]
    st.db.execute("CREATE TABLE bill_meta (bill_key TEXT, family TEXT)")   # with metadata, versions are grouped by family
    st.db.executemany("INSERT INTO bill_meta VALUES (?, ?)", [("086/2026", "081/2026"), ("081/2026", "081/2026")])
    assert st.coverage()["bills"] == 1 and len(st.coverage(recent=5)["recent"]) == 1
    st.db.execute("DELETE FROM bill_meta WHERE bill_key = '081/2026'")
    assert st.coverage()["bills"] == 2                         # 081 now stands alone
    assert [r["key"] for r in st.coverage()["recent"]] == ["086/2026", "081/2026"]


def test_healthz_carries_coverage_and_never_fails_because_of_it():
    import urllib.request
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler

    class Good:
        def coverage(self):
            return {"bills": 2, "first": "2003-07-07", "last": "2026-10-21", "recent": []}

    class Broken:
        def coverage(self):
            raise RuntimeError("db gone")

    for app, want in ((Good(), {"ok": True, "coverage": Good().coverage()}), (Broken(), {"ok": True})):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/healthz") as r:
                assert json.loads(r.read()) == want
        finally:
            srv.shutdown()


def test_web_app_computes_coverage_once(tmp_path):
    from billrag import web
    st = make_store(tmp_path)
    app = web.App.__new__(web.App)
    app.store = st
    first = app.coverage()
    st.db.execute("DELETE FROM bills")                          # a later change in the index is not re-read: computed once per process
    assert app.coverage() is first and first["bills"] == 1


# ---------------------------------------------------------------- no "GS" jargon for users or the answering model
def test_version_tag_uses_plain_words():
    from billrag.pipeline import version_tag
    gs = {"title": "The Goodness Foundation (Incorporation) - GS", "bill_kind": "GS", "date": "2026-10-16T00:00:00Z"}
    bill = {"title": "Trusts (Amendment) - Bill", "bill_kind": "Bill", "date": "2026-10-21T00:00:00Z"}
    act = {"title": "Appropriation", "bill_kind": "Other", "date": "2026-10-07T00:00:00Z"}
    assert version_tag(gs) == "The Goodness Foundation (Incorporation) (Gazette text, 2026-10-16)"
    assert version_tag(bill) == "Trusts (Amendment) (Bill, 2026-10-21)"
    assert version_tag(act) == "Appropriation (2026-10-07)"


def test_gazette_text_is_never_called_gs_in_sources_or_prompts(tmp_path):
    class AllRelevant(ScriptLLM):
        def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
            user = messages[-1]["content"]
            if "CANDIDATES:" in user:
                self.calls.append(user)
                n = len(re.findall(r"^\[\d+\] ", user.split("CANDIDATES:")[1], re.M))
                return Reply(json.dumps({"scores": [{"i": i, "s": 3} for i in range(1, n + 1)], "enough": True, "missing": "", "queries": []}))
            return super().chat(system, messages, tools, max_tokens, effort)

    st, llm = make_store(tmp_path), AllRelevant()
    r = Pipeline(st, Retriever(st), llm).ask("How many days does the amendment allow?")
    assert any(e.key == "081/2026" for e in r.evidence)               # the Gazette text (kind GS) was used
    shown = "\n".join(r.sources() + llm.calls)
    assert "Gazette text" in shown and "(GS" not in shown and "- GS" not in shown
    assert any("(Gazette text, 2026-10-14)" in s for s in r.sources())


def test_shown_text_rewrites_only_the_stored_overview_header():
    from billrag.pipeline import shown_text
    head = "Bill 084/2026 — The Goodness Foundation (Incorporation) - GS (GS, 2026-10-16). Summary (auto-generated): x - GS (GS, 2026-01-01)"
    out = shown_text({"kind": "overview", "text": head})
    assert out.startswith("Bill 084/2026 — The Goodness Foundation (Incorporation) (Gazette text, 2026-10-16). Summary")
    assert out.endswith("x - GS (GS, 2026-01-01)")                      # only the header, once
    assert shown_text({"kind": "overview", "text": "Bill 086/2026 — Trusts (Amendment) - Bill (Bill, 2026-10-21). S"}) \
        == "Bill 086/2026 — Trusts (Amendment) (Bill, 2026-10-21). S"
    assert shown_text({"kind": "overview", "text": "Bill 080/2026 — Appropriation (Other, 2026-10-07). S"}) \
        == "Bill 080/2026 — Appropriation (2026-10-07). S"
    clause = {"kind": "section", "text": "see — X - GS (GS, 2026-10-16) in the Act"}
    assert shown_text(clause) == clause["text"]                          # clause text is never touched


# ---------------------------------------------------------------- backup model (llm.FallbackLLM)
class _Failing:
    model = "main"

    def __init__(self, error, after_tokens=()):
        self.error, self.after_tokens, self.calls = error, after_tokens, 0

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None, on_token=None):
        self.calls += 1
        for piece in self.after_tokens:
            on_token(piece)
        if self.error:
            raise self.error
        return llm_mod().Reply("from main")


class _Backup:
    model = "backup"

    def __init__(self):
        self.seen = []

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None, on_token=None):
        self.seen.append({"effort": effort, "stream": on_token is not None})
        if on_token:
            on_token("from backup")
        return llm_mod().Reply("from backup")


def llm_mod():
    from billrag import llm
    return llm


class _Status(Exception):
    def __init__(self, code):
        self.status_code = code


def _fallback(error, **kw):
    clock = [100.0]
    main, backup = _Failing(error, **kw), _Backup()
    return llm_mod().FallbackLLM(main, backup, clock=lambda: clock[0]), main, backup, clock


def test_fallback_not_used_while_the_main_model_works():
    f, main, backup, _ = _fallback(None)
    assert f.chat("s", []).text == "from main" and not backup.seen and f.model == "main"


def test_no_credit_answers_on_the_backup_and_leaves_the_main_model_alone():
    from billrag import calltrace
    f, main, backup, clock = _fallback(_Status(402))
    with calltrace.tracing() as tr:
        assert f.chat("s", [], effort="off").text == "from backup"
        assert f.chat("s", [], effort=None).text == "from backup"
    assert main.calls == 1  # the second call did not spend time on a provider known to be out of credit
    assert [b["effort"] for b in backup.seen] == ["low", None]  # "reasoning off" is not sent as-is to another provider
    assert calltrace.fallback_summary(tr.calls) == {"calls": 2, "model": "backup", "reason": "_Status 402"}
    clock[0] += 299
    f.chat("s", [])
    assert main.calls == 1
    clock[0] += 2
    f.chat("s", [])
    assert main.calls == 2  # tried again after the cool-down


def test_short_outage_retries_sooner_and_one_bad_request_stops_nothing():
    f, main, backup, clock = _fallback(_Status(503))
    f.chat("s", [])
    clock[0] += 61
    f.chat("s", [])
    assert main.calls == 2
    f, main, backup, clock = _fallback(_Status(400))
    f.chat("s", [])
    f.chat("s", [])
    assert main.calls == 2


def test_a_stream_that_fails_at_the_start_continues_as_a_stream_and_one_that_fails_midway_does_not():
    got = []
    f, main, backup, _ = _fallback(ConnectionError("x"))
    assert f.chat("s", [], on_token=got.append).text == "from backup"
    assert got == ["from backup"] and backup.seen[0]["stream"]
    got = []
    f, main, backup, _ = _fallback(ConnectionError("x"), after_tokens=["half an "])
    assert f.chat("s", [], on_token=got.append).text == "from backup"
    assert got == ["half an "] and not backup.seen[0]["stream"]  # the checked `done` answer replaces the draft


def test_both_models_failing_is_reported_as_a_failure():
    import pytest
    f, main, backup, _ = _fallback(_Status(500))
    backup.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("both down"))
    with pytest.raises(RuntimeError):
        f.chat("s", [])


def test_make_llm_wraps_only_when_a_different_backup_is_configured():
    import dataclasses
    from billrag.config import load_settings
    from billrag.llm import FallbackLLM, OpenAILLM, make_llm
    s = dataclasses.replace(load_settings(), chat_provider="openai", chat_model="m", chat_base_url="http://x.test", chat_api_key="k")
    assert isinstance(make_llm(s), OpenAILLM)
    assert isinstance(make_llm(dataclasses.replace(s, fallback_model="m")), OpenAILLM)
    f = make_llm(dataclasses.replace(s, fallback_model="b", fallback_base_url="http://y.test", fallback_api_key="k2"))
    assert isinstance(f, FallbackLLM) and f.primary.client.max_retries == 1 and f.backup.model == "b"


def _serve_web(app):
    import threading
    from http.server import ThreadingHTTPServer
    from billrag.web import make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def _post(url, path, lane=False):
    import json
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url + path, data=json.dumps({"question": "q"}).encode(), method="POST",
                                 headers={"X-Lane": "public"} if lane else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_visitors_never_see_provider_errors_but_members_do():
    from billrag.web import PUBLIC_ERROR

    class App:
        secret = None

        def ask(self, *a, **k):
            raise RuntimeError("Error code: 402 - Insufficient Balance (request_id: abc)")

    srv, url = _serve_web(App())
    try:
        for path in ("/api/ask", "/api/ask/stream"):
            status, body, _ = _post(url, path, lane=True)
            assert "402" not in body and PUBLIC_ERROR in body, path
            assert "402" in _post(url, path)[1], path
    finally:
        srv.shutdown()


def test_a_reply_that_is_only_an_apology_is_marked_for_the_worker():
    from billrag.pipeline import UNUSABLE

    class App:
        secret = None
        reply = "Sorry.\n\n⚠ " + UNUSABLE

        def ask(self, *a, **k):
            return self.reply

    srv, url = _serve_web(App())
    try:
        assert _post(url, "/api/ask")[2].get("X-Unanswered") == "1"
        assert "event: unanswered" in _post(url, "/api/ask/stream")[1]
        App.reply = "A real answer."
        assert "X-Unanswered" not in _post(url, "/api/ask")[2]
        assert "event: unanswered" not in _post(url, "/api/ask/stream")[1]
    finally:
        srv.shutdown()


def test_turns_that_cost_nothing_are_marked_free_for_the_worker():
    from types import SimpleNamespace as R
    from billrag import web

    greeting = R(clarify=False, path="en", evidence=[], llm_calls=0, queries=[])
    assert web.free_turn(greeting)
    assert web.free_turn(R(clarify=True, path="en", evidence=[1], llm_calls=2, queries=["q"]))      # "which bill did you mean?"
    assert web.free_turn(R(clarify=False, path="language_switch", evidence=[1], llm_calls=1, queries=[]))
    assert not web.free_turn(R(clarify=False, path="en", evidence=[1], llm_calls=2, queries=["q"]))  # a real answer
    assert not web.free_turn(R(clarify=False, path="en", evidence=[], llm_calls=2, queries=["q"]))   # searched, found nothing: it cost us

    class App:
        secret = None

        def ask(self, *a, **k):
            web._TL.free = self.free
            return "hello"

    App.free = True
    srv, url = _serve_web(App())
    try:
        assert _post(url, "/api/ask")[2].get("X-Free") == "1"
        assert "event: free" in _post(url, "/api/ask/stream")[1]
        App.free = False
        assert "X-Free" not in _post(url, "/api/ask")[2]
        assert "event: free" not in _post(url, "/api/ask/stream")[1]
    finally:
        srv.shutdown()


def test_questions_overlapping_on_one_store_do_not_break_sqlite(tmp_path):
    """Found by the load test: one connection shared by threads gave InterfaceError / IndexError on 11 of 24 questions at 4-8 people."""
    import threading
    from billrag.store import Store
    store = Store(tmp_path / "i.sqlite")
    for n in range(40):
        store.replace_bill({"key": f"{n:03d}/2026", "bill_no": n, "year": 2026, "date": "2026-01-01T00:00:00.000Z", "title": f"Bill {n}", "title_norm": f"bill {n}",
                            "kind": "Bill"},
                           [{"kind": "section", "section_no": s, "page": 1, "text": f"terror offence penalty clause {s}", "ctx": f"terror offence penalty clause {s}"} for s in range(1, 30)])
    store.commit()
    errors = []

    def work():
        try:
            for _ in range(60):
                assert store.bm25("terror offence penalty", 20)
                assert store.bill("001/2026")
                assert store.section_text("001/2026", 3)
        except Exception as e:  # noqa: BLE001 - collected for the assertion in the main thread
            errors.append(repr(e))
    threads = [threading.Thread(target=work) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


def _refresh():
    import importlib.util
    spec = importlib.util.spec_from_file_location("refresh_bills", ROOT_FOR_SCRIPTS / "scripts/refresh_bills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refresh_finds_only_new_documents_with_a_pdf(tmp_path):
    import json
    r = _refresh()
    (tmp_path / "bills/pdfs/2026").mkdir(parents=True)
    (tmp_path / "acts/pdfs/2026").mkdir(parents=True)
    bills = [dict(billNo=n, billSubNo=2026, billNoText=f"{n:03d}/2026", date="2026-10-01T00:00:00.000Z", descriptionEnglish=f"Bill  {n}") for n in (1, 2, 3)]
    (tmp_path / "bills/bills.json").write_text(json.dumps(bills))
    for n in (1, 2):
        (tmp_path / f"bills/pdfs/2026/{n:03d}_2026_ENGLISH.pdf").write_bytes(b"%PDF")
    acts = [dict(actNo=5, actSubNo=2026, actNoText="5/2026", date="2026-10-01T00:00:00.000Z", descriptionEnglish="An Act"),
            dict(actNo=0, actSubNo=2026, actNoText="0/2026", date="2026-10-01T00:00:00.000Z", descriptionEnglish="Unnumbered"),
            dict(actNo=9, actSubNo=2020, actNoText="9/2020", date="2020-10-01T00:00:00.000Z", descriptionEnglish="Too old")]
    (tmp_path / "acts/acts.json").write_text(json.dumps(acts))
    (tmp_path / "acts/pdfs/2026/5_2026_ENGLISH.pdf").write_bytes(b"%PDF")
    out = r.new_documents(tmp_path, {"001/2026"})
    assert out == {"bills": {"002/2026": "Bill 2"}, "acts": {"Act 5/2026": "An Act"}}  # 001 is known, 003 has no PDF


def test_refresh_refuses_to_publish_when_something_was_lost_or_cannot_be_found():
    r = _refresh()
    before = {"keys": {"a", "b"}, "chunks": {1, 2}, "summaries": {"a"}}
    assert r.check_nothing_lost(before, {"keys": {"a", "b", "c"}, "chunks": {1, 2, 3}, "summaries": {"a", "c"}}) == []
    bad = r.check_nothing_lost(before, {"keys": {"a"}, "chunks": {1, 3}, "summaries": set()})
    assert len(bad) == 3 and "1 documents" in bad[0]
    assert r.check_findable(lambda t, k: k == "x", {"x": "X Bill", "y": "Y Bill"}) == ["y is not found by its own title: Y Bill"]


def test_only_new_ingest_keeps_existing_chunk_ids(tmp_path, monkeypatch):
    import json
    from billrag import ingest as ing
    from billrag.store import Store
    bills = [dict(billNo=n, billSubNo=2026, billNoText=f"{n:03d}/2026", date="2026-10-01T00:00:00.000Z", descriptionEnglish=f"Thing {n} - Bill") for n in (1, 2)]
    (tmp_path / "bills.json").write_text(json.dumps(bills))
    (tmp_path / "pdfs/2026").mkdir(parents=True)
    for n in (1, 2):
        (tmp_path / f"pdfs/2026/{n:03d}_2026_ENGLISH.pdf").write_bytes(b"%PDF")
    db = tmp_path / "index.sqlite"
    store = Store(db)
    store.replace_bill({"key": "001/2026", "bill_no": 1, "year": 2026, "date": "2026-10-01T00:00:00.000Z", "title": "Thing 1", "title_norm": "thing 1", "kind": "Bill"},
                       [{"kind": "overview", "page": 1, "text": "x", "ctx": "x"}])
    store.commit()
    old = [r[0] for r in store.db.execute("SELECT id FROM chunks")]
    seen = []

    class NoPool:  # the parser itself is tested elsewhere; only which documents reach it matters here
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def imap(self, fn, jobs, chunksize=1):
            seen.extend(p for p, _ in jobs)
            return [(None, "stub") for _ in jobs]
    monkeypatch.setattr(ing, "Pool", NoPool)
    ing.ingest(tmp_path, db, only_new=True)
    assert [p.name for p in seen] == ["002_2026_ENGLISH.pdf"]
    assert [r[0] for r in Store(db).db.execute("SELECT id FROM chunks")] == old


def test_a_partial_scrape_cannot_replace_the_full_csv_in_r2(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("scrape_bills", ROOT_FOR_SCRIPTS / "scripts/scrape_bills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.csv_path(Path("d"), "bills", 0) == Path("d/bills.csv")
    assert mod.csv_path(Path("d"), "bills", 2025) == Path("d/bills.since-2025.csv")
    upload = (ROOT_FOR_SCRIPTS / "scripts/upload_r2.py").read_text(encoding="utf-8")
    assert '("bills.json", "bills.csv")' in upload and "rglob(\"*.csv\")" not in upload  # upload_r2 sends only these by name


# ---- read-time cleanup of parser noise in clause text (pipeline.strip_noise)
def test_strip_noise_removes_margin_notes_printer_and_running_header_only():
    from billrag.pipeline import shown_text, strip_noise
    t = ("shall be required to be registered under this Act, if- Amendment of section 7 of the principal enactment. Amendment of "
         "section 10 of the principal enactment.\n(a) at the end of any taxable period the value has exceeded nine million rupees; or")
    assert strip_noise(t) == ("shall be required to be registered under this Act, if-\n(a) at the end of any taxable period the value "
                              "has exceeded nine million rupees; or")
    last = ("In the event of any inconsistency between the Sinhala and Tamil texts of this Act, the Sinhala text shall prevail. "
            "Short title. Amendment of section 36 of Act, No. 35 of 2018. Sinhala text to prevail in case of inconsistency. "
            "Department of Government Printing, Sri Lanka.")
    assert strip_noise(last) == ("In the event of any inconsistency between the Sinhala and Tamil texts of this Act, the Sinhala text "
                                 "shall prevail.")
    header = "the Commission.”. Replacement of Article 109 of the Constitution 25Twentieth Amendment to the Constitution\n(2) The"
    assert strip_noise(header, "Twentieth Amendment to the Constitution - Bill") == "the Commission.”.\n(2) The"
    # kept: a real sentence that mentions an amendment, the bill's title inside text, a Schedule after the last clause
    keep = ["The amendment of section 7 of the principal enactment shall not apply to pending cases.",
            "This Act may be cited as the Twentieth Amendment to the Constitution Act.\n(2) Next",
            "the Sinhala text shall prevail. Column I Column II Section Offence 137 Wearing the dress one hundred rupees"]
    for k in keep:
        assert strip_noise(k, "Twentieth Amendment to the Constitution - Bill") == k
    c = {"kind": "section", "text": last, "title": "X - Bill"}
    assert shown_text(c).endswith("shall prevail.") and c["text"] == last   # stored text untouched


# ---------------------------------------------------------------- version suffixes in listing titles (SSCL question, 2026-11-09)
def test_classify_reads_every_spelling_of_the_version_suffix():
    from billrag.parse import plain_title
    cases = {
        "Social Security Contribution Levy - (GS)": ("GS", "social security contribution levy"),
        "Central Bank of Sri Lanka (GS)": ("GS", "central bank of sri lanka"),
        "Coconut Development (Amendment) GS": ("GS", "coconut development amendment"),
        "Minimum Retirement Age of Workers _ GS": ("GS", "minimum retirement age of workers"),
        "Surcharge Tax - (Bill)": ("Bill", "surcharge tax"),
        "Poisons, Opium and Dangerous Drugs (Amendment) Bill": ("Bill", "poisons opium and dangerous drugs amendment"),
        "National Minimum Wage of Workers (Amendment) - SUPPLEMENT (Bill)": ("Bill", "national minimum wage of workers amendment"),
        "Trusts (Amendment) - Bill": ("Bill", "trusts amendment"),
        "Foo BIll": ("Bill", "foo"),
        "Appropriation": ("Other", "appropriation"),
        "Appropriation Bill 2020": ("Other", "appropriation bill 2020"),  # not at the end
        "Bill": ("Other", "bill"), "GS": ("Other", "gs"),                 # nothing left: not a suffix
    }
    for title, want in cases.items():
        assert classify(title) == want, title
    assert plain_title("Social Security Contribution Levy - (GS)") == "Social Security Contribution Levy"
    assert plain_title("Coconut Development (Amendment) GS") == "Coconut Development (Amendment)"


def test_reclassify_fixes_kind_in_place_without_touching_chunks(tmp_path):
    from billrag.ingest import reclassify
    from billrag import meta
    st = make_store(tmp_path)
    st.db.execute("UPDATE bills SET title='Trusts (Amendment) - (GS)', kind='Other', title_norm='trusts amendment gs' WHERE key='081/2026'")
    st.commit()
    meta.build(st)
    fams = dict(st.db.execute("SELECT bill_key, family FROM bill_meta").fetchall())
    assert fams["081/2026"] != fams["086/2026"]                        # the bug: GS text and Bill in different families
    fp = st.fingerprint()
    assert reclassify(st) == [("081/2026", "Other", "GS", "trusts amendment")]
    assert st.fingerprint() == fp                                      # chunk text unchanged: vectors stay valid
    meta.build(st)
    fams = dict(st.db.execute("SELECT bill_key, family FROM bill_meta").fetchall())
    assert fams["081/2026"] == fams["086/2026"]
    assert reclassify(st) == []


def test_overview_header_stored_as_other_shows_the_plain_version():
    from billrag.pipeline import shown_text, version_tag
    c = {"kind": "overview", "bill_kind": "GS", "title": "Social Security Contribution Levy - (GS)", "date": "2022-06-21T00:00:00Z",
         "text": "Bill 213/2022 — Social Security Contribution Levy - (GS) (Other, 2022-06-21). An Act to ..."}
    assert shown_text(c).startswith("Bill 213/2022 — Social Security Contribution Levy (Gazette text, 2022-06-21).")
    assert version_tag(c) == "Social Security Contribution Levy (Gazette text, 2022-06-21)"


def test_refresh_reclassifies_kinds_once(tmp_path):
    r = _refresh()
    st = make_store(tmp_path)  # index at tmp_path/i.sqlite: refresh expects <bills_dir>/index.sqlite
    st.db.execute("UPDATE bills SET title='Trusts (Amendment) GS', kind='Other', title_norm='trusts amendment gs' WHERE key='081/2026'")
    st.commit()
    (tmp_path / "i.sqlite").rename(tmp_path / "index.sqlite")
    assert r.reclassify_kinds(tmp_path) == ["081/2026"]
    assert r.reclassify_kinds(tmp_path) == []                         # idempotent: a later run has nothing to do


# ---------------------------------------------------------------- Acts transcribed from scans (OCR + manual review), 2026-11-09
def _add_itr(tmp_path):
    from billrag.ingest import add_base_act
    root = ROOT_FOR_SCRIPTS / "sources" / "base_acts"
    st = make_store(tmp_path)
    key, n = add_base_act(tmp_path / "i.sqlite", None, 14, 1990, "Inland Trust Receipts", "1990-03-06",
                          text=root / "14-1990.txt", reviewed="2026-11-09")
    return Store(tmp_path / "i.sqlite"), key, n


def test_reviewed_transcription_is_indexed_and_marked(tmp_path):
    from billrag import acts, transcribed
    st, key, n = _add_itr(tmp_path)
    assert (key, n) == ("Act 14/1990", 7)
    b = st.bill(key)
    assert b["title"] == "Inland Trust Receipts Act, No. 14 of 1990" and b["date"].startswith("1990-03-06")
    s1 = " ".join(r[0] for r in st.db.execute("SELECT text FROM chunks WHERE bill_key=? AND section_no=1", (key,)))
    assert "Act, No. 14 of 1990" in s1                                   # the year OCR read as 1980, as reviewed
    assert transcribed.is_transcribed(st, key) and not transcribed.is_transcribed(st, "086/2026")
    assert "scanned copy" in acts.status_lines(st, key, 3)
    note = transcribed.note(st, ["086/2026", key, key])
    assert note.startswith("The text of Inland Trust Receipts Act, No. 14 of 1990 was read from a scanned copy")
    assert "official printed document" in note
    assert transcribed.note(st, ["086/2026"]) is None


def test_answer_using_a_transcribed_act_carries_the_note_and_source_tag(tmp_path):
    class AllRelevant(ScriptLLM):
        def chat(self, system, messages, tools=(), max_tokens=0, effort=None):
            user = messages[-1]["content"]
            if "CANDIDATES:" in user:
                n = len(re.findall(r"^\[\d+\] ", user.split("CANDIDATES:")[1], re.M))
                return Reply(json.dumps({"scores": [{"i": i, "s": 3} for i in range(1, n + 1)], "enough": True, "missing": "", "queries": []}))
            return super().chat(system, messages, tools, max_tokens, effort)

    st, key, _ = _add_itr(tmp_path)
    r = Pipeline(st, Retriever(st), AllRelevant()).ask("What is the penalty for breaching an inland trust receipt undertaking?")
    assert key in {e.key for e in r.evidence}
    assert r.transcribed == {key} and "scanned copy" in r.scan_note
    assert any(s.endswith(" - transcribed from a scan") for s in r.sources() if key in s)


def test_refresh_adds_pending_transcriptions_once(tmp_path):
    r = _refresh()
    st = make_store(tmp_path)
    (tmp_path / "i.sqlite").rename(tmp_path / "index.sqlite")
    src = tmp_path / "base_acts"
    src.mkdir()
    for no, year, accepted in ((14, 1990, "owner"), (46, 1992, None)):  # only the accepted one may be added
        (src / f"{no}-{year}.txt").write_text("text", encoding="utf-8")
        (src / f"{no}-{year}.json").write_text(json.dumps({"no": no, "year": year, "title": "X", "accepted_by": accepted}), encoding="utf-8")
    assert [k for k, _m, _t in r.pending_transcripts(tmp_path, src)] == ["Act 14/1990"]
    ran = []
    assert r.add_transcripts(lambda *cmd, step=None: ran.append(cmd), tmp_path, src) == ["Act 14/1990"]
    assert ran[0][:4] == ("-m", "billrag", "add-base-act", "--text") and "14" in ran[0] and "1990" in ran[0]


def test_every_reviewed_transcription_parses_to_its_sections():
    from billrag.ingest import read_transcript
    from billrag.parse import act_cover_ok, parse_act_pages
    root = ROOT_FOR_SCRIPTS / "sources" / "base_acts"
    metas = sorted(root.glob("*.json"))
    assert metas
    for mp in metas:
        m = json.loads(mp.read_text(encoding="utf-8"))
        pages = read_transcript(mp.with_suffix(".txt"))
        assert any(act_cover_ok(x, m["no"], m["year"]) for x in pages[:3]), mp.name
        secs = [s.no for s in parse_act_pages(pages, m["title"], m["no"], m["year"]).sections if s.kind == "section"]
        assert secs == list(range(1, m["sections"] + 1)), (mp.name, secs)


def test_reviewed_transcriptions_have_no_words_split_across_lines():
    root = ROOT_FOR_SCRIPTS / "sources" / "base_acts"
    for f in sorted(root.glob("*.txt")):
        assert not re.findall(r"[a-z]-\n[a-z]", f.read_text(encoding="utf-8")), f.name  # "pres-\ncribed": search would miss "prescribed"


def test_base_act_note_says_as_enacted_and_names_the_amending_acts(tmp_path):
    from billrag import acts, baseacts
    st, key, _ = _add_itr(tmp_path)
    assert baseacts.is_base(st, key) and not baseacts.is_base(st, "Act 25/2026") and not baseacts.is_base(st, "086/2026")
    assert "it may have been amended since" in baseacts.as_enacted(st, key)  # nothing in the index amends it yet
    st.db.execute("INSERT INTO bills(key, bill_no, year, date, title, title_norm, kind, long_title) VALUES(?,?,?,?,?,?,?,?)",
                  ("Act 3/2025", 3, 2025, "2025-02-01T00:00:00Z", "Inland Trust Receipts (Amendment) Act, No. 3 of 2025",
                   "inland trust receipts amendment", "Act", "to amend the Inland Trust Receipts Act, No. 14 of 1990"))
    st.commit()
    assert baseacts.amended_by(st, key) == ["Act 3/2025"]
    note = baseacts.note(st, ["086/2026", key])
    assert note.startswith("Inland Trust Receipts Act, No. 14 of 1990 is the text as enacted in 1990; amended since by Act No. 3 of 2025.")
    assert "may be out of date" in note and baseacts.note(st, ["086/2026"]) is None
    assert "as originally enacted" in acts.status_lines(st, key, 3)


# ---------------------------------------------------------------- IRD consolidated texts next to the certified Act, 2026-11-09
def _add_sscl_cons(tmp_path):
    from billrag.ingest import add_consolidation
    root = ROOT_FOR_SCRIPTS / "sources" / "consolidations"
    m = json.loads((root / "25-2022.json").read_text(encoding="utf-8"))
    make_store(tmp_path)
    key, n = add_consolidation(tmp_path / "i.sqlite", root / "25-2022.txt", 25, 2022, m["title"], m["up_to"], m["amendments"], m["source"])
    return Store(tmp_path / "i.sqlite"), key, n


def test_consolidation_files_parse_to_their_sections():
    from billrag.ingest import read_transcript
    from billrag.parse import parse_act_pages
    root = ROOT_FOR_SCRIPTS / "sources" / "consolidations"
    for mp in sorted(root.glob("*.json")):
        m = json.loads(mp.read_text(encoding="utf-8"))
        secs = [s.no for s in parse_act_pages(read_transcript(mp.with_suffix(".txt")), m["title"], m["no"], m["year"]).sections
                if s.kind == "section"]
        assert secs == list(range(1, m["sections"] + 1)), mp.name


def test_consolidation_is_indexed_next_to_the_act_and_labelled(tmp_path):
    from billrag import acts, consolidated
    from billrag.pipeline import CITE, version_tag
    st, key, n = _add_sscl_cons(tmp_path)
    assert (key, n) == ("Cons 25/2022", 61)
    b = st.bill(key)
    assert b["kind"] == "Cons" and b["title"].startswith("Social Security Contribution Levy Act, No. 25 of 2022 (IRD consolidated text")
    s4 = " ".join(r[0] for r in st.db.execute("SELECT text FROM chunks WHERE bill_key=? AND section_no=4", (key,)))
    assert "July 1, 2026" in s4 and "[IRD note: Sec 2(2) of 10 of 2026]" in s4
    assert CITE.search("as amended [Cons 25/2022 §4]").groups() == ("Cons 25/2022", "4")
    status = acts.status_lines(st, key, 4)
    assert "consolidated text" in status and "9 April 2026" in status and "not a statutory text" in status
    assert "not shown to have passed" not in status                        # never treated as a bill
    assert not st.db.execute("SELECT 1 FROM act_links WHERE bill_key=?", (key,)).fetchone()
    note = consolidated.note(st, ["086/2026", key])
    assert note.startswith("Parts of this answer use the Inland Revenue Department consolidated text") and "certified Act" in note
    assert consolidated.note(st, ["086/2026"]) is None
    assert version_tag({"title": b["title"], "bill_kind": "Cons", "date": b["date"]}).endswith("(IRD consolidated text, 2026-04-09)")


def test_refresh_adds_only_accepted_newer_consolidations(tmp_path):
    r = _refresh()
    make_store(tmp_path)
    (tmp_path / "i.sqlite").rename(tmp_path / "index.sqlite")
    src = tmp_path / "cons"
    src.mkdir()
    for no, accepted in ((25, "owner"), (24, None)):
        (src / f"{no}-2022.txt").write_text("x", encoding="utf-8")
        (src / f"{no}-2022.json").write_text(json.dumps({"no": no, "year": 2022, "title": "X", "up_to": "2026-04-09", "accepted_by": accepted}))
    assert [k for k, _m, _t in r.pending_consolidations(tmp_path, src)] == ["Cons 25/2022"]
