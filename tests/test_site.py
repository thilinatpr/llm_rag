"""Offline tests for scripts/build_site.py (SEO phase 2): synthetic index, no network."""
import importlib.util
import re
import sqlite3
import sys
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("build_site", Path(__file__).resolve().parent.parent / "scripts" / "build_site.py")
bs = importlib.util.module_from_spec(SPEC)
sys.modules["build_site"] = bs
SPEC.loader.exec_module(bs)


def make_index(d: Path):
    db = sqlite3.connect(d / "index.sqlite")
    db.executescript("""
    CREATE TABLE bills(key TEXT PRIMARY KEY, bill_no INT, year INT, date TEXT, title TEXT, title_norm TEXT, kind TEXT, long_title TEXT,
      presented_by TEXT, presented_on TEXT, gazette_date TEXT, printed_no TEXT, n_pages INT, n_sections INT, pdf_path TEXT, summary TEXT);
    CREATE TABLE act_links(bill_key TEXT PRIMARY KEY, act_key TEXT NOT NULL, score REAL, title_sim REAL, direct INT);
    CREATE TABLE act_sections(act_key TEXT NOT NULL, bill_key TEXT NOT NULL, bill_section INT, act_section INT, ratio REAL, status TEXT NOT NULL);
    CREATE TABLE bill_meta(bill_key TEXT PRIMARY KEY, title_norm TEXT, family TEXT, stage TEXT, bill_type TEXT, amends TEXT, is_latest INT, latest_family TEXT, tags TEXT);
    """)
    rows = [  # key, no, year, date, title, kind, long_title, summary
        ("Act 5/2026", 5, 2026, "2026-09-01T00:00:00.000Z", "Widget (Amendment) Act, No. 5 of 2026", "Act", "to Amend the Widget OrdinAnce (ChApter 9)",
         "Amends the widget law. Adds a fine.\nKeywords: widget, fine"),
        ("010/2026", 10, 2026, "2026-06-01T00:00:00.000Z", "Widget (Amendment) - Bill", "Bill", "to amend the Widget Ordinance", "Bill summary. Keywords: x"),
        ("004/2026", 4, 2026, "2026-05-01T00:00:00.000Z", "Widget (Amendment) - GS", "GS", "to amend the Widget Ordinance", None),
        ("020/2026", 20, 2026, "2026-07-01T00:00:00.000Z", "<script>alert(1)</script> Foundation (Incorporation) - Bill", "Bill", "to incorporate it", "A foundation."),
        ("099/2020", 99, 2020, "2020-01-01T00:00:00.000Z", "Old - Bill", "Bill", "to be old", "Old."),
    ]
    for r in rows:
        db.execute("INSERT INTO bills(key,bill_no,year,date,title,kind,long_title,summary,n_sections) VALUES(?,?,?,?,?,?,?,?,?)", (*r[:5], r[5], r[6], r[7], 3))
    db.executemany("INSERT INTO bill_meta(bill_key,family) VALUES(?,?)", [("010/2026", "004/2026"), ("004/2026", "004/2026"), ("020/2026", "020/2026"), ("099/2020", "099/2020")])
    db.executemany("INSERT INTO act_links VALUES(?,?,?,?,?)", [("010/2026", "Act 5/2026", .9, 1, 1), ("004/2026", "Act 5/2026", .9, 1, 1)])
    db.executemany("INSERT INTO act_sections VALUES('Act 5/2026',?,?,?,?,?)", [(b, i, i, 1.0, st) for b in ("010/2026", "004/2026")
                   for i, st in enumerate(["same", "same", "dropped"], 1)])
    db.commit()
    db.close()


def build(tmp_path):
    idx = tmp_path / "idx"
    idx.mkdir()
    make_index(idx)
    out = tmp_path / "site"
    out.mkdir()
    return bs.build(idx, out, 2026, 2024), out


def test_pages_hubs_and_sitemap_are_written(tmp_path):
    n, out = build(tmp_path)
    assert n["acts"] == 1 and n["bills"] == 2  # old 2020 bill is out of scope; GS + Bill of one title are one family
    for f in ["acts/5-2026.html", "acts/index.html", "bills/004-2026.html", "bills/020-2026.html", "bills/index.html", "sitemap-docs.xml"]:
        assert (out / f).exists(), f
    assert not (out / "bills" / "099-2020.html").exists()
    sm = (out / "sitemap-docs.xml").read_text()
    assert "<loc>https://citizenlk.com/acts/5-2026</loc>" in sm and "<loc>https://citizenlk.com/bills/004-2026</loc>" in sm
    assert "099-2020" not in sm


def test_act_page_is_clean_and_indexable(tmp_path):
    _, out = build(tmp_path)
    h = (out / "acts" / "5-2026.html").read_text()
    assert '<link rel="canonical" href="https://citizenlk.com/acts/5-2026">' in h
    assert "index,follow" in h and "noindex" not in h
    assert "Keywords" not in h and "widget, fine" not in h  # the search keyword line is not for readers
    assert "Ordinance (Chapter 9)" in h  # stray capitals from the PDF text are repaired
    assert h.count("Where it came from") == 1 and h.count('href="/bills/004-2026"') == 1  # Gazette text + Bill = one entry
    assert "2 appear in the Act in the same words" in h and "not in the Act (the Act may still deal with the topic" in h
    assert "Not legal advice" in h and "Not a government service" in h


def test_bill_without_an_act_never_says_it_failed(tmp_path):
    _, out = build(tmp_path)
    h = (out / "bills" / "020-2026.html").read_text()
    assert "not been shown to have passed" in h and "does not mean it failed" in h
    assert not re.search(r"(?<!not mean it )\b(failed|rejected|defeated)\b", h)
    assert "<script>alert(1)</script>" not in h and "&lt;script&gt;" in h  # titles are escaped
    linked = (out / "bills" / "004-2026.html").read_text()
    assert 'href="/acts/5-2026"' in linked and "became" in linked


def test_page_text_follows_the_design_rules(tmp_path):
    _, out = build(tmp_path)
    for f in out.rglob("*.html"):
        text = re.sub(r"<style>.*?</style>|<script.*?</script>", "", f.read_text(), flags=re.S)
        assert not re.search(r"[඀-෿஀-௿]", text), f  # English only (DESIGN.md principle 7)
        assert not re.search(r"\b(model|embedding|retrieval|LLM|auto-generated)\b", text, re.I), f  # no technology words (principle 2)


def test_fix_case_keeps_names_and_acronyms():
    assert bs.fix_case("to Amend the Poisons, Opium And DAngerous Drugs OrdinAnce (ChApter 218)") == \
        "to Amend the Poisons, Opium And Dangerous Drugs Ordinance (Chapter 218)"
    assert bs.fix_case("the UnV Foundation and the NPC") == "the UnV Foundation and the NPC"
