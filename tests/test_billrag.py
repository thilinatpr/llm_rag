import json

import pytest

from billrag.agent import Agent, TOOLS
from billrag.ingest import build_chunks, classify
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
            assert b"Sri Lanka Bills Chat" in r.read()
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
