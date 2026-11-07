"""The scheduled refresh's Python side: the report refresh_bills.py writes, the manifest and status records the admin panel reads. Offline."""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_listing_keys_are_the_indexs_own_keys_and_ignore_old_years(tmp_path):
    r = load("refresh_bills")
    (tmp_path / "bills").mkdir()
    (tmp_path / "acts").mkdir()
    bills = [dict(billNo=7, billSubNo=2026, date="2026-10-01T00:00:00.000Z"), dict(billNo=9, billSubNo=2020, date="2020-01-01T00:00:00.000Z")]
    acts = [dict(actNo=25, actSubNo=2026), dict(actNo=0, actSubNo=2026), dict(actNo=3, actSubNo=2019)]
    (tmp_path / "bills/bills.json").write_text(json.dumps(bills))
    (tmp_path / "acts/acts.json").write_text(json.dumps(acts))
    assert r.listing_keys(tmp_path, 2025) == {"007/2026", "Act 25/2026"}


def test_report_is_written_for_a_run_that_stops_and_for_a_crash(tmp_path, monkeypatch):
    r = load("refresh_bills")
    out = tmp_path / "report.json"

    def crash(a, report):
        raise RuntimeError("boom")

    monkeypatch.setattr(r, "run_refresh", lambda a, report: report.update(state="built") or 0)
    assert r.main(["--work", str(tmp_path), "--report", str(out)]) == 0
    assert json.loads(out.read_text())["state"] == "built"
    monkeypatch.setattr(r, "run_refresh", crash)
    try:
        r.main(["--work", str(tmp_path), "--report", str(out)])
        raise AssertionError("the crash must still show")
    except RuntimeError:
        pass
    rep = json.loads(out.read_text())
    assert rep["state"] == "failed" and "boom" in rep["error"]


def test_the_manifest_lists_what_the_index_holds(tmp_path):
    up = load("upload_r2")
    db = sqlite3.connect(tmp_path / "index.sqlite")
    db.execute("CREATE TABLE bills(key TEXT)")
    db.executemany("INSERT INTO bills VALUES (?)", [("002/2026",), ("Act 5/2026",), ("001/2026",)])
    db.commit()
    db.close()
    assert up.index_keys(tmp_path) == ["001/2026", "002/2026", "Act 5/2026"]

    class S3:
        puts = []

        def put_object(self, **kw):
            self.puts.append(kw)

    s3 = S3()
    up.write_manifest(s3, "b", "bills", tmp_path)
    (put,) = s3.puts
    assert put["Key"] == "bills/indexed-keys.json"
    assert json.loads(put["Body"])["keys"] == ["001/2026", "002/2026", "Act 5/2026"]


def test_status_record_keeps_what_the_panel_shows_and_leaves_out_empties():
    rr = load("refresh_r2")
    rec = rr.status_record("built", "build", {"new_bills": {"001/2026": "T"}, "mrr": 0.88, "problems": [], "unindexed": ["x"], "secret": "no"}, "https://run")
    assert rec["state"] == "built" and rec["mode"] == "build" and rec["run_url"] == "https://run"
    assert rec["new_bills"] == {"001/2026": "T"} and rec["mrr"] == 0.88
    assert "problems" not in rec and "secret" not in rec and "unindexed" not in rec  # unindexed goes to skipped.json, not the status
