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
CREATE TABLE IF NOT EXISTS groups(doc TEXT, name TEXT, recurrent INTEGER, capital INTEGER, heads TEXT, page INTEGER);
CREATE TABLE IF NOT EXISTS charged(doc TEXT, head INTEGER, title TEXT, authority TEXT, programme TEXT, recurrent INTEGER, capital INTEGER, total INTEGER, page INTEGER);
CREATE TABLE IF NOT EXISTS proposals(doc TEXT, serial INTEGER, sector TEXT, proposal TEXT, amount_million INTEGER, col TEXT, page TEXT);
"""


def rs(n: int | None) -> str:
    return "nil" if not n else f"Rs. {n:,}"


def connect(path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    """readonly: the server opens the baked index immutable (no journal files, safe for one connection per thread)."""
    db = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True, check_same_thread=False) if readonly else sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    return db


def add_chunk(db, doc, kind, ref, heading, page, text):
    cur = db.execute("INSERT INTO chunks(doc,kind,ref,heading,page,text) VALUES(?,?,?,?,?,?)", (doc, kind, ref, heading, str(page) if page is not None else None, text))
    db.execute("INSERT INTO chunks_fts(rowid,heading,text) VALUES(?,?,?)", (cur.lastrowid, heading, text))


def split_section(text: str, limit: int = 1100) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for piece in re.split(r"(?<=[.;:]) (?=\(\d+\)|\([a-z]\) |Provided)", text):
        if cur and len(cur) + len(piece) > limit:
            parts.append(cur.strip())
            cur = ""
        cur += piece + " "
    if cur.strip():
        parts.append(cur.strip())
    return parts


def borrowing_limit(sections: list[dict]) -> int | None:
    for s in sections:
        m = re.search(r"shall not exceed rupees ([a-z\- ]+?) and the details", s["text"])
        if m:
            return bill_parse.words_to_int(m.group(1))
    return None


def add_bill(db, key: str, pdf: str, title: str, date: str, source: str, status: str) -> dict:
    """Parse and store a bill. Raises ValueError if the printed totals do not reconcile."""
    bill = bill_parse.parse_bill(pdf)
    problems = bill_parse.check(bill)
    if problems:
        raise ValueError(f"{key}: totals do not reconcile: " + "; ".join(problems))
    db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?)", (key, "bill", title, date, source, status))
    for s in bill["sections"]:
        # long sections (s2 holds the total, the borrowing limit and the transfer rules) are split at sub-section marks so that
        # BM25 length normalisation does not bury them; every part keeps the same label [Bill sN]
        for part in split_section(s["text"]):
            add_chunk(db, key, "section", f"s{s['no']}", f"Section {s['no']}", s["no"], part)
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
    # ministry / group totals printed in the bill (they reconcile with the heads: see parse.check + tests)
    for g in bill["totals"]["groups"]:
        name = re.sub(r"^Head [\d\s\-–and]+\s", "", g["group"]).replace("Provicial", "Provincial")
        name = re.sub(r"\s+", " ", name).strip()
        heads = sorted({r["head"] for r in bill["estimate"] if r["group"] == g["group"]})
        titles = {r["head"]: r["head_title"] for r in bill["estimate"]}
        db.execute("INSERT INTO groups VALUES(?,?,?,?,?,?)", (key, name, g["recurrent"], g["capital"], ",".join(map(str, heads)), g["page"]))
        add_chunk(db, key, "group", f"group {name}", f"{name} (ministry total)", g["page"],
                  f"{name}: ministry total in the First Schedule (estimate for 2027): recurrent {rs(g['recurrent'])}, capital {rs(g['capital'])}, "
                  f"recurrent and capital together {rs((g['recurrent'] or 0) + (g['capital'] or 0))}. Heads: "
                  + "; ".join(f"{h} {titles[h]}" for h in heads) + ".")
    t = bill["totals"]
    voted = t["first_recurrent"] + t["first_capital"]
    limit = borrowing_limit(bill["sections"])
    limit_txt = (f" Borrowing limit (section 2(1)(b)): the balance outstanding of borrowing during the financial year 2027 shall not exceed {rs(limit)}." if limit else "")
    add_chunk(db, key, "facts", "totals", "Key figures of the Bill: total expenditure, total budget, Appropriation, borrowing", None,
              f"Key figures, {title}: expenditure voted by this Bill (First Schedule, section 2(1)): recurrent {rs(t['first_recurrent'])}, capital {rs(t['first_capital'])}, "
              f"together {rs(voted)}.{limit_txt} Expenditure charged on the Consolidated Fund by the Constitution and other laws (Second Schedule): recurrent {rs(t['second_recurrent'])}, "
              f"capital {rs(t['second_capital'])}, together {rs(t['second_total'])}. Voted and charged together (sum of the two printed totals): {rs(voted + t['second_total'])}. "
              "")
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
    cover = " ".join(texts[a].split())
    m = re.search(r"(\d+(?:st|nd|rd|th)) Budget Speech.*?(\d{1,2})(?:st|nd|rd|th)?\s+(\w+ \d{4})", cover)
    if m:
        add_chunk(db, key, "facts", "cover", "Budget Speech 2027: cover, date of delivery", None,
                  f"The Budget Speech 2027 is the {m.group(1)} Budget Speech, delivered in the Parliament of Sri Lanka on {m.group(2)} {m.group(3)} "
                  "by Anura Kumara Dissanayake, Minister of Finance, Planning and Economic Development.")
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


STOP = set("""a an the of to for in on at by is are was were be been it its this that these those and or but not what which who whom how much many
does do did can could would should will shall has have had there their they them from with as about into per any all give tell me us you your please
allocated allocation allocate allocations provided provide proposed budget bill speech""".split())


def search(db, query: str, k: int = 8, kinds: tuple[str, ...] | None = None, doc: str | None = None) -> list[sqlite3.Row]:
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", query) if len(t) > 1 and t.lower() not in STOP]
    if not terms:
        return []
    q = " OR ".join(f'"{t}"' for t in terms)
    sql = "SELECT c.*, bm25(chunks_fts, 4.0, 1.0) AS score FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
    args: list = [q]
    if doc:
        sql += " AND c.doc = ?"
        args.append(doc)
    if kinds:
        sql += f" AND c.kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    sql += " ORDER BY score LIMIT ?"
    args.append(k)
    return db.execute(sql, args).fetchall()
