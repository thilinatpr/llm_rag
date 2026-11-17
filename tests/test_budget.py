"""Budget service: parser, reconciliation, speech chunker, index. Offline (synthetic pages);
the gold test runs only when the downloaded PDFs are present (data/ is git-ignored)."""
import json
import os
import sqlite3

import pytest

from budget import index, parse, speech


def test_words_to_int():
    w = parse.words_to_int
    assert w("four thousand nine hundred ninety-two billion seven hundred seventy-three million three hundred eighty-eight thousand") == 4_992_773_388_000
    assert w("twenty-seven billion seven hundred seventy million three hundred ninety thousand") == 27_770_390_000
    assert w("rupees banana") is None


def first_schedule_pages():
    # a normal programme, a programme printed in a single cell, and a nil amount
    p = ("Appropriation\n8\nFIRST SCHEDULE\n"
         "Head \t1\tHis Excellency the President\t\n"
         "\tProgramme 01\tOperational Activities\t\n 1,000 \t 250,000 \n"
         "Head \t2\tOffice X\t\n"
         "\tProgramme 01\tOperational Activities\t 1,000 \t 50,000 \n"
         "\tProgramme 02 - Development Activities\t 2,000 \t -   \n"
         "Total\t\n 3,000 \t 300,000 \n")
    return ["cover", p, "SECOND SCHEDULE"]


def test_first_schedule_single_cell_programme_and_nil():
    rows, tot = parse.parse_first(first_schedule_pages(), 2, 2)
    assert [(r["head"], r["programme"], r["recurrent"], r["capital"]) for r in rows] == [
        (1, 1, 1000, 250000), (2, 1, 1000, 50000), (2, 2, 2000, 0)]
    assert tot["first_recurrent"] == 3000 and tot["first_capital"] == 300000
    # sums: recurrent 4000 vs printed 3000 -> check() must flag it (a dropped or invented row is never silent)
    bill = {"estimate": rows, "totals": {**tot, "second_recurrent": 0, "second_capital": 0, "second_total": 0}, "charged": [], "sections": []}
    assert any("First Schedule recurrent" in p for p in parse.check(bill))


def test_second_schedule_margin_numbers_are_not_heads():
    titles = {5: "Office of the Cabinet", 16: "Parliament"}
    page = ("SECOND SCHEDULE\n5\n10\n16\nParliament\nArticle 65 of the\nConstitution\nProgramme 01-\nOperational\nActivities\n 3,000,000 \n 3,000,000 \n"
            "5\n10\n15\nTotal\n 3,000,000 \n 0 \n 3,000,000 \n")
    rows, tot = parse.parse_second(["x", page], 2, 2, titles)
    assert [(r["head"], r["vals"]) for r in rows] == [(16, [3000000, 3000000])]


SPEECH_PAGES = [
    "81st Budget Speech\n2027\n12th November 2026\n",  # cover
    "1 \nBudget Speech – 2027 \n1. Preamble \nHonourable Speaker,\nWe present this Budget.\n"
    "2. Energy \nHonourable Speaker,\n2.1 Electricity \nRs. 500 million has been allocated for solar.\n"
    "3. \nIrrigation \nRs. 200 million has been allocated for canals.\n",
    "2 \nBudget Speech – 2027 \nMore irrigation text here for the canals and reservoirs.\n"
    "1. Lanka Sugar Company\n2. Janatha Estates\nAnnexure I\nthis must not be in the speech\n",
]


def test_speech_chapters_pages_and_annex_stop():
    ch = speech.parse_speech(SPEECH_PAGES)
    refs = [(c["ref"], c["heading"], c["page"]) for c in ch]
    assert ("1", "Preamble", "1") in refs
    assert ("2.1", "Energy / Electricity", "1") in refs
    assert ("3", "Irrigation", "1") in refs                       # "3." and the title on the next line
    assert any("More irrigation" in c["text"] and c["ref"] == "3" for c in ch)  # list items "1. Lanka.." are not chapters
    assert not any("must not be" in c["text"] for c in ch)


def test_index_search_and_status(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(index.SCHEMA)
    index.add_chunk(db, "Speech 2027", "speech", "17.2", "Agriculture / Irrigation", "40", "Rs. 2,000 million for minor irrigation schemes")
    index.add_chunk(db, "Speech 2027", "speech", "8", "Education", "20", "Free textbooks for school students")
    hits = index.search(db, "irrigation schemes")
    assert hits and hits[0]["ref"] == "17.2"


GOLD = [json.loads(line) for line in open(os.path.join(os.path.dirname(__file__), "..", "evals", "budget_eval.jsonl"))]
DB = os.path.join(os.path.dirname(__file__), "..", "data", "budget", "index.sqlite")


@pytest.mark.skipif(not os.path.exists(DB), reason="needs data/budget/index.sqlite (python -m budget build)")
@pytest.mark.parametrize("item", GOLD, ids=lambda i: i["id"])
def test_gold(item):
    db = index.connect(DB)
    if item["table"] == "sql":
        assert db.execute(item["sql"]).fetchone()[0] == item["expect"]
    else:
        hits = index.search(db, item["find"], 5, (item["kind"],))
        assert any(item["find"] in " ".join(r["text"].split()) for r in hits)  # the PDF wraps lines mid-phrase


# ------------------------------------------------------------------ answer layer
from budget import ask  # noqa: E402


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(index.SCHEMA)
    index.add_chunk(db, ask.BILL, "estimate", "head 118", "Head 118 Minister of Agriculture", "23",
                    "Head 118: Minister of Agriculture. Programme 02 Development Activities: recurrent Rs. 55,587,000,000, capital Rs. 78,439,000,000.")
    index.add_chunk(db, ask.BILL, "group", "group Ministry of Agriculture", "Ministry of Agriculture (ministry total)", "23",
                    "Ministry of Agriculture: recurrent Rs. 99,000,000,000, capital Rs. 119,000,000,000.")
    index.add_chunk(db, ask.BILL, "facts", "totals", "Key figures of the Bill: total expenditure", None, "voted together Rs. 4,992,773,388,000.")
    index.add_chunk(db, ask.SPEECH, "speech", "17.2", "Agriculture / Irrigation Sector", "40", "Rs. 19,500 million has been allocated for the Mahaweli programme.")
    index.add_chunk(db, ask.SPEECH, "annex", "annex II #3", "Annexure II: new expenditure proposal (Education)", "84",
                    "New expenditure proposal no. 3 (Education): spectacles. Proposed annual allocation Rs. 100 million (capital).")
    return db


class Scripted:
    def __init__(self, text):
        self.text, self.calls = text, []

    def chat(self, system, messages, tools=(), max_tokens=0, effort=None, on_token=None):
        from billrag.llm import Reply
        self.calls.append(messages[0]["content"])
        return Reply(text=self.text)


def test_gather_lanes_and_head_number():
    u = ask.gather(_db(), "Head 118 irrigation")
    labels = [x["label"] for x in u]
    assert labels[0] == "[Bill Head 118]"                     # a named head comes first
    assert any(l.startswith("[Speech") for l in labels)       # the speech lane is always represented
    assert "[Bill Totals]" in [x["label"] for x in ask.gather(_db(), "what is the total budget")]


def test_verify_citations_incl_semicolon_and_page_suffix():
    units = ask.gather(_db(), "Head 118 development allocation mahaweli")
    ok = "x [Bill Head 118] y [Speech §17.2, p.40] z [Bill Head 118; Speech §17.2]"
    assert ask.verify(ok, units) == []
    bad = ask.verify("x [Bill Head 999] [Bill = description] [Speech §3]", units)
    assert set(bad) == {"Bill Head 999", "Bill = description", "Speech §3"}


def test_verify_numbers_accepts_conversions_flags_calculated_sums():
    units = ask.gather(_db(), "Head 118 development allocation mahaweli")
    good = "Recurrent Rs. 55,587,000,000 (Rs. 55,587 million, about Rs. 55.6 billion) and Rs. 19,500 million (Rs. 19.5 billion)."
    assert ask.verify_numbers(good, units) == []
    # the model adds 55,587,000,000 + 78,439,000,000 itself: not printed anywhere, so it must be flagged
    assert ask.verify_numbers("Together Rs. 134,026,000,000 (Rs. 134,026 million).", units) != []


def test_ask_adds_fixed_notes_and_warns_on_figures_without_citation():
    db = _db()
    res = ask.ask(db, Scripted("Rs. 78,439,000,000 is the capital allocation [Bill Head 118]."), "Head 118 capital")
    assert res.warnings == [] and ask.BILL_NOTE in res.answer and ask.SPEECH_NOTE not in res.answer
    res = ask.ask(db, Scripted("Rs. 19,500 million is allocated."), "mahaweli")
    assert any("figures but no citations" in w for w in res.warnings)
    res = ask.ask(db, Scripted("The passages do not answer this."), "head 118 capital")
    assert res.warnings == []                                  # a refusal needs no citation
