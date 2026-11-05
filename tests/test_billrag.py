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

    def chat(self, system, messages, tools=(), max_tokens=0):
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
    assert {t["name"] for t in TOOLS} == {"search_bills", "list_bills", "get_bill_overview", "get_section", "diff_bills"}
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
