"""Clause index for the procurement documents: SQLite + FTS5 at data/procurement/index.sqlite (git-ignored)."""
import re
import sqlite3
from pathlib import Path

from billrag.store import fts_query
from . import parse, sources, tables

DB = Path("data/procurement/index.sqlite")
SCHEMA = """
CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, kind TEXT, status TEXT, issued TEXT, effective TEXT, title TEXT,
  file TEXT, n_pages INT, n_chunks INT);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, doc TEXT NOT NULL, ref TEXT, heading TEXT, chapter TEXT,
  kind TEXT, page INT, ord INT, part INT, xref TEXT, note TEXT, text TEXT, ctx TEXT);
CREATE INDEX IF NOT EXISTS chunks_ref ON chunks(doc, ref, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(ctx, tokenize='porter unicode61');
"""
# Acronyms the documents use everywhere; the FTS text gets the long form too so "Ministry Procurement Committee" finds "MPC".
ACRONYMS = {"PC": "Procurement Committee", "BEC": "Bid Evaluation Committee", "AC": "Acceptance Committee",
            "HLPC": "High Level Procurement Committee", "SHLPC": "Standing High Level Procurement Committee",
            "MPC": "Ministry Procurement Committee", "DPC": "Department Procurement Committee",
            "PPC": "Project Procurement Committee", "RPC": "Regional Procurement Committee",
            "TCE": "Total Cost Estimate", "PE": "Procuring Entity", "CAO": "Chief Accounting Officer",
            "AO": "Accounting Officer", "NPC": "National Procurement Commission", "NCB": "National Competitive Bidding",
            "ICB": "International Competitive Bidding", "VRC": "Variation Review Committee", "SPD": "Standard Procurement Document",
            "PTS": "Procurement Time Schedule", "TEC": "Technical Evaluation Committee", "PAB": "Procurement Appeal Board",
            "PAC": "Procurement Appeal Committee", "HD": "Head of Department", "PD": "Project Director",
            "SBD": "Standard Bidding Document", "CAPC": "Cabinet Appointed Procurement Committee"}


def connect(path=DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def ctx_of(doc, c):
    expand = " ".join(f"{a} {b}" for a, b in ACRONYMS.items() if re.search(rf"\b{a}s?\b", c["text"]))
    return f"{doc['title']} | {c['ref']} {c['heading']} | {c['chapter']}\n{c['text']}\n{c.get('note') or ''}\n{expand}"


def build(path=DB, docs=None, log=print):
    db = connect(path)
    for d in sources.DOCS:
        if docs and d["id"] not in docs:
            continue
        if not Path(d["file"]).exists():
            log(f"{d['id']}: missing {d['file']} - skipped"); continue
        overrides = tables.load(d["id"])
        pages = parse.pdf_pages(d["file"], overrides)
        chunks, stats = parse.PARSERS[d["id"]](pages, doc=d["id"])
        ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE doc=?", (d["id"],))]
        db.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
        db.execute("DELETE FROM chunks WHERE doc=?", (d["id"],))
        titles = {r["ref"]: r["heading"] for r in db.execute("SELECT ref, heading FROM chunks WHERE doc=?",
                                                             ("PG" + d["id"][2:],))}
        for ordn, c in enumerate(chunks):
            if not c["heading"]:  # a manual section whose heading did not extract: use the guideline clause title
                c["heading"] = titles.get(c["ref"], "")
            if d["id"] == "PG2024" and c["ref"] in sources.AMENDMENTS:
                c["note"] = f"Amended by {sources.AMENDMENT}: {sources.AMENDMENTS[c['ref']]}"
            ctx = ctx_of(d, c)
            cur = db.execute("INSERT INTO chunks(doc,ref,heading,chapter,kind,page,ord,part,xref,note,text,ctx) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (d["id"], c["ref"], c["heading"], c["chapter"], c["kind"], c["page"], ordn, c["part"],
                              c.get("xref"), c.get("note"), c["text"], ctx))
            db.execute("INSERT INTO chunks_fts(rowid, ctx) VALUES(?,?)", (cur.lastrowid, ctx))
        db.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?,?,?,?)",
                   (d["id"], d["kind"], d["status"], d["issued"], d["effective"], d["title"], str(d["file"]),
                    len(pages), len(chunks)))
        log(f"{d['id']}: {len(pages)} pages -> {len(chunks)} chunks  {stats}"
            + (f"  ({len(overrides)} table pages from transcriptions)" if overrides else ""))
    db.commit()
    return db


class Index:
    def __init__(self, path=DB, reranker=None):
        if not Path(path).exists():
            raise FileNotFoundError(f"{path} not found: run `python -m procurement build`")
        self.db, self.reranker = connect(path), reranker
        self.docs = {r["id"]: dict(r) for r in self.db.execute("SELECT * FROM docs")}

    def bm25(self, query, limit=40, docs=None):
        q = fts_query(" ".join(f"{w} {ACRONYMS.get(w.strip('?.,;:!()'), '')}" for w in query.split()))
        if not q:
            return []
        sql = ("SELECT c.id, bm25(chunks_fts) AS s FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
               "WHERE chunks_fts MATCH ?")
        args = [q]
        if docs:
            sql += f" AND c.doc IN ({','.join('?' * len(docs))})"; args += list(docs)
        return [r["id"] for r in self.db.execute(sql + " ORDER BY s LIMIT ?", args + [limit])]

    def chunks(self, ids):
        if not ids:
            return []
        rows = {r["id"]: dict(r) for r in self.db.execute(
            f"SELECT * FROM chunks WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
        return [rows[i] for i in ids if i in rows]

    def search(self, query, k=8, docs=None, pool=30, keep_bm25=2):
        """BM25 -> optional cross-encoder rerank (Workers AI, best-effort) -> top k. BM25's top `keep_bm25` always stay in
        the result: the reranker pushed Constitution Art. 156B (BM25 rank 1 for "who appoints the NPC members") out of
        the top 8 in favour of committee-appointment clauses."""
        ids = self.bm25(query, pool, docs)
        if self.reranker and len(ids) > 1:
            try:
                rows = self.chunks(ids)
                sc = self.reranker.scores(query, [r["ctx"] for r in rows])
                ranked = [r["id"] for _, r in sorted(zip(sc, rows), key=lambda x: -x[0])]
                top = ranked[:k]
                for i in reversed(ids[:keep_bm25]):
                    if i not in top[:k]:  # second place: the answer step fills its slots in hit order
                        top = [t for t in top if t != i]
                        top.insert(1, i)
                ids = top + [i for i in ranked if i not in top]
            except Exception:
                pass  # keep BM25 order
        return self.chunks(ids[:k])

    def clause(self, doc, ref, heading=None):
        """All parts of one clause (or one manual section: same ref and heading) in reading order."""
        sql, args = "SELECT * FROM chunks WHERE doc=? AND ref=?", [doc, ref]
        if heading is not None:
            sql += " AND heading=?"; args.append(heading)
        rows = [dict(r) for r in self.db.execute(sql + " ORDER BY ord", args)]
        return rows

    def manual_for(self, ref, doc="PM2024"):
        """Manual sections that explain a guideline clause (exact ref, or listed in xref)."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM chunks WHERE doc=? AND (ref=? OR ','||xref||',' LIKE ?) ORDER BY ord",
            (doc, ref, f"%,{ref},%"))]
