"""Certified Acts next to their bills: parsing, linking, the section comparison, and how answers use them. Offline."""
import os

for _k in ("R2_BUCKET", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
    os.environ.pop(_k, None)

from billrag import acts, resolve, translate
from billrag.parse import act_cover_ok, certified_on, parse_act_pages
from billrag.pipeline import CITE, Evidence, Pipeline, Result, version_tag
from billrag.retrieve import Retriever
from billrag.store import Store, normalize_ref

COMMON = ("The Commission shall publish every declaration of assets within thirty days of receiving it, and any person may "
          "inspect a published declaration at the office of the Commission during its ordinary hours of business.")


def section(no, text):
    return {"kind": "section", "section_no": no, "page": no, "text": text, "ctx": text}


def add(store, key, kind, date, title, secs, tnorm=None, overview=True):
    no, year = key.replace("Act ", "").split("/")
    row = {"key": key, "bill_no": int(no), "year": int(year), "date": date + "T00:00:00.000Z", "title": title,
           "title_norm": tnorm or "anti corruption amendment", "kind": kind, "n_sections": len(secs)}
    chunks = [{"kind": "overview", "page": 1, "text": f"{key} overview", "ctx": f"{key} overview"}] if overview else []
    store.replace_bill(row, chunks + [section(n, t) for n, t in secs.items()])


AMEND = "Section {} of the principal enactment is hereby amended by the substitution for the word twenty of the word ten in paragraph ({})."


def populate(store):
    """Bill: 1 short title, 2 kept, 3 dropped (a bail rule), 4 kept (Act s.3), 5 changed (Act s.4: twenty -> ten)."""
    bill = {1: "This Act may be cited as the Anti-Corruption (Amendment) Act, No. of 2026.", 2: COMMON,
            3: "All offences under this Act shall be cognizable and non-bailable as provided for in the Bail Act, unless the "
               "High Court on application directs otherwise for exceptional reasons recorded in writing by the judge.",
            4: AMEND.format(90, "a"), 5: "The term of office of a member of the Commission shall be twenty years from appointment "
               "and that member shall not be eligible for a further appointment under any circumstances at all."}
    act = {1: "This Act may be cited as the Anti-Corruption (Amendment) Act, No. 25 of 2026.", 2: COMMON, 3: AMEND.format(90, "a"),
           4: bill[5].replace("twenty years", "ten years")}
    add(store, "051/2026", "GS", "2026-07-27", "Anti-Corruption (Amendment) - GS", bill)
    add(store, "063/2026", "Bill", "2026-08-19", "Anti-Corruption (Amendment) - Bill", bill)
    add(store, "Act 25/2026", "Act", "2026-10-19", "Anti-Corruption (Amendment) Act, No. 25 of 2026", act)
    store.commit()
    return acts.build(store)


class Quiet:
    def search(self, *a, **k):
        return []


def pipeline(store):
    return Pipeline(store, Quiet(), None)


def evidence(store, key, no, score=3):
    r = store.db.execute("SELECT id FROM chunks WHERE bill_key=? AND section_no=?", (key, no)).fetchone()
    return Evidence(store.chunks_by_ids([r[0]])[r[0]], score)


def test_references_tell_acts_from_bills():
    assert normalize_ref("Act No. 25 of 2026") == "Act 25/2026"
    assert normalize_ref("Act, No. 9 of 2023") == "Act 9/2023"
    assert normalize_ref("Act 25/2026") == "Act 25/2026"
    assert normalize_ref("25/2026") == "025/2026"  # a bare number stays a bill number, as it always was
    assert CITE.findall("see [Act 25/2026 §7] and [063/2026 §8]") == [("Act 25/2026", "7"), ("063/2026", "8")]
    assert translate.cites("x [Act 25/2026 §7]") == ["[Act 25/2026 §7]"]


def test_act_parser_removes_page_furniture_and_keeps_the_law():
    pages = ["ANTI-CORRUPTION (AMENDMENT)\nACT, No. 25 OF 2026\n[Certified on 19th of October, 2026]\nPrinted on the Order of Government",
             "",
             "3\nAnti-Corruption (Amendment) Act, No. 25 of 2026\nBE it enacted by the Parliament as follows:-\n"
             "1. This Act may be cited as the Anti-Corruption\n(Amendment) Act, No. 25 of 2026.\n2. A person may apply for a certiﬁcate.",
             "4Anti-Corruption (Amendment) Act, No. 25 of 2026\nThe Commission shall reply.\n"
             "English Acts of the Parliament can be purchased at the Bureau,\nColombo 8."]
    p = parse_act_pages(pages, "Anti-Corruption (Amendment)", 25, 2026)
    secs = {s.no: s.text for s in p.sections}
    assert p.cover["certified"] == "2026-10-19"
    assert "No. 25 of 2026." in secs[1]  # the short title is a sentence, not a running head
    assert "certificate" in secs[2]  # ligature repaired, so a search finds it
    assert "The Commission shall reply." in secs[2]
    assert "Anti-Corruption (Amendment) Act" not in secs[2] and "purchased" not in secs[2]


def test_act_cover_rejects_a_file_for_something_else():
    assert act_cover_ok("ACT, No. 25 OF 2026", 25, 2026)
    assert not act_cover_ok("A BILL to amend the Anti-Corruption Act", 25, 2026)
    assert not act_cover_ok("ACT, No. 26 OF 2026", 25, 2026)
    assert certified_on("no such line") is None


def test_one_changed_word_in_a_long_clause_is_not_the_same():
    long = acts._toks(COMMON + " " + COMMON + " for a term of twenty years")
    other = acts._toks(COMMON + " " + COMMON + " for a term of ten years")
    assert acts.material_differences(long, other) == [("twenty", "ten")]
    r = acts.align({1: " ".join(long)}, {1: " ".join(other)})
    assert r[0][3] == "changed" and r[0][2] > 0.95  # scores high, is still a change


def test_extraction_noise_is_not_a_change():
    pairs = [("in sub section 5 the Minister may act", "in subsection 5 the Minister may act"),  # word split by the PDF
             ("under section 54 AA the Minister", "under section 54AA the Minister"),
             ("the Minister may act now", "the Minister may of section 70 of the principal enactment act now"),  # a margin note
             ("This Act, No. of 2026 applies", "This Act, No. 25 of 2026 applies")]  # the bill leaves its number blank
    for x, y in pairs:
        assert acts.material_differences(acts._toks(x), acts._toks(y)) == [], (x, y)


def test_align_finds_the_dropped_clause_and_the_renumbering():
    s = Store(":memory:")
    populate(s)
    rows = {r["bill_section"]: dict(r) for r in s.db.execute("SELECT * FROM act_sections WHERE bill_key='063/2026'")}
    assert rows[3]["status"] == "dropped" and rows[3]["act_section"] is None
    assert (rows[4]["act_section"], rows[4]["status"]) == (3, "same")
    assert (rows[5]["act_section"], rows[5]["status"]) == (4, "changed")


def test_amendments_of_different_sections_are_not_matched_because_they_read_alike():
    r = acts.align({7: AMEND.format(81, "a"), 8: AMEND.format(82, "a")}, {7: AMEND.format(82, "a")})
    assert (7, None, 0.0, "dropped") in r
    assert any(x[:2] == (8, 7) for x in r)


def test_a_clause_moved_into_a_schedule_is_not_called_dropped():
    bill = {1: COMMON * 3}
    r = acts.align(bill, {1: "Something else entirely, with nothing in common at all."}, act_full=COMMON * 3)
    assert ("moved" in {x[3] for x in r}) and ("dropped" not in {x[3] for x in r})


def test_two_acts_with_one_title_link_by_text():
    s = Store(":memory:")
    text_a, text_b = COMMON, "The Minister may by Order published in the Gazette name further dangerous drugs for the purposes of this Ordinance after consulting the Authority."
    add(s, "067/2026", "Bill", "2026-08-01", "Poisons - Bill", {1: text_a}, "poisons")
    add(s, "068/2026", "Bill", "2026-08-01", "Poisons - Bill", {1: text_b}, "poisons")
    add(s, "Act 26/2026", "Act", "2026-10-30", "Poisons Act, No. 26 of 2026", {1: text_a}, "poisons")
    add(s, "Act 27/2026", "Act", "2026-10-30", "Poisons Act, No. 27 of 2026", {1: text_b}, "poisons")
    acts.build(s)
    assert acts.act_for_bill(s, "067/2026")["key"] == "Act 26/2026"
    assert acts.act_for_bill(s, "068/2026")["key"] == "Act 27/2026"


def test_an_earlier_round_did_not_itself_become_the_act():
    s = Store(":memory:")
    add(s, "541/2024", "Bill", "2024-08-01", "Trusts - Bill", {1: COMMON}, "trusts")
    add(s, "606/2025", "Bill", "2025-06-01", "Trusts - Bill", {1: COMMON}, "trusts")
    add(s, "Act 12/2025", "Act", "2025-08-01", "Trusts Act, No. 12 of 2025", {1: COMMON}, "trusts")
    acts.build(s)
    assert acts.act_for_bill(s, "606/2025")["direct"] and not acts.act_for_bill(s, "541/2024")["direct"]
    assert acts.bills_for_act(s, "Act 12/2025") == ["606/2025"]
    assert acts.bill_section(s, "541/2024", 1) is None
    assert "earlier round" in acts.status_lines(s, "541/2024", 1)


def test_a_recurring_title_years_apart_with_other_text_is_not_linked():
    s = Store(":memory:")
    add(s, "100/2020", "Bill", "2020-01-01", "Finance - Bill", {1: COMMON}, "finance")
    add(s, "Act 1/2026", "Act", "2026-01-01", "Finance Act, No. 1 of 2026", {1: "A wholly different text about the customs duty on imported goods and how it is charged."}, "finance")
    acts.build(s)
    assert acts.act_for_bill(s, "100/2020") is None


def test_status_never_claims_more_than_we_know():
    s = Store(":memory:")
    populate(s)
    add(s, "070/2026", "Bill", "2026-09-01", "Terrorism - Bill", {1: "x"}, "terrorism")
    add(s, "100/2021", "Bill", "2021-01-01", "Old - Bill", {1: "y"}, "old")
    assert "The wording of this clause is not in the Act" in acts.status_lines(s, "063/2026", 3)
    assert "Same text as Act 25/2026 s.3" in acts.status_lines(s, "063/2026", 4)
    assert "differs" in acts.status_lines(s, "063/2026", 5)
    now = acts.status_lines(s, "070/2026", 1)
    assert "We hold no certified Act" in now and "2026-10-19" in now  # no Act held is not "it failed"
    assert "not checked" in acts.status_lines(s, "100/2021", 1)  # older than the Acts we hold: no claim either way
    act = acts.status_lines(s, "Act 25/2026", 2)
    assert "law as passed" in act and "051/2026, 063/2026" in act  # "has the bill become law?" needs the link stated


def test_answer_uses_the_act_and_keeps_what_differs():
    s = Store(":memory:")
    populate(s)
    res = Result(question="q", answer="")
    out = pipeline(s).apply_acts("q", [evidence(s, "063/2026", 4), evidence(s, "063/2026", 5), evidence(s, "063/2026", 3)], None, res)
    got = [(e.key, e.section) for e in out]
    assert got[:3] == [("Act 25/2026", 3), ("Act 25/2026", 4), ("063/2026", 5)]  # same -> replaced; changed -> Act first, then the bill
    assert ("063/2026", 4) not in got
    assert ("063/2026", 3) in got and ("Act 25/2026", None) in got  # the dropped clause stays, with the Act's overview beside it
    assert {"bill": "063/2026", "section": 3, "act": "Act 25/2026", "act_section": None, "status": "dropped"} in res.acts


def test_naming_the_bill_or_comparing_keeps_its_clause():
    s = Store(":memory:")
    populate(s)
    for q, keys in (("q", ["063/2026"]), ("how did the bill change in committee?", None)):
        out = pipeline(s).apply_acts(q, [evidence(s, "063/2026", 4)], keys, Result(question=q, answer=""))
        assert [(e.key, e.section) for e in out] == [("Act 25/2026", 3), ("063/2026", 4)]


def test_status_reaches_the_answer_prompt_without_being_a_citation():
    s = Store(":memory:")
    populate(s)
    seen = {}

    class LLM:
        def chat(self, system, messages, **k):
            seen["user"] = messages[0]["content"]
            return type("R", (), {"text": "Proposed, not enacted in these words [063/2026 §3].", "truncated": False})()

    p = Pipeline(s, Quiet(), LLM())
    res = Result(question="q", answer="")
    p.summarise("q", [evidence(s, "063/2026", 3)], True, "", res)
    assert "STATUS: Proposal; this bill became Act 25/2026" in seen["user"]
    assert not CITE.search(seen["user"].split("STATUS:")[1].split("\n")[0])


def test_verify_accepts_act_citations_and_catches_a_bills_number_on_the_act():
    s = Store(":memory:")
    populate(s)
    ev = [evidence(s, "Act 25/2026", 3)]
    assert Pipeline.verify("ok [Act 25/2026 §3]", ev) == []
    assert Pipeline.verify("wrong [Act 25/2026 §4]", ev) == ["Act 25/2026 §4"]  # §4 of the bill is §3 of the Act
    assert Pipeline.verify("wrong [063/2026 §3]", ev) == ["063/2026 §3"]


def test_identical_text_prefers_the_act_and_does_not_pair_their_section_numbers():
    s = Store(":memory:")
    populate(s)
    hits = Retriever(s).search("declaration of assets published inspect", k=5, kinds=("section",))
    same = [c for c in hits if "inspect a published" in c["text"]]
    assert [c["bill_key"] for c in same] == ["Act 25/2026"]
    assert not [k for k in same[0].get("also_in", []) if not acts.is_act(k)]


def test_versions_leave_acts_out_and_coverage_counts_them_apart():
    s = Store(":memory:")
    populate(s)
    assert {v["key"] for v in s.versions("anti corruption amendment", "2026-08-19T")} == {"051/2026", "063/2026"}
    from billrag import meta
    meta.build(s)
    c = s.coverage()
    assert c["acts"] == 1 and c["bills"] == 1 and [r["key"] for r in c["recent"]] == ["063/2026"]
    assert c["last"] == "2026-10-19"
    assert "Act 25/2026" not in resolve.which_bill_reply(s)


def test_labels_and_titles():
    c = {"title": "Anti-Corruption (Amendment) Act, No. 25 of 2026", "bill_kind": "Act", "date": "2026-10-19T00"}
    assert version_tag(c) == "Anti-Corruption (Amendment) Act, No. 25 of 2026 (certified, 2026-10-19)"
    assert resolve.base_title(c["title"]) == "Anti-Corruption (Amendment)"


def test_an_index_without_the_tables_still_works():
    s = Store(":memory:")
    add(s, "070/2026", "Bill", "2026-09-01", "Terrorism - Bill", {1: COMMON}, "terrorism")
    e = evidence(s, "070/2026", 1)
    assert pipeline(s).apply_acts("q", [e], None, Result(question="q", answer="")) == [e]
    assert acts.act_for_bill(s, "070/2026") is None
