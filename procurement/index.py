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


# Words users type that the documents never use, any case: no passage says "IPC", they say "interim payment".
_IPC = "interim payment certificate certified certification"
QUERY_TERMS = {"ipc": _IPC, "ipcs": _IPC, "block": "withhold withholding", "blocks": "withhold withholding", "stop": "withhold withholding"}
HIERARCHICAL = {"PG2024", "PM2024", "PG2006", "PM2006"}  # documents whose refs nest by dots


def connect(path=DB):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def ctx_of(doc, c):
    expand = " ".join(f"{a} {b}" for a, b in ACRONYMS.items() if re.search(rf"\b{a}s?\b", c["text"]))
    return f"{doc['title']} | {c['ref']} {c['heading']} | {c['chapter']}\n{c['text']}\n{c.get('note') or ''}\n{expand}"


def drop_replaced(chunks):
    """FR chunks with the 1992 text that later circulars replaced cut out (sources.FR_REPLACED); what stays in force is kept."""
    out, done = [], set()
    for c in chunks:
        if c["ref"] not in sources.FR_REPLACED:
            out.append(c)
        elif c["ref"] not in done:
            done.add(c["ref"])
            parts = [p for p in chunks if p["ref"] == c["ref"]]
            text = "\n".join(p["text"] for p in parts)
            kept = re.sub(sources.FR_REPLACED[c["ref"]], "", text, count=1, flags=re.S).strip()
            marker = f"[This 1992 text was replaced by Public Finance Circulars: see FRA {c['ref']}.]"
            out.append({**c, "text": f"{marker}\n{kept}".strip(), "part": 0})
    return out


def build(path=DB, docs=None, log=print):
    db = connect(path)
    for d in sources.DOCS:
        if docs and d["id"] not in docs:
            continue
        if not Path(d["file"]).exists():
            log(f"{d['id']}: missing {d['file']} - skipped"); continue
        overrides, pages = tables.load(d["id"]), []
        if d.get("parser") == "curated":
            chunks, stats = parse.parse_curated(Path(d["file"]).read_text(encoding="utf-8"), d["id"])
        elif d.get("parser") == "act":
            chunks, stats = parse.parse_act(d["file"], d["id"])
        else:
            pages = parse.pdf_pages(d["file"], overrides)
            parser = parse.parse_numbered if d.get("parser") == "numbered" else parse.PARSERS[d["id"]]
            chunks, stats = parser(pages, doc=d["id"])
        if d["id"] == "FR":
            chunks = drop_replaced(chunks)
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
            if d["id"] == "FR":
                c["note"] = sources.FR_NOTES.get(c["ref"], sources.FR_GENERAL_NOTE)
            notes = [d["note"]] if d.get("note") else []  # e.g. "unofficial translation" on every chunk of the document
            notes += [n for doc, ref, h, n in sources.SUPPLEMENT_NOTES
                      if doc == d["id"] and ref == c["ref"] and (h is None or h in (c["heading"] or ""))]
            if notes:
                c["note"] = " ".join(filter(None, [c.get("note")] + notes))
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
        def expand(w):
            bare = w.strip("?.,;:!()")
            return f"{w} {ACRONYMS.get(bare, '')} {QUERY_TERMS.get(bare.lower(), '')}"
        q = fts_query(" ".join(expand(w) for w in query.split()))
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

    def chain(self, doc, ref):
        """Headings of the clauses above `ref` in the same document, outermost first: [("2", "Governance"), ("2.3", "...")].
        Refs are dotted numbers (2.3.1 sits under 2.3 under 2), so the parents come from the ref itself, no re-ingest needed.
        A manual section whose own heading did not extract borrows the guideline clause's title (PM2024 -> PG2024)."""
        if not ref or doc not in HIERARCHICAL or not re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){1,3}", ref):
            return []
        parts = ref.split(".")
        twin = "PG" + doc[2:] if doc.startswith("PM") else doc
        out = []
        for n in range(1, len(parts)):
            up = ".".join(parts[:n])
            row = self.db.execute("SELECT heading FROM chunks WHERE doc IN (?,?) AND ref=? AND heading!='' "
                                  "ORDER BY doc=? DESC, ord LIMIT 1", (doc, twin, up, doc)).fetchone()
            if row:
                out.append((up, row["heading"]))
        return out

    def supplements_for(self, doc, ref):
        """Supplement sections that change `ref` of `doc` (or one of its sub-clauses). PG2024 and PM2024 share numbering, so a
        Guidelines hit also brings the Manual supplements for that clause."""
        target = "PM2024" if doc in ("PG2024", "PM2024") else doc
        amenders = [d["id"] for d in sources.DOCS if d.get("amends") == target]
        if not amenders or not ref:
            return []
        q = ",".join("?" * len(amenders))
        return [dict(r) for r in self.db.execute(
            f"SELECT * FROM chunks WHERE doc IN ({q}) AND (ref=? OR ref LIKE ?) ORDER BY doc, ord",
            (*amenders, ref, f"{ref}.%"))]

    def manual_for(self, ref, doc="PM2024"):
        """Manual sections that explain a guideline clause (exact ref, or listed in xref)."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM chunks WHERE doc=? AND (ref=? OR ','||xref||',' LIKE ?) ORDER BY ord",
            (doc, ref, f"%,{ref},%"))]
