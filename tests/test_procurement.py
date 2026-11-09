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


def test_curated_texts_parse_and_cover_the_registry():
    from procurement import sources
    text = "<!-- SUPX | demo -->\n## 8.2 | Communication (replaces 8.2)\nWithin Seven (07) calendar days.\n" \
           "## | Whole circular\n01. Paragraph one.\n"
    chunks, st = parse.parse_curated(text, "SUPX")
    assert [(c["ref"], c["heading"]) for c in chunks] == [("8.2", "Communication (replaces 8.2)"), ("", "Whole circular")]
    for d in sources.DOCS:  # every shipped curated file parses into at least one section
        if d.get("parser") == "curated":
            assert parse.parse_curated(d["file"].read_text(encoding="utf-8"), d["id"])[0], d["id"]


def test_numbered_document_skips_contents_and_centred_headings():
    pages = ["Contents\n   1.1 Purpose.................... 1\n   1.2 Scope ..................... 2\n   2.1 Methods ............ 5\n",
             "Foreword text about the note.\n                                 1.1 Purpose\nThe purpose is X.\n"
             "1.1.1 Detail in 2.5 million cases.\n                                 1.2 Scope\nScope text.\n"
             "                 2.1 Methods\nMethods text.\n   1.1. Annex item restarting numbering\n"]
    chunks, st = parse.parse_numbered(pages, "DOCX")
    assert st["clauses"] == 3 and [c["ref"] for c in chunks] == ["0", "1.1", "1.2", "2.1"]
    assert "Annex item" in chunks[-1]["text"] and "Foreword" in chunks[0]["text"]


def test_citations_of_any_document_and_whole_document_units():
    units = [dict(doc="PFC0326i", ref="", text=""), dict(doc="SUP05", ref="5.6.1", text="")]
    assert pa.verify("Relief applies [PFC0326i] and [PFC0326i §03]; grades [SUP05 §5.6.1].", units) == []
    assert pa.verify("[SUP07 §3.1.6]", units) == ["SUP07 §3.1.6"]
    assert pa.label("NPCC2024", "") == "[NPCC2024]"
    assert not pa.OLD.search("What does Supplement 05 change?")  # NPC supplements are current, not the old regime


def test_supplements_are_found_for_the_section_they_change(tmp_path, monkeypatch):
    from procurement import sources
    idx = _index(tmp_path, monkeypatch)
    sup = tmp_path / "SUPX.md"
    sup.write_text("<!-- SUPX -->\n## 1.2 | Emergency procurement: new rule\nCovering approval within 3 days.\n")
    docs = sources.DOCS + [dict(id="SUPX", kind="supplement", status="current", issued="2026", effective="2026",
                                amends="PM2024", parser="curated", title="Supplement X", file=sup)]
    monkeypatch.setattr(pix.sources, "DOCS", docs)
    pix.build(tmp_path / "i.sqlite", docs=["SUPX"], log=lambda *a: None)
    idx = pix.Index(tmp_path / "i.sqlite")
    assert [c["doc"] for c in idx.supplements_for("PG2024", "1.2")] == ["SUPX"]
    labels = [u["label"] for u in pa.gather(idx, "emergency procurement covering approval", docs=None)]
    assert "[SUPX §1.2]" in labels


def test_financial_regulations_parse_and_cite():
    pages = ["CONTENTS\n F.R.135 Delegation ....... 84\n",
             "            CHAPTER III\n       F.R.135. Delegation of Functions for Financial Control. (1) An accounting\n"
             "Officer may delegate his functions.\n   F.R.487. Misprint. Text.\n       F.R.137. Approval. An officer who is\n"
             "empowered to approve.\n F.R.138. (1) Certification of vouchers. (a) The certifying officer ...\n"]
    chunks, st = parse.parse_fr(pages)
    assert [c["ref"] for c in chunks] == ["135", "137", "138"] and st["dropped"] == 1
    assert chunks[0]["heading"] == "Delegation of Functions for Financial Control" and chunks[0]["chapter"] == "CHAPTER III"
    assert chunks[2]["heading"] == "Certification of vouchers"
    units = [dict(doc="FR", ref="135", text="")]
    assert pa.verify("Delegation is allowed [FR 135].", units) == [] and pa.verify("[FR 137]", units) == ["FR 137"]
    assert pa.label("FR", "135") == "[FR 135]"


def test_referenced_financial_regulation_is_pulled_in():
    assert pa.FR_REF.findall("attention should be paid to Financial Regulation (FR) 136 (3); see also F.R.135 and FR 137.") \
        == ["136", "135", "137"]


def test_translated_fr_amendments_are_paired_cited_and_flagged(tmp_path, monkeypatch):
    from procurement import sources
    fr, fra = tmp_path / "FR.md", tmp_path / "FRA.md"
    fr.write_text("<!-- FR -->\n## 108 | Authority to deal with losses\nAccounting Officer: losses up to Rs. 25,000.\n")
    fra.write_text("<!-- FRA -->\n## 108 | FR 108 - Authorities to deal with losses (amended)\n"
                   "Accounting Officer: losses as in FR 105(1): Rs. 500,000 (before: Rs. 250,000).\n")
    docs = [dict(id="FR", kind="regulations", status="current", issued="1993", effective="1992", parser="curated",
                 title="Financial Regulations", file=fr),
            dict(id="FRA", kind="regulations", status="current", issued="2025", effective="2020", amends="FR",
                 parser="curated", note=sources.UNOFFICIAL, title="FR amendments", file=fra)]
    monkeypatch.setattr(pix.sources, "DOCS", docs)
    pix.build(tmp_path / "i.sqlite", log=lambda *a: None)
    idx = pix.Index(tmp_path / "i.sqlite")
    units = pa.gather(idx, "Accounting Officer authority to deal with losses")
    by = {u["label"]: u for u in units}
    assert "[FR 108]" in by and "[FRA 108]" in by  # the amendment comes with the 1992 text (and the other way round)
    assert "[FRA 108]" in by["[FR 108]"]["note"] and "SUPERSEDED" in by["[FR 108]"]["note"] and "Unofficial" in by["[FRA 108]"]["note"]
    assert pa.label("FRA", "108") == "[FRA 108]"
    assert pa.verify("Rs. 500,000 [FRA 108]; telephone [PFC0120 §4].", units) == ["PFC0120 §4"]
    r = pa.ask(idx, FakeLLM("Up to Rs. 500,000 [FRA 108]."), "Accounting Officer authority to deal with losses")
    assert r.warnings == [] and "unofficial English translation" in r.answer and "FRA is" in r.answer
    r = pa.ask(idx, FakeLLM("Up to Rs. 25,000 [FR 108]."), "Accounting Officer authority to deal with losses")
    assert "unofficial" not in r.answer  # no translated passage cited: no note


def test_replaced_1992_text_is_cut_and_the_rest_kept():
    def part(ref, n, text):
        return dict(doc="FR", ref=ref, heading="", chapter="", kind="regulation", page=1, part=n, text=text, xref=None)
    out = pix.drop_replaced([part("103", 0, "Keep."),
                             part("104", 0, "F.R.104. (1) Inquiry and fixing of responsibility. Old Rs. 500,000."),
                             part("104", 1, "(2) Reporting. (3) Preliminary Report: still in force."),
                             part("108", 0, "Old limits.")])
    assert [c["ref"] for c in out] == ["103", "104", "108"] and out[0]["text"] == "Keep."
    assert "500,000" not in out[1]["text"] and "(3) Preliminary Report" in out[1]["text"] and "see FRA 104" in out[1]["text"]
    assert "Old limits" not in out[2]["text"] and "see FRA 108" in out[2]["text"]


def test_server_requires_the_secret_and_answers(tmp_path, monkeypatch):
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from procurement import serve
    idx = _index(tmp_path, monkeypatch)
    secret = "s" * 32
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(serve.App(idx, FakeLLM("Yes [PG2024 §1.2]."), secret)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"

    def call(path, body=None, key=secret):
        req = urllib.request.Request(url + path, data=None if body is None else _json.dumps(body).encode(),
                                     headers={"X-Proxy-Secret": key} if key else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, _json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, _json.load(e)
    try:
        assert call("/healthz", key=None)[0] == 403 and call("/healthz", key="wrong")[0] == 403
        assert call("/healthz") == (200, {"ok": True})
        assert call("/api/ask", {"question": " "})[0] == 400 and call("/api/ask", {"question": "x" * 1001})[0] == 413
        code, out = call("/api/ask", {"question": "emergency procurement"})
        assert code == 200 and out["answer"] == "Yes [PG2024 §1.2]." and out["warnings"] == []
    finally:
        srv.shutdown()


def test_worker_reranker_reports_failure_so_search_keeps_bm25_order():
    from procurement.serve import WorkerReranker
    try:
        WorkerReranker("http://127.0.0.1:9/none", "s", timeout=1).scores("q", ["a"])
        raise AssertionError("should have raised")
    except OSError:
        pass


def test_worker_reranker_names_itself_and_aligns_scores():
    import io
    import json as _json
    from unittest import mock
    from procurement import serve
    seen = {}

    def fake(req, timeout=0):
        seen.update(ua=req.get_header("User-agent"), secret=req.get_header("X-proxy-secret"))
        return io.BytesIO(_json.dumps({"response": [{"id": 1, "score": 0.9}, {"id": 0, "score": 0.1}]}).encode())
    with mock.patch("urllib.request.urlopen", fake):
        assert serve.WorkerReranker("https://x.test/r", "s").scores("q", ["a", "b"]) == [0.1, 0.9]
    assert seen["ua"] == serve.USER_AGENT and "urllib" not in seen["ua"].lower() and seen["secret"] == "s"


def test_heading_chain_gives_the_parents_of_a_clause(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    assert idx.chain("PG2024", "1.2.1") == [("1.2", "Emergency Procurement")]  # chapter 1 has no clause of its own
    assert idx.chain("PM2024", "1.2.1")[-1] == ("1.2", "Emergency Procurement")  # manual borrows the guideline titles
    assert idx.chain("PG2024", "2.1") == []
    assert idx.chain("FR", "135") == [] and idx.chain("PG2024", None) == []
    units = pa.gather(idx, "covering approval")
    u = next(u for u in units if u["label"] == "[PG2024 §1.2.1]")
    assert "Under: " in pa.render([u]) and "1.2 Emergency Procurement" in pa.render([u])


def test_xrefs_need_a_keyword_and_follow_lists():
    assert pa.xrefs("PG2024", "set out in clause 1.2 and Section 2.1, costing Rs. 2.5 million") == [("PG2024", "1.2"), ("PG2024", "2.1")]
    assert pa.xrefs("PG2024", "see Guidelines 4.2.1, 4.2.2 or 4.3") == [("PG2024", "4.2.1"), ("PG2024", "4.2.2"), ("PG2024", "4.3")]
    assert pa.xrefs("PM2024", "as Guideline 3.1 requires; Section 3.2 below") == [("PG2024", "3.1"), ("PM2024", "3.2")]
    assert pa.xrefs("PG2024", "an amount of 2.5 and 3.4 units") == []


def test_pointed_at_clause_is_added_once_with_its_source(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    units = pa.gather(idx, "what is the purpose of procurement", k=1)
    labels = [u["label"] for u in units]
    assert "[PG2024 §1.1]" in labels and labels.count("[PG2024 §2.1]") == 1  # 1.1 says "Section 2.1"
    ref = next(u for u in units if u["label"] == "[PG2024 §2.1]")
    assert "Referred to by [PG2024 §1.1]" in ref["note"]
    assert "[PG2024 §1.2]" not in labels  # "set out in 1.2 below" has no keyword, so it is not followed
    assert pa.verify("Committees are appointed by the CAO [PG2024 §2.1].", units) == []


class ScriptedLLM(FakeLLM):
    """First reply rewrites the follow-up, the second answers it; records what each call saw."""
    def __init__(self, *replies):
        self.replies, self.seen = list(replies), []

    def chat(self, system, messages, **kw):
        self.seen.append(messages[-1]["content"])
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if not isinstance(r, str) else type("R", (), {"text": r, "truncated": False})()


HISTORY = [{"question": "Who approves emergency procurement?", "answer": "The CAO [PG2024 §1.2]."}]


def test_followup_is_rewritten_before_search_and_answer(tmp_path, monkeypatch):
    from procurement.ask import ask
    idx = _index(tmp_path, monkeypatch)
    llm = ScriptedLLM("How is emergency procurement approved by the CAO?", "By covering approval [PG2024 §1.2].")
    r = ask(idx, llm, "and how?", history=HISTORY)
    assert r.standalone == "How is emergency procurement approved by the CAO?" and r.question == "and how?"
    assert "Who approves emergency procurement?" in llm.seen[0]
    assert llm.seen[1].startswith("Question: How is emergency procurement approved by the CAO?")
    assert "The CAO [PG2024" not in llm.seen[1]  # the earlier answer is not evidence


def test_no_history_costs_no_extra_call(tmp_path, monkeypatch):
    from procurement.ask import ask
    llm = ScriptedLLM("Yes [PG2024 §1.2].")
    r = ask(_index(tmp_path, monkeypatch), llm, "emergency procurement", history=[])
    assert len(llm.seen) == 1 and r.standalone == "" and r.warnings == []


def test_failed_rewrite_searches_on_the_last_question_too_and_warns(tmp_path, monkeypatch):
    from procurement.ask import ask
    for bad in (RuntimeError("down"), "man " * 40):
        llm = ScriptedLLM(bad, "Yes [PG2024 §1.2].")
        r = ask(_index(tmp_path, monkeypatch), llm, "emergency procurement", history=HISTORY)
        assert r.standalone == "" and llm.seen[-1].startswith("Question: Who approves emergency procurement? emergency procurement")
        assert any("not rewritten" in w for w in r.warnings)


def test_history_from_the_browser_is_validated_and_capped():
    from procurement.ask import MAX_HISTORY, MAX_HISTORY_ANSWER, clean_history
    assert clean_history("x") == [] and clean_history([1, {"question": 2, "answer": "a"}, {"question": " ", "answer": "a"}]) == []
    long = [{"question": f"q{i}", "answer": "a" * 9000} for i in range(9)]
    out = clean_history(long)
    assert len(out) == MAX_HISTORY and out[-1]["question"] == "q8" and len(out[0]["answer"]) == MAX_HISTORY_ANSWER


def test_non_binding_guide_is_capped_paired_and_always_labelled(tmp_path, monkeypatch):
    from procurement import ask as A
    idx = _index(tmp_path, monkeypatch)
    guide = [dict(doc="GPMCM", ref=f"8.{i}", heading="h", part=0) for i in range(5)]
    monkeypatch.setattr(idx, "search", lambda *a, **k: guide)
    monkeypatch.setattr(A, "GUIDE_BINDING", (("PG2024", "1.2"),))
    monkeypatch.setattr(idx, "clause", lambda doc, ref, heading=None: [dict(heading="h", chapter="", page=1, note=None, part=0, text="t")])
    units = A.gather(idx, "q", docs=A.CURRENT)
    assert [u["doc"] for u in units].count("GPMCM") == A.MAX_GUIDE and units[0]["doc"] == "PG2024"
    monkeypatch.setattr(idx, "search", lambda *a, **k: guide[:1])
    r = A.ask(idx, FakeLLM("Withhold under the contract [GPMCM §8.0]."), "q", docs=A.CURRENT)
    assert "non-binding guidance" in r.answer and "unofficial" not in r.answer


def test_lower_case_ipc_finds_interim_payments(tmp_path, monkeypatch):
    from procurement.index import QUERY_TERMS
    assert "interim payment" in QUERY_TERMS["ipc"] and "withhold" in QUERY_TERMS["block"]


def test_eval_gold_is_well_formed_and_scoring_handles_typography():
    import json as _j
    from procurement.ask import Answer
    from procurement.evaluate import EVAL, score
    items = [_j.loads(l) for l in open(EVAL, encoding="utf-8")]
    assert len(items) == 39 and len({i["id"] for i in items}) == 39 and all(i["facts"] and i["gold"] for i in items)
    item = dict(id="x", question="q", gold=["PM2024 §2.9"], facts=["seven years"])
    r = score(item, Answer("q", answer="up to seven\u202fyears [PM2024 §2.9.1].", warnings=["unsupported citations: FR 1"]))
    assert r["strict"] and r["cited"] and not r["clean"]


def test_naming_a_regulation_a_passage_names_is_not_an_unsupported_citation():
    from procurement.ask import verify
    units = [dict(doc="FR", ref="113", text="Secretaries may authorize waivers arising out of liquidated damages - see F.R.704")]
    assert verify("Secretaries may authorise such waivers (FR 704) [FR 113].", units) == []  # what live answers do
    assert verify("Liquidated damages can be waived [FR 704].", units) == ["FR 704"]  # a citation still has to be shown
    assert verify("See FR 705 for this.", units) == ["FR 705"]  # no passage names this one


def test_a_regulation_a_passage_points_at_is_still_shown_when_the_list_is_full(tmp_path, monkeypatch):
    from procurement.ask import gather
    idx = _index(tmp_path, monkeypatch)
    many = [dict(doc="PG2024", ref=f"9.{n}", heading="H", part=0) for n in range(13)]
    chunks = {c["ref"]: c for c in many}
    monkeypatch.setattr(idx, "search", lambda q, k=8, docs=None: many)
    monkeypatch.setattr(idx, "manual_for", lambda ref, doc="PM2024": [])
    monkeypatch.setattr(idx, "supplements_for", lambda doc, ref: [])
    monkeypatch.setattr(idx, "chain", lambda doc, ref: [])

    def clause(doc, ref, heading=None):
        if doc == "FR" and ref == "107":
            return [dict(heading="Accounting", chapter="", page=1, part=0, text="F.R.107 text", note=None)]
        if doc == "PG2024" and ref in chunks:
            return [dict(heading="H", chapter="", page=1, part=0, text="Dealt with in terms of F.R.107." if ref == "9.0" else "x", note=None)]
        return []
    monkeypatch.setattr(idx, "clause", clause)
    units = gather(idx, "q")
    assert len([u for u in units if u["doc"] == "PG2024"]) == 10
    assert ("FR", "107") in [(u["doc"], u["ref"]) for u in units]


def test_rewriting_a_follow_up_does_not_spend_its_tokens_on_reasoning():
    from procurement.ask import condense

    class LLM:
        def chat(self, system, messages, max_tokens=0, effort=None, **kw):
            self.effort, self.limit = effort, max_tokens
            return Reply("How are unpaid contract liabilities dealt with?")
    llm = LLM()
    q, warn = condense(llm, "And unpaid ones?", [{"question": "Q", "answer": "A"}])
    assert (q, warn, llm.effort) == ("How are unpaid contract liabilities dealt with?", None, "off") and llm.limit >= 1000


def test_server_logs_cost_refuses_when_full_and_uses_the_workers_backup(tmp_path, monkeypatch):
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from billrag.gate import Gate
    from billrag.llm import FallbackLLM
    from procurement import serve
    idx = _index(tmp_path, monkeypatch)
    secret = "s" * 32
    seen = {}

    class WorkerStub(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(_json.loads(self.rfile.read(int(self.headers["Content-Length"]))), secret=self.headers["X-Proxy-Secret"])
            out = _json.dumps({"text": "Backup says [PG2024 §1.2].", "stop": "stop", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    worker = ThreadingHTTPServer(("127.0.0.1", 0), WorkerStub)
    threading.Thread(target=worker.serve_forever, daemon=True).start()

    class NoCredit:
        model = "deepseek-flash"

        def chat(self, *a, **k):
            e = RuntimeError("Insufficient Balance")
            e.status_code = 402
            raise e
    llm = FallbackLLM(NoCredit(), serve.WorkerChat(f"http://127.0.0.1:{worker.server_port}/internal/chat", secret))
    gate = Gate(1, 0, 1, 0, 0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.make_handler(serve.App(idx, llm, secret, gate)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def ask():
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/ask", data=_json.dumps({"question": "emergency procurement"}).encode(),
                                     headers={"X-Proxy-Secret": secret})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, _json.load(r), r.headers
        except urllib.error.HTTPError as e:
            return e.code, _json.load(e), e.headers
    try:
        code, out, headers = ask()
        assert code == 200 and out["answer"] == "Backup says [PG2024 §1.2]." and seen["secret"] == secret
        trace = _json.loads(headers["X-Ask-Trace"])
        assert trace["fallback"]["reason"] == "RuntimeError 402"
        chat = [c for c in trace["calls"] if c["kind"] == "chat"]
        assert chat[0]["model"] == "@cf/openai/gpt-oss-120b" and chat[0]["in"] == 7 and chat[0]["out"] == 3
        assert trace["cost"]["chat_calls"] == 1 and "seconds" in trace
        gate.acquire()  # someone else holds the only place and the queue is 0 long
        code, out, headers = ask()
        assert code == 503 and out["busy"] and headers["Retry-After"]
        gate.release()
    finally:
        srv.shutdown()
        worker.shutdown()


def _relief_unit(doc="PFC0326", heading="Special measures"):
    return dict(doc=doc, ref="", heading=heading, chapter="", page=None, label=f"[{doc}]", text="valid until 30th September 2026", note=None, chain=[])


def test_a_circular_past_its_date_gets_a_note_only_once_the_date_has_passed():
    from datetime import date
    from procurement.ask import lapsed_notes
    units, answer = [_relief_unit()], "Relief is available [PFC0326]."
    assert lapsed_notes(answer, units, date(2026, 9, 30)) == []  # its last day
    notes = lapsed_notes(answer, units, date(2026, 10, 1))
    assert len(notes) == 1 and "30 September 2026" in notes[0] and "Public Finance Department" in notes[0]
    assert lapsed_notes("Relief is available [PFC0326i].", [_relief_unit("PFC0326i")], date(2026, 11, 7)) == notes  # the amendment changed no end date
    assert lapsed_notes("The rule is in [PG2024 §5.16].", units, date(2026, 11, 7)) == []  # shown but not relied on


def test_a_limit_on_one_passage_does_not_mark_the_others_of_that_regulation():
    from datetime import date
    from procurement.ask import lapsed_notes, limits_passed
    other, limited = _relief_unit("FRA", "FR 113(3) - Other write-offs"), _relief_unit("FRA", "FR 113(6) - Write-off of loans and advances")
    today = date(2026, 11, 7)
    assert limits_passed([other], today) == []
    assert lapsed_notes("Arrears may be written off [FRA 113].", [other, limited], today) == []  # same label, and the answer is about (3)
    assert "31 March 2025" in lapsed_notes('Under FR 113(6) the balance may be written off [FRA 113].', [other, limited], today)[0]


def test_ask_gives_the_model_the_date_marks_the_passage_and_adds_the_note(tmp_path, monkeypatch):
    from datetime import date
    from procurement import ask as ask_mod
    idx = _index(tmp_path, monkeypatch)
    monkeypatch.setattr(ask_mod, "gather", lambda *a, **k: [_relief_unit()])
    llm = FakeLLM("Relief is available [PFC0326].")
    res = ask_mod.ask(idx, llm, "Is there relief?", today=date(2026, 11, 7))
    assert "Today's date: 2026-11-07" in llm.calls[-1] and "The stated end date, 30 September 2026, has passed" in llm.calls[-1]
    assert res.answer.startswith("Relief is available [PFC0326].") and "That date has passed" in res.answer and res.warnings == []
    llm = FakeLLM("Relief is available [PFC0326].")
    res = ask_mod.ask(idx, llm, "Is there relief?", today=date(2026, 8, 1))
    assert "stated end date" not in llm.calls[-1] and res.answer == "Relief is available [PFC0326]."


def test_every_time_limit_names_real_documents_and_a_real_date():
    from datetime import date
    from procurement.sources import DOCS, TIME_LIMITS
    ids = {d["id"] for d in DOCS}
    for lim in TIME_LIMITS:
        assert set(lim["docs"]) <= ids and date.fromisoformat(lim["until"]) and lim["shown"] in lim["note"]


def test_an_answer_cut_off_while_thinking_is_retried_without_thinking(tmp_path, monkeypatch):
    from procurement import ask as ask_mod
    idx = _index(tmp_path, monkeypatch)
    monkeypatch.setattr(ask_mod, "gather", lambda *a, **k: [_relief_unit("PG2024", "H")])

    class LLM:
        efforts = []

        def chat(self, system, messages, max_tokens=0, effort=None, **kw):
            self.efforts.append(effort)
            return Reply("", [], "length") if len(self.efforts) == 1 else Reply("Yes [PG2024].", [], "stop")
    r = ask_mod.ask(idx, LLM(), "q")
    assert LLM.efforts == ["low", "off"] and r.answer == "Yes [PG2024]." and "cut off" in r.warnings[0]


# ---------------------------------------------------------------- law passages from Bill Assistant (procurement/law.py), 2026-11-09
LAW_REPLY = {"passages": [{"cite": "[Act 25/2022 §3]", "key": "Act 25/2022", "section": 3,
                           "title": "Social Security Contribution Levy Act, No. 25 of 2022 (certified, 2022-09-20)",
                           "status": "Certified Act ... shows this section unchanged", "text": "(1) A levy ... at the rate of 2.5 per centum ..."}],
             "notes": {"Act 25/2022": "Social Security Contribution Levy Act, No. 25 of 2022 is the text as enacted in 2022."}}


class FakeLaw:
    timeout = 5

    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.asked = reply, error, []

    def evidence(self, query, k=4):
        self.asked.append(query)
        if self.error:
            raise self.error
        return self.reply


def test_needs_law_only_for_tax_levy_or_act_questions():
    from procurement.law import needs_law
    for q in ("Contractor asks for SSCL payments. Can we pay", "Is VAT payable on the advance?", "what does the Excise duty say",
              "Under the Inland Revenue Act, No. 24 of 2017 ...", "who pays the social security contribution levy"):
        assert needs_law(q), q
    for q in ("When can emergency procurement be used?", "what if the contractor missed the date in the letter of bid", "bid security validity"):
        assert not needs_law(q), q


def test_tax_question_gets_law_passages_notes_and_sources(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    llm = FakeLLM("The contractor owes the levy on its turnover [Act 25/2022 §3]; payment rests on the contract [PG2024 §1.2].")
    law = FakeLaw(LAW_REPLY)
    r = pa.ask(idx, llm, "Emergency procurement: contractor asks for SSCL levy payments, can we pay?", law=law)
    assert law.asked and "[Act 25/2022 §3]" in llm.calls[0] and "STATUS: Certified Act" in llm.calls[0]
    assert r.warnings == [] and r.law["passages"] == ["[Act 25/2022 §3]"] and not r.law["error"]
    assert r.answer.endswith("is the text as enacted in 2022.")                    # Bill Assistant's note, added by code
    assert any(s.startswith("[Act 25/2022 §3]") and s.endswith("from Bill Assistant") for s in r.sources())
    bad = pa.ask(idx, FakeLLM("It is [Act 25/2022 §9] and [PG2024 §1.2]."), "SSCL levy and emergency procurement", law=FakeLaw(LAW_REPLY))
    assert any("Act 25/2022 §9" in w for w in bad.warnings)                         # a law clause that was not received


def test_unreachable_bill_assistant_gives_the_fixed_pointer(tmp_path, monkeypatch):
    from procurement.law import FALLBACK_NOTE
    idx = _index(tmp_path, monkeypatch)
    r = pa.ask(idx, FakeLLM("Payment rests on the contract [PG2024 §1.2]."), "emergency procurement and the VAT levy",
               law=FakeLaw(error=TimeoutError("timed out")))
    assert r.answer.endswith(FALLBACK_NOTE) and any("Bill Assistant not consulted" in w for w in r.warnings)


def test_procurement_only_question_does_not_call_bill_assistant(tmp_path, monkeypatch):
    idx = _index(tmp_path, monkeypatch)
    law = FakeLaw(LAW_REPLY)
    r = pa.ask(idx, FakeLLM("Use it in exceptional circumstances [PG2024 §1.2]."), "When can emergency procurement be used?", law=law)
    assert not law.asked and not r.law and "Bill Assistant" not in r.answer


def test_procurement_image_copies_every_module_the_server_imports():
    """The Dockerfile lists procurement modules one by one: a new module (law.py, 2026-11-09) left out of it fails only on Cloudflare."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    copy = next(ln for ln in (root / "workers/procurement/Dockerfile").read_text().splitlines() if ln.startswith("COPY procurement/"))
    shipped = set(re.findall(r"procurement/(\w+)\.py", copy))
    imported = set()
    for mod in shipped:
        src = (root / "procurement" / f"{mod}.py").read_text()
        imported |= set(re.findall(r"from \.(\w+) import|from \. import (\w+)", src) and
                        [m for pair in re.findall(r"from \.(\w+) import|from \. import (\w+)", src) for m in pair if m])
    assert imported <= shipped, sorted(imported - shipped)
