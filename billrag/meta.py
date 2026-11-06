"""Bill metadata kept OUTSIDE `chunks` (table `bill_meta`, `section_refs`): derived deterministically from `bills`
and the section text, plus optional LLM topic tags. Because `chunks` is untouched, `fingerprint()` and `vectors.npz`
stay valid and an older build of the code simply ignores the tables.

bill_meta   family      key of the earliest bill of the same title within 300 days (a bill's GS + Bill versions share it)
            stage       gazette (GS) | bill (presented to Parliament) | act/other
            bill_type   appropriation | incorporation | amendment | new | other
            amends      for amendments: the title of the Act being amended
            is_latest   1 if no later bill of the same title exists (i.e. the current instance of a recurring title)
            latest_family  family of the newest instance of this title ("Appropriation" recurs every year)
            tags        JSON list of topic tags (from `tag`, survives a rebuild)
section_refs  bill_key, section_no -> ref_no: section text that points at another section of the same bill
"""
import json
import re
from datetime import datetime

from .store import Store

FAMILY_DAYS = 300
TAGS = ["taxation", "public finance", "criminal law", "terrorism and security", "courts and procedure", "immigration",
        "land and property", "trade and commerce", "banking and finance", "companies and incorporation",
        "labour and employment", "education", "health", "environment", "agriculture and plantations",
        "local government and administration", "constitutional", "elections", "sport", "religion and culture",
        "transport", "energy and utilities", "digital and technology", "other"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS bill_meta(
  bill_key TEXT PRIMARY KEY, title_norm TEXT, family TEXT, stage TEXT, bill_type TEXT, amends TEXT,
  is_latest INT, latest_family TEXT, tags TEXT);
CREATE INDEX IF NOT EXISTS bill_meta_title ON bill_meta(title_norm, is_latest);
CREATE TABLE IF NOT EXISTS section_refs(
  bill_key TEXT NOT NULL, section_no INT NOT NULL, ref_no INT NOT NULL, PRIMARY KEY(bill_key, section_no, ref_no));
"""

STAGE = {"gs": "gazette", "bill": "bill"}
REF = re.compile(r"\bsections?\s+(\d{1,3})(?:\s*(?:,|and|or|to)\s*(\d{1,3}))*", re.I)
REF_ALL = re.compile(r"\bsections?\s+((?:\d{1,3}(?:\([^)]*\))*\s*(?:,|and|or|to|–|-)?\s*)+)", re.I)


def bill_type(title_norm: str) -> str:
    t = title_norm or ""
    if t.startswith("appropriation") or t.startswith("supplementary"):
        return "appropriation"
    if "incorporation" in t:
        return "incorporation"
    if "amendment" in t:
        return "amendment"
    return "new"


def amends(title_norm: str, kind: str):
    if kind != "amendment":
        return None
    t = re.sub(r"\s*(\(?\s*amendment\s*\)?)\s*$", "", title_norm or "").strip()
    t = re.sub(r"\s+amendment\b", "", t).strip()
    return t or None


def _d(s):
    return datetime.fromisoformat((s or "1970-01-01")[:10])


def _families(rows):
    """rows: bills sharing one title_norm, sorted by date -> {key: family key}. A gap > FAMILY_DAYS starts a new family."""
    out, fam, last = {}, None, None
    for r in rows:
        if fam is None or (_d(r["date"]) - last).days > FAMILY_DAYS:
            fam = r["key"]
        out[r["key"]] = fam
        last = _d(r["date"])
    return out


def build(store: Store) -> dict:
    """(Re)compute bill_meta and section_refs. Keeps existing topic tags."""
    db = store.db
    db.executescript(SCHEMA)
    old_tags = {r[0]: r[1] for r in db.execute("SELECT bill_key, tags FROM bill_meta WHERE tags IS NOT NULL")}
    by_title = {}
    for r in db.execute("SELECT key, title_norm, kind, date FROM bills ORDER BY date, key"):
        # all "Appropriation ..." titles are one recurring title (the yearly budget), whatever their suffix
        by_title.setdefault("appropriation" if bill_type(r["title_norm"]) == "appropriation" else r["title_norm"],
                            []).append(dict(r))
    rows = []
    for tn, bills in by_title.items():
        fam = _families(bills)
        latest_family = fam[bills[-1]["key"]]
        for b in bills:
            bt = bill_type(tn)
            rows.append((b["key"], tn, fam[b["key"]], STAGE.get((b["kind"] or "").lower(), "act"), bt,
                         amends(tn, bt), int(fam[b["key"]] == latest_family), latest_family, old_tags.get(b["key"])))
    db.execute("DELETE FROM bill_meta")
    db.executemany("INSERT INTO bill_meta VALUES(?,?,?,?,?,?,?,?,?)", rows)
    n_refs = build_refs(store)
    db.commit()
    return {"bills": len(rows), "families": len({r[2] for r in rows}), "latest": sum(r[6] for r in rows),
            "section_refs": n_refs}


def refs_in(text: str, own: int, valid: set):
    """Section numbers of the same bill that `text` refers to ("section 3", "sections 3 and 4")."""
    found = set()
    for m in REF_ALL.finditer(text or ""):
        for n in re.findall(r"(?<![\d(])(\d{1,3})(?![\d)])", re.sub(r"\([^)]*\)", " ", m.group(1))):
            if int(n) != own and int(n) in valid:
                found.add(int(n))
    return found


def build_refs(store: Store) -> int:
    db = store.db
    db.execute("DELETE FROM section_refs")
    n = 0
    for (key,) in db.execute("SELECT key FROM bills").fetchall():
        secs = store.section_map(key)
        valid = set(secs)
        rows = [(key, no, ref) for no, text in secs.items() for ref in sorted(refs_in(text, no, valid))]
        db.executemany("INSERT OR IGNORE INTO section_refs VALUES(?,?,?)", rows)
        n += len(rows)
    return n


def tag_bills(store: Store, llm, since_year=2024, workers=1, effort=None, limit=0):
    """LLM topic tags (fixed taxonomy) for bills that have none, newest first. Resumable."""
    db = store.db
    db.executescript(SCHEMA)
    todo = [dict(r) for r in db.execute(
        "SELECT b.key, b.title, b.long_title, b.summary FROM bills b JOIN bill_meta m ON m.bill_key=b.key "
        "WHERE m.tags IS NULL AND b.year>=? ORDER BY b.date DESC", (since_year,))]
    if limit:
        todo = todo[:limit]
    done = 0
    for b in todo:
        prompt = (f"Choose 1-3 topic tags for this Sri Lankan bill, ONLY from this list: {', '.join(TAGS)}.\n"
                  f"Title: {b['title']}\nLong title: {(b['long_title'] or '')[:600]}\nSummary: {(b['summary'] or '')[:500]}\n"
                  'Reply with ONLY a JSON list of strings, e.g. ["taxation"].')
        try:
            txt = llm.chat("You output only JSON.", [{"role": "user", "content": prompt}], max_tokens=200, effort=effort).text
            tags = [t for t in json.loads(re.search(r"\[.*?\]", txt, re.S).group(0)) if t in TAGS][:3]
        except Exception:
            continue
        if tags:
            db.execute("UPDATE bill_meta SET tags=? WHERE bill_key=?", (json.dumps(tags), b["key"]))
            done += 1
            if done % 25 == 0:
                db.commit()
    db.commit()
    return done


# ---- reads (used by Store-facing code; tolerate an index built before these tables existed)
def has_meta(store) -> bool:
    return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='bill_meta'").fetchone())


def keys_where(store, year=None, bill_type=None, tag=None, stage=None, latest_only=False):
    """Bill keys matching all given metadata filters; None if no filter was given."""
    if not (year or bill_type or tag or stage or latest_only):
        return None
    if not has_meta(store):
        return None
    sql, args = "SELECT m.bill_key FROM bill_meta m JOIN bills b ON b.key=m.bill_key WHERE 1=1", []
    if year:
        sql += " AND b.year=?"; args.append(int(year))
    if bill_type:
        sql += " AND m.bill_type=?"; args.append(bill_type)
    if stage:
        sql += " AND m.stage=?"; args.append(stage)
    if latest_only:
        sql += " AND m.is_latest=1"
    if tag:
        sql += " AND m.tags LIKE ?"; args.append(f'%"{tag}"%')
    return [r[0] for r in store.db.execute(sql, args)]


def latest_family_keys(store, title_norm):
    """Keys of the newest instance (GS + Bill versions) of a recurring title, or []."""
    if not has_meta(store):
        return []
    return [r[0] for r in store.db.execute(
        "SELECT bill_key FROM bill_meta WHERE title_norm=? AND is_latest=1", (title_norm,))]


def meta(store, key):
    if not has_meta(store):
        return None
    r = store.db.execute("SELECT * FROM bill_meta WHERE bill_key=?", (key,)).fetchone()
    return dict(r) if r else None


def refs_from(store, key, no):
    if not has_meta(store):
        return []
    return [r[0] for r in store.db.execute(
        "SELECT ref_no FROM section_refs WHERE bill_key=? AND section_no=? ORDER BY ref_no", (key, int(no)))]
