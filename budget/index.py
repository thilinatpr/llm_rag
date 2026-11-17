"""SQLite index for the Budget service: chunks (+FTS5) and the structured tables behind them.

Tables
  docs       one row per source document (key, kind, title, date, source, status)
  chunks     text chunks for search: kind speech | annex | section | estimate | charged
  estimate   First Schedule rows (head, programme, recurrent, capital, bill)
  charged    Second Schedule rows
  proposals  Annexure II of the speech (new expenditure proposals, Rs million)
Build refuses a bill whose printed totals do not reconcile (`parse.check`).
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from . import parse as bill_parse
from . import speech as speech_parse

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(key TEXT PRIMARY KEY, kind TEXT, title TEXT, date TEXT, source TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, doc TEXT, kind TEXT, ref TEXT, heading TEXT, page TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(heading, text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS estimate(doc TEXT, head INTEGER, head_title TEXT, grp TEXT, programme INTEGER, programme_name TEXT, recurrent INTEGER, capital INTEGER, page INTEGER);
CREATE TABLE IF NOT EXISTS charged(doc TEXT, head INTEGER, title TEXT, authority TEXT, programme TEXT, recurrent INTEGER, capital INTEGER, total INTEGER, page INTEGER);
CREATE TABLE IF NOT EXISTS proposals(doc TEXT, serial INTEGER, sector TEXT, proposal TEXT, amount_million INTEGER, col TEXT, page TEXT);
"""


def rs(n: int | None) -> str:
    return "nil" if not n else f"Rs. {n:,}"


def connect(path: str | Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    return db


def add_chunk(db, doc, kind, ref, heading, page, text):
    cur = db.execute("INSERT INTO chunks(doc,kind,ref,heading,page,text) VALUES(?,?,?,?,?,?)", (doc, kind, ref, heading, str(page) if page is not None else None, text))
    db.execute("INSERT INTO chunks_fts(rowid,heading,text) VALUES(?,?,?)", (cur.lastrowid, heading, text))


def add_bill(db, key: str, pdf: str, title: str, date: str, source: str, status: str) -> dict:
    """Parse and store a bill. Raises ValueError if the printed totals do not reconcile."""
    bill = bill_parse.parse_bill(pdf)
    problems = bill_parse.check(bill)
    if problems:
        raise ValueError(f"{key}: totals do not reconcile: " + "; ".join(problems))
    db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?)", (key, "bill", title, date, source, status))
    for s in bill["sections"]:
        add_chunk(db, key, "section", f"s{s['no']}", f"Section {s['no']}", s["no"], s["text"])
    by_head: dict[int, list[dict]] = {}
    for r in bill["estimate"]:
        db.execute("INSERT INTO estimate VALUES(?,?,?,?,?,?,?,?,?)", (key, r["head"], r["head_title"], r["group"], r["programme"], r["programme_name"], r["recurrent"], r["capital"], r["page"]))
        by_head.setdefault(r["head"], []).append(r)
    for head, rs_ in by_head.items():
        t = rs_[0]
        lines = [f"Head {head}: {t['head_title']}" + (f" (group: {t['group']})" if t["group"] else "") + " - First Schedule, estimate for 2027 (Rs.)."]
        for r in rs_:
            lines.append(f"Programme {r['programme']:02d} {r['programme_name']}: recurrent {rs(r['recurrent'])}, capital {rs(r['capital'])}.")
        lines.append(f"Head total: recurrent {rs(sum(r['recurrent'] or 0 for r in rs_))}, capital {rs(sum(r['capital'] or 0 for r in rs_))}.")
        add_chunk(db, key, "estimate", f"head {head}", f"Head {head} {t['head_title']}", rs_[0]["page"], "\n".join(lines))
    for r in bill["charged"]:
        db.execute("INSERT INTO charged VALUES(?,?,?,?,?,?,?,?,?)", (key, r["head"], r["title"], r["authority"], r["programme"], r["recurrent"], r["capital"], r["total"], r["page"]))
        add_chunk(db, key, "charged", f"charged {r['head']}", f"Head {r['head']} {r['title']} (charged on the Consolidated Fund)", r["page"],
                  f"Head {r['head']}: {r['title']} - Second Schedule, expenditure charged on the Consolidated Fund. Authority: {r['authority']}. {r['programme']}: recurrent {rs(r['recurrent'])}, capital {rs(r['capital'])}, total {rs(r['total'])}.")
    return bill


def add_speech(db, key: str, pdf: str, title: str, date: str, source: str) -> int:
    import pymupdf
    doc = pymupdf.open(pdf)
    texts = [p.get_text() for p in doc]
    a, b = speech_parse.english_pages(texts)
    db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?)", (key, "speech", title, date, source, "delivered"))
    n = 0
    for c in speech_parse.parse_speech(texts):
        add_chunk(db, key, "speech", c["ref"], c["heading"], c["page"], c["text"])
        n += 1
    for name, first, last in speech_parse.annex_ranges(texts, a, b):
        if name == "II":
            for r in speech_parse.parse_proposals(doc, first, last):
                db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?)", (key, r["serial"], r["sector"], r["proposal"], r["amount"], r["column"], r["page"]))
                add_chunk(db, key, "annex", f"annex II #{r['serial']}", f"Annexure II: new expenditure proposal ({r['sector']})", r["page"],
                          f"New expenditure proposal no. {r['serial']} ({r['sector']}): {r['proposal']}. Proposed annual allocation Rs. {r['amount']:,} million ({r['column']}).")
                n += 1
        else:
            body = "\n".join(re.sub(r"[ \t]+", " ", texts[i]) for i in range(first, last + 1))
            for k, part in enumerate(speech_parse.split_text(body)):
                add_chunk(db, key, "annex", f"annex {name}.{k + 1}", f"Annexure {name}", None, part)
                n += 1
    return n


def build(out: str | Path, bills: list[dict], speeches: list[dict]) -> dict:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = connect(out)
    db.executescript(SCHEMA)
    stats = {}
    for b in bills:
        add_bill(db, **b)
        stats[b["key"]] = db.execute("SELECT COUNT(*) FROM chunks WHERE doc=?", (b["key"],)).fetchone()[0]
    for s in speeches:
        stats[s["key"]] = add_speech(db, **s)
    db.commit()
    db.close()
    return stats


def search(db, query: str, k: int = 8, kinds: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", query) if len(t) > 1]
    if not terms:
        return []
    q = " OR ".join(f'"{t}"' for t in terms)
    sql = "SELECT c.*, bm25(chunks_fts, 4.0, 1.0) AS score FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
    args: list = [q]
    if kinds:
        sql += f" AND c.kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    sql += " ORDER BY score LIMIT ?"
    args.append(k)
    return db.execute(sql, args).fetchall()
