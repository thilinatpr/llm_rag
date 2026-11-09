"""Base Acts: every Act that an indexed bill or Act amends must be in the index too (billrag/baseacts.py). Offline."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from billrag import acts, baseacts
from billrag.store import Store

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("upload_r2", ROOT / "scripts" / "upload_r2.py")
up = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(up)

IRA = "to amend the Inland Revenue Act, No. 24 of 2017"


def row(key, kind, date, long_title, title="X"):
    return {"key": key, "bill_no": 1, "year": int(date[:4]), "date": date + "T00:00:00Z", "title": title, "title_norm": title.lower(),
            "kind": kind, "long_title": long_title, "presented_by": None, "presented_on": None, "gazette_date": None, "printed_no": None,
            "n_pages": 1, "n_sections": 0, "pdf_path": "x", "summary": None}


def store_with(tmp_path, *rows):
    st = Store(tmp_path / "index.sqlite")
    for r in rows:
        st.replace_bill(r, [])
    st.commit()
    return st


def listing(*ids, english=True):
    return {i: {"title": "t", "english": english} for i in ids}


def test_references_read_numbered_chapter_and_several_acts():
    assert baseacts.references(IRA) == {"24/2017": "Inland Revenue Act"}
    both = baseacts.references("to amend the Immigrants and Emigrants Act (Chapter 351) and the Passport (Regulation) and Exit Permit Act, No. 53 of 1971")
    assert both["53/1971"] == "Passport (Regulation) and Exit Permit Act" and both["Chapter 351"] == "Immigrants and Emigrants Act"
    assert baseacts.references("To amended the Value Added Tax Act, No. 14 of 2002") == {"14/2002": "Value Added Tax Act"}
    assert baseacts.references("to repeal the Foo Act, No. 5 of 1999 and to provide for X") == {"5/1999": "Foo Act"}
    # a long title that only mentions an Act is not an amendment of it
    assert baseacts.references("to give effect to the Convention referred to in the Bar Act, No. 9 of 2001") == {}


def test_check_sorts_held_fetchable_waived_and_unresolved(tmp_path):
    st = store_with(
        tmp_path,
        row("013/2026", "GS", "2026-02-24", IRA),
        row("014/2026", "Bill", "2026-03-24", IRA),  # a second document naming the same Act
        row("031/2026", "GS", "2026-04-29", "to amend the Value Added Tax Act, No. 14 of 2002"),
        row("032/2026", "Bill", "2026-04-30", "to amend the Judicature Act, No. 2 of 1978"),
        row("033/2026", "Bill", "2026-05-01", "to amend the Rent Act, No. 7 of 1972"),
        row("034/2026", "Bill", "2026-05-02", "to amend the Mediation Boards Act, No. 72 of 1988"),
        row("Act 25/2022", "Act", "2022-05-01", None))
    st.replace_bill(row("035/2026", "Bill", "2026-05-03", "to amend the Social Security Contribution Levy Act, No. 25 of 2022"), [])
    st.commit()
    res = baseacts.check(st, listing("24/2017", "14/2002") | {"72/1988": {"title": "t", "english": False}}, waived={"7/1972": "owner: no text exists"})
    assert res["required"] == 6 and res["held"] == 1
    assert sorted(res["fetchable"]) == ["14/2002", "24/2017"]
    assert res["fetchable"]["24/2017"]["cited_by"] == ["013/2026", "014/2026"]
    assert list(res["waived"]) == ["7/1972"] and res["waived"]["7/1972"]["why"] == "owner: no text exists"
    assert sorted(res["unresolved"]) == ["2/1978", "72/1988"]
    assert "without an English PDF" in res["unresolved"]["72/1988"]["why"] and "not on documents.gov.lk" in res["unresolved"]["2/1978"]["why"]
    msg = baseacts.problem(res)
    assert "Judicature Act (2/1978)" in msg and "base_acts_waived.json" in msg
    assert baseacts.problem({**res, "unresolved": {}}) is None


def test_without_a_listing_everything_missing_is_unresolved_and_old_documents_are_not_checked(tmp_path):
    st = store_with(tmp_path, row("013/2026", "GS", "2026-02-24", IRA), row("300/2019", "Bill", "2019-01-01", "to amend the Old Act, No. 1 of 1950"))
    res = baseacts.check(st, None, waived={})
    assert list(res["unresolved"]) == ["24/2017"] and not res["fetchable"]  # the 2019 bill is before CHECK_SINCE
    st.replace_bill(row("Act 24/2017", "Act", "2017-07-01", None), [])
    st.commit()
    assert baseacts.check(st, None, waived={})["unresolved"] == {}


def test_an_amending_act_needs_its_base_too(tmp_path):
    st = store_with(tmp_path, row("Act 11/2026", "Act", "2026-06-03", IRA))
    assert list(baseacts.check(st, None, waived={})["unresolved"]) == ["24/2017"]


def test_listing_and_waiver_files(tmp_path):
    f = tmp_path / "acts.json"
    f.write_text(json.dumps([{"actNo": 24, "actSubNo": 2017, "descriptionEnglish": "Inland Revenue", "contents": [{"language": "ENGLISH"}]},
                             {"actNo": 0, "actSubNo": 2020, "contents": []},
                             {"actNo": 72, "actSubNo": 1988, "contents": [{"language": "SINHALA"}]}]), encoding="utf-8")
    assert baseacts.load_listing(f) == {"24/2017": {"title": "Inland Revenue", "english": True}, "72/1988": {"title": "", "english": False}}
    assert baseacts.load_listing(tmp_path / "none.json") is None
    assert baseacts.load_waived(tmp_path / "none.json") == {}
    assert baseacts.load_waived(ROOT / "evals" / "base_acts_waived.json") == {} or isinstance(baseacts.load_waived(), dict)


def test_base_acts_are_never_link_targets_for_bills(tmp_path):
    """An old principal Act sits in the index so amendments can be read against it, but a bill must not be 'linked' to it as the Act it became."""
    st = store_with(tmp_path, row("013/2026", "GS", "2026-02-24", IRA, "Inland Revenue (Amendment) - GS"),
                    row("Act 24/2017", "Act", "2017-07-01", None, "Inland Revenue"))
    assert acts.build(st)["acts"] == 0
    assert st.db.execute("SELECT COUNT(*) FROM act_links").fetchone()[0] == 0


def test_publish_refuses_an_index_with_a_missing_base_act_unless_waived(tmp_path, monkeypatch):
    store_with(tmp_path, row("013/2026", "GS", "2026-02-24", IRA)).db.close()
    with pytest.raises(up.PublishError, match=r"Inland Revenue Act \(24/2017\).*Nothing was changed"):
        up.check_base_acts(tmp_path)
    waive = tmp_path / "waived.json"
    waive.write_text(json.dumps({"24/2017": "owner decided"}), encoding="utf-8")
    monkeypatch.setattr(baseacts, "WAIVED_FILE", waive)
    up.check_base_acts(tmp_path)  # waived on purpose: passes


def test_restoring_the_live_index_does_not_apply_the_base_act_rule(tmp_path):
    """fetch_index uses check_pair: a live index that lacks base Acts must still restore. Only publishing is gated."""
    import inspect
    assert "check_base_acts" not in inspect.getsource(up.check_pair)
    assert "check_base_acts(root)" in inspect.getsource(up.publish_index)


def test_ingest_acts_can_be_limited_to_named_acts_of_any_year():
    import inspect
    from billrag import ingest
    assert "keys" in inspect.signature(ingest.ingest_acts).parameters and callable(ingest.add_base_act)


@pytest.fixture(autouse=True)
def _no_owner_waivers(tmp_path, monkeypatch):
    """Tests decide their own waivers: the committed evals/base_acts_waived.json (the owner's decisions) must not change their outcome."""
    from billrag import baseacts
    monkeypatch.setattr(baseacts, "WAIVED_FILE", tmp_path / "no-waivers.json")


def _refresh():
    spec = importlib.util.spec_from_file_location("refresh_bills", ROOT / "scripts" / "refresh_bills.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _work(tmp_path, listed):
    (tmp_path / "bills").mkdir()
    (tmp_path / "acts").mkdir()
    st = Store(tmp_path / "bills" / "index.sqlite")
    for r in (row("013/2026", "GS", "2026-02-24", IRA), row("032/2026", "Bill", "2026-04-30", "to amend the Rent Act, No. 7 of 1972")):
        st.replace_bill(r, [])
    st.commit()
    (tmp_path / "acts" / "acts.json").write_text(json.dumps(
        [{"actNo": no, "actSubNo": yr, "descriptionEnglish": "t", "contents": [{"language": "ENGLISH"}]} for no, yr in listed]), encoding="utf-8")
    return tmp_path, tmp_path / "bills"


def test_refresh_fetches_the_base_acts_it_can_and_counts_a_failed_fetch_as_missing(tmp_path):
    r = _refresh()
    work, bills = _work(tmp_path, [(24, 2017)])  # the Rent Act (7/1972) is not listed
    ran = []
    assert r.base_acts(lambda *cmd, step=None: ran.append((cmd, step)), work, bills) == ["24/2017"]
    assert [s for _, s in ran] == ["fetch base Acts", "parse base Acts"]
    assert ran[0][0][:4] == ("scripts/scrape_bills.py", "--source", "acts", "--out") and "--keys" in ran[0][0] and "24/2017" in ran[0][0]
    assert ran[1][0][:3] == ("-m", "billrag", "ingest-acts") and "24/2017" in ran[1][0]
    # nothing was ingested (the fake run did nothing), so the listed Act failed to arrive: it is missing, with the honest reason
    res = r.unresolved_base_acts(work, bills)
    assert sorted(res["unresolved"]) == ["24/2017", "7/1972"] and "failed" in res["unresolved"]["24/2017"]["why"]
    assert "not on documents.gov.lk" in res["unresolved"]["7/1972"]["why"]


def test_refresh_reports_base_acts_even_when_nothing_is_new(tmp_path):
    r = _refresh()
    work, bills = _work(tmp_path, [])
    assert sorted(r.base_check(work, bills)["unresolved"]) == ["24/2017", "7/1972"]


def test_the_report_keeps_base_acts_for_the_admin_panel():
    spec = importlib.util.spec_from_file_location("refresh_r2", ROOT / "scripts" / "refresh_r2.py")
    r2 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r2)
    rec = r2.status_record("report", "build", {"state": "failed", "base_acts_missing": {"15/1979": {"name": "Code of Criminal Procedure Act"}}})
    assert rec["base_acts_missing"]["15/1979"]["name"] == "Code of Criminal Procedure Act"
