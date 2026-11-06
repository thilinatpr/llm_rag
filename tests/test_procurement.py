"""Offline tests for the procurement prototype: synthetic page text (no PDFs, no network, no keys)."""
import re
from billrag.llm import Reply
from procurement import ask as pa
from procurement import index as pix
from procurement import parse

HDR = "      Part I : Sec. (I) - GAZETTE EXTRAORDINARY OF THE DEMOCRATIC SOCIALIST REPUBLIC OF SRI LANKA - 25.11.2024"
G24 = [
    "                                Contents\n"
    "CHAPTER 01 – GENERAL..................... 12\n"
    "   1.1 Purpose ............................\n"
    "   1.2 Emergency Procurement ..............\n"
    "       1.2.1 Covering Approval ............\n"
    "CHAPTER 02 - GOVERNANCE.................. 14\n"
    "   2.1 Procurement Committees .............\n",
    HDR + "\n                 DEFINITIONS\n\n“PE”        Means the Procuring Entity.\n",
    HDR + "\n                 CHAPTER 01 – GENERAL\n\n"
    "1.1 Purpose                                                                                     Refer Manual\n"
    "                                                                                                 1.1\n"
    "The purpose is set out in 1.2 below and in Section 2.1.\n\n"
    "1.2 Emergency Procurement\n(a) PE may use this method in exceptional circumstances.\n"
    "1.2.1 Covering Approval\nA covering approval shall be obtained at the first available opportunity.\n"
    "                                        15\n",
    HDR + "\n          CHAPTER 02 - GOVERNANCE\n\n2.1 Procurement Committees\nPCs are appointed by the CAO.\n",
]
PM24 = [
    "Contents\nCHAPTER 01 – GENERAL ............ 4\n  Purpose ..........\n",
    "     CHAPTER 01 – GENERAL\n\n        Emergency Procurement\n"
    "  (Relevant Reference to the Procurement Guidelines - 1.2)\n"
    "The CAO may approve emergency procurement up to Rs. 5 Mn.\n\n"
    "   Annexure I of Chapter 01\n   Format for Covering Approval\n"
    "  (Procurement Guideline Reference - 1.2.1)\nName of PE: ..........\n                 4\n",
]


def test_cut_margin():
    assert parse.cut_margin("1.4 Scope of Application" + " " * 80 + "Refer Manual") == ("1.4 Scope of Application", "Refer Manual")
    assert parse.cut_margin(" " * 97 + "1.4.1") == ("", "1.4.1")
    assert parse.cut_margin("as stated in Section 2.9 of the Guidelines")[1] is None  # in-text numbers stay


def test_guidelines_2024_follow_contents_order():
    chunks, st = parse.parse_guidelines_2024(G24)
    assert st["missing"] == [] and st["found"] == 4
    by = {c["ref"]: c for c in chunks}
    assert by["1.1"]["xref"] == "1.1" and "Refer Manual" not in by["1.1"]["text"]
    assert "1.2 below" in by["1.1"]["text"]  # an in-text number does not start a clause
    assert by["1.2.1"]["heading"] == "Covering Approval" and by["1.2"]["chapter"] == "Chapter 01 - General"
    assert by["2.1"]["chapter"].startswith("Chapter 02") and "GAZETTE" not in by["2.1"]["text"]
    assert by["Definitions"]["kind"] == "definitions"
    assert "CHAPTER 02" not in by["1.2.1"]["text"]


def test_manual_2024_sections_keyed_by_guideline_ref():
    chunks, st = parse.parse_manual_2024(PM24)
    assert st["sections"] == 2
    a, b = chunks
    assert (a["ref"], a["heading"], a["kind"]) == ("1.2", "Emergency Procurement", "section")
    assert "Annexure" not in a["text"]  # the next heading is taken out of the previous section
    assert (b["ref"], b["kind"]) == ("1.2.1", "annexure") and b["heading"].startswith("Annexure I of Chapter 01")


def test_manual_2006_merges_continuations():
    pages = ["PROCUREM ENT GUIDELINE REFERENCE: 2.7.1\nCABINET APPOINTED COMMITTEES\nText one.\n",
             "PROCUREMENT GUIDELINE REFERENCE: 2.7.1 (Cont)\nText two.\n",
             "PROCUREMENT GUIDELINE REFERENCE: 2.8\nPAYMENTS\nText three.\n"]
    chunks, st = parse.parse_manual_2006(pages)
    assert st["sections"] == 2
    assert chunks[0]["heading"] == "Cabinet Appointed Committees" and "Text two." in chunks[0]["text"]


def test_guidelines_2006_sequence_check():
    pages = ["CONTENTS\n1.1 Purpose\n1.2 Scope\n",
             "    CHAPTER I\n    GENERAL\n1.1     Purpose\n1.1.1   The purpose is to set procedures.\n"
             "1.2     Scope\n1.2.1   Applies to 5.5 million items and 3.4 others.\n"
             "                 2.1     Committees\n                 2.1.1   PCs.\n"]
    chunks, st = parse.parse_guidelines_2006(pages)
    assert [c["ref"] for c in chunks] == ["1.1", "1.2", "2.1"]


def test_split_long_respects_limit():
    parts = parse.split_long("\n\n".join(["x" * 500] * 10), 1200)
    assert len(parts) > 3 and all(len(p) <= 1800 for p in parts)
    assert parse.split_long("y" * 5000, 1000)  # one block, no line breaks: still cut, no endless loop


def _index(tmp_path, monkeypatch):
    docs = [dict(id="PG2024", kind="guidelines", status="current", issued="2024-11-25", effective="2025-01-01",
                 title="Procurement Guidelines - 2024", file=tmp_path / "g.pdf"),
            dict(id="PM2024", kind="manual", status="current", issued="2025-01-01", effective="2025-01-01",
                 title="Procurement Manual 2024", file=tmp_path / "m.pdf")]
    for d in docs:
        d["file"].write_bytes(b"%PDF")
    pages = {str(tmp_path / "g.pdf"): G24, str(tmp_path / "m.pdf"): PM24}
    monkeypatch.setattr(pix.sources, "DOCS", docs)
    monkeypatch.setattr(pix.parse, "pdf_pages", lambda p, overrides=None: pages[str(p)])
    monkeypatch.setattr(pix.sources, "AMENDMENTS", {"1.2": "test amendment"})
    pix.build(tmp_path / "i.sqlite", log=lambda *a: None)
    return pix.Index(tmp_path / "i.sqlite")


def test_index_search_pairs_manual_and_amendment(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    hits = idx.search("emergency procurement covering approval", k=3)
    assert hits and hits[0]["doc"] in ("PG2024", "PM2024")
    units = pa.gather(idx, "emergency procurement")
    labels = [u["label"] for u in units]
    assert "[PG2024 §1.2]" in labels and "[PM2024 §1.2]" in labels
    assert next(u for u in units if u["label"] == "[PG2024 §1.2]")["note"].endswith("test amendment")
    assert idx.search("CAO", k=5)  # acronym is searchable


class FakeLLM:
    def __init__(self, text):
        self.text, self.calls = text, []

    def chat(self, system, messages, max_tokens=0, effort=None, **kw):
        self.calls.append(messages[-1]["content"])
        return Reply(self.text)


def test_ask_verifies_citations(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    good = FakeLLM("Use it in exceptional circumstances [PG2024 §1.2]; the CAO may approve up to Rs. 5 Mn 【PM2024 §1.2】.")
    r = pa.ask(idx, good, "When can emergency procurement be used?")
    assert r.warnings == [] and "PG2024 §1.2" in good.calls[0]
    bad = pa.ask(idx, FakeLLM("Yes [PG2024 §9.9] and [PM2006 §1.2]."), "When can emergency procurement be used?")
    assert any("PG2024 §9.9" in w and "PM2006 §1.2" in w for w in bad.warnings)
    none = pa.ask(idx, FakeLLM("It depends."), "emergency procurement")
    assert "answer has no citations" in none.warnings


def test_verify_parent_and_child_refs():
    units = [dict(doc="PG2024", ref="1.2", text="see 1.2.1 below"), dict(doc="PM2024", ref="2.7.3", text="")]
    assert pa.verify("[PG2024 §1.2.1] [PM2024 §2.7] [PG2024 §1.2]", units) == []
    assert pa.verify("[PG2024 §1.2.4]", units) == ["PG2024 §1.2.4"]


def test_old_regime_questions_search_2006_docs():
    assert pa.OLD.search("A procurement started under the 2006 guidelines")
    assert pa.OLD.search("what did the TEC do")
    assert not pa.OLD.search("What is the standstill period?")


def test_broken_replies_are_retried(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    assert pa.broken("natural or man " + "man " * 20)
    assert pa.broken("text <|end|><|start|>assistantanalysis: redo")
    assert pa.broken("a " + "‑" * 80)
    assert pa.broken("The PC shall review the bid. The PC shall approve. [PG2024 §1.2]") is None

    class Flaky(FakeLLM):
        def chat(self, system, messages, **kw):
            self.calls.append(messages[-1]["content"])
            return Reply("man " * 30 if len(self.calls) == 1 else "Exceptional cases only [PG2024 §1.2].")
    r = pa.ask(idx, Flaky(""), "emergency procurement")
    assert r.answer.endswith("[PG2024 §1.2].") and r.warnings == ["attempt 1 rejected: degenerate repetition"]


def test_old_regime_question_gets_transition_rule_first(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    units = pa.gather(idx, "emergency", first=[("PG2024", "1.1")])
    assert units[0]["label"] == "[PG2024 §1.1]"


def test_table_transcriptions_replace_pages_and_numbers_are_checked(tmp_path, monkeypatch):
    from procurement import tables
    (tmp_path / "PMX").mkdir()
    (tmp_path / "PMX" / "p002.md").write_text(
        "<!-- PMX page 2: transcribed -->\nTable: payments, in LKR (caption 99 is ours)\n"
        "| Value (LKR Million) | Chairman (LKR) |\n|---|---|\n| More than 1000 | 35,000 |\n(table continues on page 3)\n")
    monkeypatch.setattr(tables, "ROOT", tmp_path)
    over = tables.load("PMX")
    assert list(over) == [2] and "<!--" not in over[2]
    assert dict(tables.numbers(over[2])) == {"1000": 1, "35000": 1}  # caption and continuation notes are skipped
    monkeypatch.setattr(parse.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "p1\fMore than 1000  35,000\n  55\f"}))
    assert parse.pdf_pages("x.pdf", over)[1] == over[2]
    assert tables.check("PMX", "x.pdf") == []  # the printed page number (55) is not required
    (tmp_path / "PMX" / "p002.md").write_text("| More than 1000 | 36,000 |\n")
    assert tables.check("PMX", "x.pdf") == [(2, ["36000"], ["35000"])]


def test_manual_reference_variant_starts_a_section():
    assert parse.REF_2024.match("(Procurement Manual Reference - 5.5)").group(1) == "5.5"


def test_shipped_transcriptions_are_well_formed():
    from procurement import tables
    pages = tables.load("PM2024")
    assert len(pages) >= 20
    for p, txt in pages.items():
        rows = [ln for ln in txt.splitlines() if ln.startswith("|")]
        assert rows, p
        for block in re.findall(r"(?:^\|.*\n?)+", txt, re.M):  # every row of one table has the same number of cells
            assert len({ln.count("|") for ln in block.strip().splitlines()}) == 1, (p, block[:80])


def test_window_keeps_the_matching_part_of_a_long_clause():
    parts = [dict(part=n, text=f"part{n} " + "x" * 990) for n in range(6)]
    t = pa.window(parts, 5, 2500)
    assert "part5" in t and "part4" in t and "part0" not in t and t.startswith("[...]")
    assert pa.window(parts[:2], 1, 2500).startswith("part0")  # short clause: whole text


CON_PAGES = [
    "196   The Constitution of the Democratic Socialist Republic of Sri Lanka\n"
    "                     CHAPTER XIX B\n"
    "     156B. (1) There shall be a National Procurement                National\n"
    "                                                                    Procurement\n"
    "Commission consisting of five members appointed by the             Commission\n"
    "President on the rec-\n"
    "ommendation of the Constitutional Council.\n",
    "         The Constitution of the Democratic Socialist Republic of Sri Lanka     197\n"
    "  Powers of the        156D. (1) The Commission may, by Notice in writing,\n"
    "  Commission\n"
    "                   require any person to attend before the Commission and to answer\n"
    "                   the questions put to that person by the Commission.\n"
    "                                             CHAPTER XX\n"
    "  International           157. Where Parliament by resolution passed by not\n",
]


def test_constitution_chapter_articles_and_margin_notes():
    chunks, st = parse.parse_constitution_npc(CON_PAGES)
    assert st["articles"] == 2
    b, d = chunks
    assert (b["ref"], b["heading"]) == ("156B", "National Procurement Commission")
    assert "recommendation of the Constitutional Council" in b["text"] and "\n\n" not in b["text"]
    assert (d["ref"], d["heading"]) == ("156D", "Powers of the Commission")
    assert "157" not in d["text"] and "Constitution of the Democratic" not in d["text"]


def test_constitution_citations():
    units = [dict(doc="CON", ref="156C", text="")]
    assert pa.verify("NPC formulates guidelines [CON Art. 156C].", units) == []
    assert pa.verify("[CON Art. 156D]", units) == ["CON Art. 156D"]
    assert pa.label("CON", "156C") == "[CON Art. 156C]"


def test_rerank_keeps_bm25_top_hits(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    first = idx.bm25("emergency procurement covering approval", 30)[0]

    class Contrarian:  # scores the BM25 winner lowest
        def scores(self, query, texts):
            return [float(i) for i in range(len(texts))]
    idx.reranker = Contrarian()
    assert first in [c["id"] for c in idx.search("emergency procurement covering approval", k=2)]
    assert idx.bm25("Who is the NPC?", 5) == idx.bm25("Who is the NPC", 5)
