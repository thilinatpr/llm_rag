"""Bill metadata kept OUTSIDE `chunks` (table `bill_meta`, `section_refs`): derived deterministically from `bills`
and the section text, plus optional LLM topic tags. Because `chunks` is untouched, `fingerprint()` and `vectors.npz`
stay valid and an older build of the code simply ignores the tables.

bill_meta   family      key of the earliest bill of the same title within 300 days (a bill's GS + Bill versions share it)
            stage       gazette (GS) | bill (presented to Parliament) | act (certified; billrag/acts.py) | other
            bill_type   appropriation | incorporation | amendment | new | other
            amends      for amendments: the title of the Act being amended
            is_latest   1 if no later bill of the same title exists (i.e. the current instance of a recurring title)
            latest_family  family of the newest instance of this title ("Appropriation" recurs every year)
            tags        JSON list of topic tags (from `tag`, survives a rebuild)
section_refs  bill_key, section_no -> ref_no: section text that points at another section of the same bill
amended_sections  bill_key, section_no -> action (amended | repealed | replaced | inserted), target ("section 8", "section 52A",
            "CHAPTER XVIIA", "First Schedule"), act (None = the principal enactment): what each clause of an amending bill or
            Act changes, read from the clause's opening words. Clauses that change nothing (short title, commencement,
            transitional provisions, "Sinhala text to prevail") get no row, so "every clause" = rows + the rest, never guessed.
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
CREATE TABLE IF NOT EXISTS amended_sections(
  bill_key TEXT NOT NULL, section_no INT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, act TEXT,
  PRIMARY KEY(bill_key, section_no));
"""

STAGE = {"gs": "gazette", "bill": "bill", "act": "act"}
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
            rows.append((b["key"], tn, fam[b["key"]], STAGE.get((b["kind"] or "").lower(), "other"), bt,
                         amends(tn, bt), int(fam[b["key"]] == latest_family), latest_family, old_tags.get(b["key"])))
    db.execute("DELETE FROM bill_meta")
    db.executemany("INSERT INTO bill_meta VALUES(?,?,?,?,?,?,?,?,?)", rows)
    n_refs = build_refs(store)
    n_amend = build_amended(store, {r[0] for r in rows})  # all: some amending instruments have no "(Amendment)" in the title
    db.commit()
    return {"bills": len(rows), "families": len({r[2] for r in rows}), "latest": sum(r[6] for r in rows),
            "section_refs": n_refs, "amended_sections": n_amend}


# ---- what each clause of an amending instrument changes. Only the clause's OPENING words are read: a clause like "the
# Commissioner-General shall write off interest under section 157 of the principal enactment" mentions a section without
# amending it, and must not be listed.
# 8, 52A, 155FF, "16 A", "97a", "6 b" (a letter after a space only when "of"/"to"/a list follows, so the "o" of "8 of" is not one)
_NUM = r"\d{1,3}(?:[A-Z]{1,3}\b|[a-z]{1,2}\b|\s[A-Za-z]{1,2}(?=\s+(?:of|to|and|or)\b|\s*,))?"
_NUMS = rf"{_NUM}(?:\s*(?:,|and|to|-|–)\s*{_NUM})*"
_SEC = r"(?:[Ss]e\s?c\s?t\s?i\s?o\s?ns?|Articles?)"  # "Se ction", "Secti on": a word split by the PDF; Articles of the Constitution
_UNIT = (rf"(?:{_SEC}\s+{_NUMS}|(?:the\s+)?(?:[A-Z][a-z]+\s+)?Schedules?(?:\s+[IVX]+\b)?|(?:the\s+)?(?:Part|PART|Chapter|CHAPTER)\s+[IVXLC]+[A-Z]?|"
         r"(?:the\s+)?(?:Preamble|long\s+title))"
         r"(?:\s*\([^)]{0,80}\))?")  # "Chapter LXVI (sections 801 to 833 B both inclusive)"
_OF = r"\s+(?:to|of)\s+(?:the\s+)?(?P<act>principal\s+enactment|.{3,250}?)\s+(?:is|are)\s+(?:hereby\s+)?(?:further\s+)?"
AMEND_OPEN = re.compile(rf"^(?:The\s+)?(?P<unit>{_UNIT}){_OF}(?P<verb>amended|repealed)", re.S)
INSERT_OPEN = re.compile(r"^The\s+following\s+(?:new\s+)?(?P<what>sections?|Articles?|Chapters?|CHAPTERS?|Parts?|PARTS?|Schedules?|"
                         r"headings?|divisions?|sub-?sections?|paragraphs?|definitions?)(?:\s*\([^)]{0,80}\))?\s+"
                         r"(?:is|are)\s+(?:hereby\s+)?inserted\b(?P<rest>.{0,400})", re.S)
# "The principal enactment is hereby amended by the substitution for the words ...", "In the principal enactment ... there shall
# be substituted", "The sections of the principal enactment specified in Column I of the Schedule hereto are hereby amended"
WHOLE_OPEN = re.compile(r"^(?:The\s+(?P<a>principal\s+enactment|[^.]{3,120}?(?:Act|Ordinance|Code|Law),?\s+No\.\s*\d+\s+of\s+\d{4}"
                        r"(?:\s*\([^)]{0,120}\))?)\s+is\s+(?:hereby\s+)?amended|"
                        r"(?:\(1\)\s*)?In\s+the\s+(?P<b>principal\s+enactment)\b.{0,200}?\bshall\s+be\s+substituted)", re.S)
LISTED_OPEN = re.compile(r"^The\s+sections\s+of\s+the\s+principal\s+enactment\s+specified\s+in\s+.{0,80}?Schedule.{0,40}?"
                         r"(?:is|are)\s+(?:hereby\s+)?amended", re.S)
EFFECT_AS = re.compile(rf"have\s+effect\s+as\s+(?P<t>(?:{_SEC}\s+{_NUMS}|(?:CHAPTER|Chapter|PART|Part)\s+[IVXLC]+[A-Z]?|"
                       rf"(?:the\s+)?[A-Z][a-z]+\s+Schedule))(?:\s+(?:of|to)\s+(?:that|the)\s+(?P<act>[^.;:]{{0,200}}?))?"
                       r"(?:[:;.\-–—]|\s+of\s+that|\s*$)", re.S)
INSERT_AFTER = re.compile(rf"(?:after|before|above|below)\s+(?P<t>{_SEC}\s+{_NUM}|(?:CHAPTER|Chapter|PART|Part)\s+[IVXLC]+[A-Z]?)\s+"
                          r"(?:of|to)\s+(?:the\s+)?(?P<act>principal\s+enactment|[^,]{3,200}?)\s*(?:,|and\b)")
_LEAD = re.compile(r"^\s*(?:\d{1,3}\s*\.\s*)?")


def _act_name(raw):
    """'principal enactment' -> None (the Act the instrument amends); else the named Act, without the 'hereinafter' aside."""
    if not raw:
        return None
    a = re.sub(r"\s+", " ", raw).strip()
    if re.search(r"(?:hereinafter|in\s+this\s+Act)\s+referred\s+to\s+as\s+(?:the\s+)?[\"“]?(?:the\s+)?principal\s+enactment", a, re.I):
        return None  # the clause that names the principal enactment
    if re.fullmatch(r"(?:that\s+|the\s+)?(?:principal\s+)?enactment|that\s+Act", a, re.I):
        return None
    a = re.split(r"\s*\(\s*hereinafter", a, maxsplit=1)[0].strip(" ,")
    return None if re.fullmatch(r"principal\s+enactment", a, re.I) else a[:160]


def _tidy(t):
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^the\s+", "", t)
    t = re.sub(r"^Se\s?ction", "Section", t)
    t = re.sub(r"(\d)\s?(?!(?:and|to|of|or)\b)([A-Za-z]{1,3})\b", lambda m: m.group(1) + m.group(2).upper(), t)  # "16 A", "97a" -> 16A, 97A
    return t[0].lower() + t[1:] if t.lower().startswith("section") else t


def amendment_of(text):
    """(action, target, act) for one clause of an amending instrument, or None if its opening words change nothing."""
    t = _LEAD.sub("", re.sub(r"\s+", " ", text or "")).strip()[:700]
    m = AMEND_OPEN.match(t)
    if m:
        verb = m.group("verb")
        after = t[m.end():m.end() + 300]
        action = "replaced" if verb == "repealed" and re.search(r"substitut", after) else verb
        return action, _tidy(m.group("unit")), _act_name(m.group("act"))
    m = INSERT_OPEN.match(t)
    if m:
        rest = m.group("rest")
        e = EFFECT_AS.search(rest)
        if e:
            act = _act_name(e.group("act")) if e.group("act") else None
            if act is None and (a := INSERT_AFTER.search(rest)):
                act = _act_name(a.group("act"))
            return "inserted", _tidy(e.group("t")), act
        a = INSERT_AFTER.search(rest)
        if a:
            where = re.search(r"\b(after|before|above|below)\b", rest).group(1)
            return "inserted", f"new {m.group('what').lower()} {where} {_tidy(a.group('t'))}", _act_name(a.group("act"))
    if WHOLE_OPEN.match(t):
        return "amended", "words throughout the Act", None
    if LISTED_OPEN.match(t):
        return "amended", "the sections listed in its Schedule", None
    return None


def build_amended(store: Store, keys) -> int:
    """Fill amended_sections for the given (amending) bills and Acts."""
    db = store.db
    db.execute("DELETE FROM amended_sections")
    n = 0
    for key in sorted(keys):
        rows = []
        for no, text in store.section_map(key).items():
            a = amendment_of(text)
            if a:
                rows.append((key, no, *a))
        db.executemany("INSERT OR REPLACE INTO amended_sections VALUES(?,?,?,?,?)", rows)
        n += len(rows)
    return n


# a title = capitalised words (and of/and/the/for between them) ending in "No. N of YYYY" or "(Chapter N)", right before the aside
_TW = r"[A-Z][\w'’&-]*"
PRINCIPAL = re.compile(rf"(?P<a>{_TW}(?:\s+(?:{_TW}|of|and|the|for|on|in|\([A-Z][\w ]*\)))*(?:,?\s+No\.\s*\d+\s+of\s+\d{{4}}|\s*\(Chapter\s+\d+\)))"
                       r"\s*\(\s*(?:hereinafter|in\s+this\s+Act)\s+referred\s+to\s+as\s+(?:the\s+)?[\"“]?(?:the\s+)?principal\s+enactment")


def principal_of(store, key):
    """'Inland Revenue Act, No. 24 of 2017' when a clause of the instrument names its principal enactment; else None."""
    for text in store.section_map(key).values():
        m = PRINCIPAL.search(re.sub(r"\s+", " ", text[:1500]))
        if m:
            return re.sub(r"^the\s+", "", m.group("a"), flags=re.I).strip(" ,")
    return None


def amended(store, key):
    """[{section_no, action, target, act}] in clause order; [] if the table is missing or the instrument amends nothing."""
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE name='amended_sections'").fetchone():
        return []
    return [dict(r) for r in store.db.execute(
        "SELECT section_no, action, target, act FROM amended_sections WHERE bill_key=? ORDER BY section_no", (key,))]


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


def refs_to(store, key, no):
    """Sections of the same bill that refer TO section `no` (e.g. the penalty section that points at the offence section)."""
    if not has_meta(store):
        return []
    return [r[0] for r in store.db.execute(
        "SELECT section_no FROM section_refs WHERE bill_key=? AND ref_no=? ORDER BY section_no", (key, int(no)))]
