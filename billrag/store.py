"""SQLite store: bill metadata, clause chunks, and an FTS5 (BM25) index."""
import re
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills(
  key TEXT PRIMARY KEY, bill_no INT, year INT, date TEXT, title TEXT, title_norm TEXT,
  kind TEXT, long_title TEXT, presented_by TEXT, presented_on TEXT, gazette_date TEXT,
  printed_no TEXT, n_pages INT, n_sections INT, pdf_path TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, bill_key TEXT NOT NULL, kind TEXT, part TEXT, section_no INT,
  heading TEXT, page INT, ord INT, text TEXT, ctx TEXT);
CREATE INDEX IF NOT EXISTS chunks_bill ON chunks(bill_key, section_no, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(ctx, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS local_sections(
  bill_key TEXT NOT NULL, lang TEXT NOT NULL, section_no INT NOT NULL, heading TEXT, page INT, text TEXT,
  PRIMARY KEY(bill_key, lang, section_no));
CREATE TABLE IF NOT EXISTS local_bills(
  bill_key TEXT NOT NULL, lang TEXT NOT NULL, status TEXT, n_sections INT, n_kept INT, pdf TEXT,
  PRIMARY KEY(bill_key, lang));
"""
# local_sections: the official Sinhala (later Tamil) text of a section, linked to the English index by (bill_key,
# section_no). Kept OUT of `chunks` on purpose: retrieval stays English, and fingerprint()/vectors.npz are unaffected.

STOP = set("a an the of to in on for and or is are was were be been by with as at from that this which "
           "what who whom how does do did it its any all under shall may can about into than then there "
           "their they them such not no per also i me my we our you your tell say says said".split())


def fts_query(q: str) -> str:
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", q) if t.lower() not in STOP and len(t) > 1]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


def bill_key(bill_no: int, year: int) -> str:
    return f"{int(bill_no):03d}/{int(year)}"


def normalize_ref(ref: str):
    """'77/2026', '077 of 2026', 'bill 77-2026' -> '077/2026'; None if not a number reference."""
    m = re.search(r"(\d{1,4})\s*(?:/|-|of|,)\s*(\d{4})", ref)
    return bill_key(m.group(1), m.group(2)) if m else None


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)  # sqlite is serialized; evals read from threads
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- writes
    def replace_bill(self, bill: dict, chunks: list):
        key = bill["key"]
        self.delete_bill(key)
        cols = list(bill)
        self.db.execute(f"INSERT INTO bills({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                        [bill[c] for c in cols])
        for ordn, c in enumerate(chunks):
            cur = self.db.execute(
                "INSERT INTO chunks(bill_key,kind,part,section_no,heading,page,ord,text,ctx) VALUES(?,?,?,?,?,?,?,?,?)",
                (key, c["kind"], c.get("part"), c.get("section_no"), c.get("heading"), c.get("page"),
                 ordn, c["text"], c["ctx"]))
            self.db.execute("INSERT INTO chunks_fts(rowid, ctx) VALUES(?,?)", (cur.lastrowid, c["ctx"]))

    def delete_bill(self, key):
        ids = [r[0] for r in self.db.execute("SELECT id FROM chunks WHERE bill_key=?", (key,))]
        self.db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        self.db.execute("DELETE FROM chunks WHERE bill_key=?", (key,))
        self.db.execute("DELETE FROM bills WHERE key=?", (key,))

    def set_summary(self, key, summary):
        self.db.execute("UPDATE bills SET summary=? WHERE key=?", (summary, key))

    def commit(self):
        self.db.commit()

    # ---- reads
    def bill(self, key):
        r = self.db.execute("SELECT * FROM bills WHERE key=?", (key,)).fetchone()
        return dict(r) if r else None

    def find_bills(self, year=None, title_contains=None, kind=None, limit=30):
        sql, args = "SELECT * FROM bills WHERE 1=1", []
        if year:
            sql += " AND year=?"; args.append(int(year))
        if title_contains:
            sql += " AND title_norm LIKE ?"; args.append(f"%{re.sub(r'[^a-z0-9]+', ' ', title_contains.lower()).strip()}%")
        if kind:
            sql += " AND lower(kind)=lower(?)"; args.append(kind)
        sql += " ORDER BY date DESC LIMIT ?"; args.append(int(limit))
        return [dict(r) for r in self.db.execute(sql, args)]

    def versions(self, title_norm, near_date=None, days=300):
        """Bills sharing a normalized title. Titles like "Finance (Amendment)" recur every year, so pass `near_date`
        to keep only versions of the *same* bill (its GS and Bill forms are weeks or months apart, not years)."""
        rows = [dict(r) for r in self.db.execute(
            "SELECT key,kind,date FROM bills WHERE title_norm=? ORDER BY date", (title_norm,))]
        if near_date:
            from datetime import datetime
            d0 = datetime.fromisoformat(near_date[:10])
            rows = [r for r in rows if abs((datetime.fromisoformat(r["date"][:10]) - d0).days) <= days]
        return rows

    def chunks_by_ids(self, ids):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {r["id"]: dict(r) for r in self.db.execute(
            f"SELECT c.*, b.title, b.kind AS bill_kind, b.date FROM chunks c JOIN bills b ON b.key=c.bill_key "
            f"WHERE c.id IN ({q})", list(ids))}

    def section_text(self, key, no):
        rows = self.db.execute("SELECT text, part, page FROM chunks WHERE bill_key=? AND section_no=? ORDER BY ord",
                               (key, int(no))).fetchall()
        if not rows:
            return None
        return {"text": "\n".join(r["text"] for r in rows), "part": rows[0]["part"], "page": rows[0]["page"]}

    def outline(self, key):
        return [dict(r) for r in self.db.execute(
            "SELECT section_no, kind, MAX(heading) AS heading, MIN(substr(text,1,90)) AS head, MIN(page) AS page "
            "FROM chunks WHERE bill_key=? AND kind IN ('section','schedule') "
            "GROUP BY COALESCE(section_no, -ord) ORDER BY MIN(ord)", (key,))]

    def section_map(self, key):
        out = {}
        for r in self.db.execute("SELECT section_no, text FROM chunks WHERE bill_key=? AND kind='section' "
                                 "ORDER BY ord", (key,)):
            if r["section_no"] is not None:
                out[r["section_no"]] = out.get(r["section_no"], "") + ("\n" if r["section_no"] in out else "") + r["text"]
        return out

    def bm25(self, query, limit=50, year=None, keys=None, kinds=None):
        fq = fts_query(query)
        if not fq:
            return []
        sql = ("SELECT c.id AS id, bm25(chunks_fts) AS score FROM chunks_fts "
               "JOIN chunks c ON c.id = chunks_fts.rowid JOIN bills b ON b.key = c.bill_key "
               "WHERE chunks_fts MATCH ?")
        args = [fq]
        if year:
            sql += " AND b.year=?"; args.append(int(year))
        if keys:
            sql += f" AND c.bill_key IN ({','.join('?' * len(keys))})"; args += list(keys)
        if kinds:
            sql += f" AND c.kind IN ({','.join('?' * len(kinds))})"; args += list(kinds)
        sql += " ORDER BY score LIMIT ?"; args.append(limit)
        return [r["id"] for r in self.db.execute(sql, args)]

    def fingerprint(self):
        """Hash of every chunk id + indexed text. Vectors are only valid for the exact index they were made from."""
        import hashlib
        h = hashlib.sha256()
        for r in self.db.execute("SELECT id, ctx FROM chunks ORDER BY id"):
            h.update(f"{r['id']}\x00{r['ctx']}\x00".encode())
        return h.hexdigest()

    def all_chunks(self):
        return [dict(r) for r in self.db.execute("SELECT id, ctx FROM chunks ORDER BY id")]

    def stats(self):
        q = lambda s: self.db.execute(s).fetchone()[0]
        return {"bills": q("SELECT COUNT(*) FROM bills"), "chunks": q("SELECT COUNT(*) FROM chunks"),
                "sections": q("SELECT COUNT(*) FROM chunks WHERE kind='section'"),
                "summaries": q("SELECT COUNT(*) FROM bills WHERE summary IS NOT NULL")}

    def replace_local(self, key, lang, status, sections, pdf=None, n_sections=None):
        """sections: [(no, heading, page, text)] for one bill; status is 'ok' or why the bill is not linked."""
        self.db.execute("DELETE FROM local_sections WHERE bill_key=? AND lang=?", (key, lang))
        self.db.executemany("INSERT INTO local_sections VALUES(?,?,?,?,?,?)",
                            [(key, lang, no, heading, page, text) for no, heading, page, text in sections])
        self.db.execute("INSERT OR REPLACE INTO local_bills VALUES(?,?,?,?,?,?)",
                        (key, lang, status, n_sections, len(sections), pdf))

    def local_section(self, key, no, lang="si"):
        r = self.db.execute("SELECT * FROM local_sections WHERE bill_key=? AND section_no=? AND lang=?",
                            (key, int(no), lang)).fetchone()
        return dict(r) if r else None

    def local_stats(self, lang="si"):
        rows = self.db.execute("SELECT status, COUNT(*) AS n, SUM(n_kept) AS kept FROM local_bills WHERE lang=? "
                               "GROUP BY status ORDER BY n DESC", (lang,)).fetchall()
        return [dict(r) for r in rows]

    def update_overview(self, key, text):
        r = self.db.execute("SELECT id FROM chunks WHERE bill_key=? AND kind='overview'", (key,)).fetchone()
        if r:
            self.db.execute("UPDATE chunks SET text=?, ctx=? WHERE id=?", (text, text, r["id"]))
            self.db.execute("DELETE FROM chunks_fts WHERE rowid=?", (r["id"],))
            self.db.execute("INSERT INTO chunks_fts(rowid, ctx) VALUES(?,?)", (r["id"], text))
