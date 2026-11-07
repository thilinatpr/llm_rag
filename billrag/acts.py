"""Certified Acts next to the bills they came from.

An Act is stored like a bill (`bills` + `chunks`, kind "Act", key "Act 25/2026"), so search, grading and citations work on it.
The key cannot be mistaken for bill 025/2026, whose number means something else. The side tables here say how the two relate:

act_links     bill_key -> act_key. Decided from the TEXT: titles recur and one title can have two Acts in a day (26/2026, 27/2026).
              direct=1: the final round (GS + Bill) that became the Act. direct=0: an earlier round of the same text that was
              re-presented later; that bill did not itself become the Act, and its sections are not compared.
act_sections  how a linked bill's sections line up with the Act's: same | changed | dropped (its text is not in the Act) |
              moved (no matching clause, but the text is elsewhere in the Act) | added (in the Act, not the bill). Section numbers shift when a clause is dropped, so never compare them directly.

What this can honestly say: "this bill became Act X" and "this clause is not in Act X". Not knowing of an Act does not mean a bill
failed, so no_act_status() words that as "we hold no Act". An amending Act lists changes: it is not the amended law's current text.
"""
import re
import unicodedata
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

ACT_SINCE = 2024  # Acts indexed from this year: a bill older than this may have become an Act we do not hold
LINK_TITLE = 0.8  # a similar title needs this title similarity and LINK_TEXT shared text
LINK_TEXT = 0.35
LINK_TEXT_ONLY = 0.75  # the same text under a different title (a bill renamed on the way)
LINK_SOON = (0.95, 0.1, 200)  # same title, certified within this many days: heavily rewritten text still links (Appropriation)
LINK_DAYS = 800  # older than this before the Act: another instance of a recurring title ...
LINK_LATE = (0.6, 1300)  # ... unless the text still matches (incorporation bills from 2021 certified in 2024)
ROUND_GAP = 120  # days between two versions that make them separate rounds (a GS and its Bill are weeks apart)
MATCH = 0.45  # below this two sections are not the same clause
MOVED = 0.5  # this share of an unmatched bill clause still found somewhere in the Act: moved or merged, not dropped
_TARGET = re.compile(r"^\s*section\s+(\d+[a-z]?)\s+of\b", re.I)
_OWN_NO = re.compile(r"\bact no(?: \d+)? of \d{4}\b")
# Text that is not the clause: margin notes leak into the extracted text of bills and Acts, at different places.
_MARGIN = re.compile(r"principal enactment|^(?:amendment|replacement|insertion|repeal|interpretation|short title)\b|^of section\b|\bact no(?: \d+)? of \d{4}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS act_links(bill_key TEXT PRIMARY KEY, act_key TEXT NOT NULL, score REAL, title_sim REAL, direct INT);
CREATE INDEX IF NOT EXISTS act_links_act ON act_links(act_key);
CREATE TABLE IF NOT EXISTS act_sections(
  act_key TEXT NOT NULL, bill_key TEXT NOT NULL, bill_section INT, act_section INT, ratio REAL, status TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS act_sections_bill ON act_sections(bill_key, bill_section);
CREATE INDEX IF NOT EXISTS act_sections_act ON act_sections(act_key, act_section);
"""


def act_key(no, year) -> str:
    return f"Act {int(no)}/{int(year)}"


def is_act(key) -> bool:
    return str(key).startswith("Act ")


def _toks(text):
    t = unicodedata.normalize("NFKC", text or "").lower()
    return _OWN_NO.sub(" ", " ".join(re.findall(r"[a-z0-9]+", t))).split()  # a bill leaves its own number blank; the Act fills it in


def _shingles(tokens, n=3):
    return Counter(tuple(tokens[i:i + n]) for i in range(max(0, len(tokens) - n + 1)))


def _overlap(a: Counter, b: Counter) -> float:
    """Shared share of the two texts (multiset Jaccard)."""
    if not a or not b:
        return 0.0
    shared = sum((a & b).values())
    return shared / (sum(a.values()) + sum(b.values()) - shared)


def _ratio(a, b):
    return SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 8000).ratio()


def _d(s):
    return datetime.fromisoformat(s[:10])


def _targets_differ(a, b):
    """"Section 81 of the principal enactment ..." and "Section 82 of ..." read alike and are different amendments."""
    ma, mb = _TARGET.match(a), _TARGET.match(b)
    return bool(ma and mb and ma.group(1).lower() != mb.group(1).lower())


def material_differences(a: list, b: list) -> list:
    """Word differences between two token lists that are not extraction noise -> [(a words, b words)]. A section is `same` only when
    this is empty. A similarity threshold cannot decide it: "twenty" -> "ten" in a long clause still scores 0.99. So anything not
    positively recognised as noise counts, and a wrong call errs towards "changed", which shows the reader both texts."""
    out = []
    for op, i, j, k, l in SequenceMatcher(None, a, b, autojunk=len(a) + len(b) > 8000).get_opcodes():
        if op == "equal":
            continue
        x, y = " ".join(a[i:j]), " ".join(b[k:l])
        if x.replace(" ", "") == y.replace(" ", ""):  # "sub section" / "subsection", "54 AA" / "54AA"
            continue
        noise_x, noise_y = not x or bool(_MARGIN.search(x)), not y or bool(_MARGIN.search(y))
        if noise_x and noise_y:
            continue
        out.append((x, y))
    return out


def align(bill: dict, act: dict, act_full: str = "") -> list:
    """{no: text} x {no: text} -> [(bill_no, act_no, ratio, status)]. Order-preserving alignment of the best-matching clauses.
    act_full: the Act's whole text including schedules, for telling a dropped clause from a moved one."""
    bn, an = sorted(bill), sorted(act)
    bt, at = [_toks(bill[n]) for n in bn], [_toks(act[n]) for n in an]
    bs, as_ = [set(t) for t in bt], [set(t) for t in at]

    def sim(i, j):
        if _targets_differ(bill[bn[i]], act[an[j]]):
            return 0.0
        u = len(bs[i] | as_[j])
        if not u or len(bs[i] & as_[j]) / u < 0.2:
            return 0.0
        r = _ratio(bt[i], at[j])
        return r if r >= MATCH else 0.0

    grid = [[sim(i, j) for j in range(len(an))] for i in range(len(bn))]
    best = [[0.0] * (len(an) + 1) for _ in range(len(bn) + 1)]
    for i in range(len(bn) - 1, -1, -1):
        for j in range(len(an) - 1, -1, -1):
            best[i][j] = max(best[i + 1][j], best[i][j + 1], grid[i][j] + best[i + 1][j + 1] if grid[i][j] else 0.0)
    out, i, j = [], 0, 0
    while i < len(bn) and j < len(an):
        if grid[i][j] and best[i][j] == grid[i][j] + best[i + 1][j + 1]:
            r = grid[i][j]
            out.append((bn[i], an[j], r, "changed" if material_differences(bt[i], at[j]) else "same"))
            i, j = i + 1, j + 1
        elif best[i][j] == best[i + 1][j]:
            out.append((bn[i], None, 0.0, "dropped"))
            i += 1
        else:
            out.append((None, an[j], 0.0, "added"))
            j += 1
    out += [(bn[x], None, 0.0, "dropped") for x in range(i, len(bn))]
    out += [(None, an[x], 0.0, "added") for x in range(j, len(an))]
    # "Not in the Act" is a strong claim, so test it against the whole Act: a clause that was moved or merged is not dropped.
    whole = _shingles(_toks(act_full) if act_full else [t for ts in at for t in ts])
    targets = {m.group(1).lower() for t in act.values() if (m := _TARGET.match(t))}
    for n, (b, a, r, status) in enumerate(out):
        if status == "dropped":
            sh = _shingles(_toks(bill[b]))
            m = _TARGET.match(bill[b])
            if m and m.group(1).lower() not in targets:  # amendment clauses share boilerplate: only the amended section tells them apart
                continue
            if sh and sum((sh & whole).values()) / sum(sh.values()) >= MOVED:
                out[n] = (b, None, 0.0, "moved")
    return out


def build(store) -> dict:
    """(Re)compute act_links + act_sections from the indexed text. Deterministic; no model."""
    db = store.db
    db.executescript(SCHEMA)
    db.execute("DELETE FROM act_links")
    db.execute("DELETE FROM act_sections")
    rows = [dict(r) for r in db.execute("SELECT key, kind, date, title_norm FROM bills ORDER BY date")]
    # Base Acts (older than ACT_SINCE, held so that an amending bill's target is in the index: baseacts.py) are never the end of a bill's
    # journey here: a bill amending the Inland Revenue Act would otherwise be matched to the 2017 Act it amends.
    acts_ = [r for r in rows if r["kind"] == "Act" and int(r["key"].rsplit("/", 1)[1]) >= ACT_SINCE]
    if not acts_:
        db.commit()
        return {"acts": 0, "links": 0}
    first = min(_d(a["date"]) for a in acts_)
    bills = [r for r in rows if r["kind"] != "Act" and (first - _d(r["date"])).days <= LINK_LATE[1]]
    secs = {r["key"]: store.section_map(r["key"]) for r in acts_ + bills}
    sh = {k: _shingles(_toks(" ".join(v[n] for n in sorted(v)))) for k, v in secs.items()}
    best = {}  # a bill goes with at most one Act: the one whose text it shares most
    for a in acts_:
        for b in bills:
            gap = (_d(a["date"]) - _d(b["date"])).days
            if gap < -3 or gap > LINK_LATE[1] or not sh[b["key"]]:
                continue
            ts = SequenceMatcher(None, a["title_norm"] or "", b["title_norm"] or "").ratio()
            if ts < LINK_TITLE and not _quick_shared(sh[a["key"]], sh[b["key"]]):
                continue
            score = _overlap(sh[a["key"]], sh[b["key"]])
            ok = ((ts >= LINK_TITLE and score >= LINK_TEXT) or score >= LINK_TEXT_ONLY
                  or (ts >= LINK_SOON[0] and score >= LINK_SOON[1] and gap <= LINK_SOON[2]))
            if ok and (gap <= LINK_DAYS or score >= LINK_LATE[0]) and score > best.get(b["key"], (0,))[0]:
                best[b["key"]] = (score, a["key"], ts)
    dates = {r["key"]: _d(r["date"]) for r in rows}
    per_act = {}
    for b in best:
        per_act.setdefault(best[b][1], []).append(b)
    direct = set()
    for ak, keys in per_act.items():  # the latest round before the Act: walk back while the versions are close together
        keys.sort(key=lambda k: dates[k], reverse=True)
        direct.add(keys[0])
        for newer, older in zip(keys, keys[1:]):
            if (dates[newer] - dates[older]).days > ROUND_GAP:
                break
            direct.add(older)
    db.executemany("INSERT INTO act_links VALUES(?,?,?,?,?)", [(b, v[1], v[0], v[2], int(b in direct)) for b, v in best.items()])
    counts = Counter()
    for bk in direct:
        ak = best[bk][1]
        full = " ".join(r[0] for r in db.execute("SELECT text FROM chunks WHERE bill_key=? AND kind IN ('section','schedule') ORDER BY ord", (ak,)))
        for bn, an, r, status in align(secs[bk], secs[ak], full):
            db.execute("INSERT INTO act_sections VALUES(?,?,?,?,?,?)", (ak, bk, bn, an, r, status))
            counts[status] += 1
    db.commit()
    return {"acts": len(acts_), "linked_acts": len(per_act), "linked_bills": len(direct), "earlier_rounds": len(best) - len(direct), **counts}


def _quick_shared(a: Counter, b: Counter) -> bool:
    return len(a.keys() & b.keys()) >= 0.5 * min(len(a), len(b))


# ---- reads: tolerate an index built before these tables existed
def has_tables(store) -> bool:
    return bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='act_links'").fetchone())


def act_for_bill(store, bill_key):
    """The Act a bill went on to, as its `bills` row plus "direct" (see act_links), or None."""
    if not has_tables(store):
        return None
    r = store.db.execute("SELECT act_key, direct FROM act_links WHERE bill_key=?", (bill_key,)).fetchone()
    return {**store.bill(r[0]), "direct": bool(r[1])} if r else None


def bills_for_act(store, key):
    if not has_tables(store):
        return []
    return [r[0] for r in store.db.execute("SELECT bill_key FROM act_links WHERE act_key=? AND direct=1 ORDER BY bill_key", (key,))]


def bill_section(store, bill_key, no):
    """-> {"act_key", "act_section", "ratio", "status"} for one bill section, or None if it was never compared."""
    if not has_tables(store):
        return None
    r = store.db.execute("SELECT act_key, act_section, ratio, status FROM act_sections WHERE bill_key=? AND bill_section=?",
                         (bill_key, int(no))).fetchone()
    return dict(r) if r else None


def changes(store, act):
    """How an Act differs from the bill it came from: rows of act_sections other than `same`, for the bill closest to the Act."""
    if not has_tables(store):
        return []
    bills = store.db.execute("SELECT l.bill_key FROM act_links l JOIN bills b ON b.key=l.bill_key WHERE l.act_key=? AND l.direct=1 "
                             "ORDER BY b.date DESC LIMIT 1", (act,)).fetchone()
    if not bills:
        return []
    return [dict(r) for r in store.db.execute(
        "SELECT * FROM act_sections WHERE act_key=? AND bill_key=? AND status!='same' ORDER BY COALESCE(bill_section, act_section)",
        (act, bills[0]))]


def equivalents(store, bill_key, no=None):
    """Act clauses that say what this bill clause says (no=None: the Act itself): [(act key, act section or None)].
    Only for `same` or `changed` clauses: a dropped clause has no equivalent."""
    if no is None:
        a = act_for_bill(store, bill_key)
        return [(a["key"], None)] if a and a["direct"] else []
    m = bill_section(store, bill_key, no)
    return [(m["act_key"], m["act_section"])] if m and m["act_section"] is not None else []


def no_act_status(store, bill) -> str:
    """Status line for a bill that links to no Act: what we can say depends on whether we hold Acts for its period."""
    if bill["date"][:4] < str(ACT_SINCE):
        return "Proposal, not itself law. Whether it later became an Act is not checked: this index holds Acts only from 2024."
    last = store.db.execute("SELECT MAX(date) FROM bills WHERE kind='Act'").fetchone()[0]
    if not last:
        return "Proposal, not itself law. Whether it became an Act is not known."
    return (f"Proposal, not itself law. We hold no certified Act for it (Acts checked up to {last[:10]}), so say it has not been "
            "shown to have passed, not that it failed.")


def status_lines(store, key, section=None):
    """Plain-language status of one passage for the answer step (no [ ] so it can't pass for a citation)."""
    b = store.bill(key)
    if not b:
        return ""
    if b["kind"] == "Act":
        purpose = re.sub(r"\s+", " ", b.get("long_title") or "").strip()[:220]
        came = bills_for_act(store, key)
        return (f"Certified Act (certified {b['date'][:10]}): law as passed."
                + (f" Its long title: {purpose}." if purpose else "")
                + (f" It came from bill {', '.join(came)}." if came else "")
                + " An amending Act states changes; it is not the amended Act's current text, and a number mentioned inside a clause "
                  "does not say which Act is being amended.")
    a = act_for_bill(store, key)
    if not a:
        return no_act_status(store, b)
    if not a["direct"]:
        return (f"An earlier round of this proposal, not itself law. A later bill of similar text became {a['key']} (certified "
                f"{a['date'][:10]}): rely on that Act, if it is in the evidence, for what became law.")
    head = f"Proposal; this bill became {a['key']} (certified {a['date'][:10]})."
    m = bill_section(store, key, section) if section is not None else None
    if not m:
        return head + " Rely on the Act for what became law."
    if m["status"] == "dropped":
        return (f"{head} The wording of this clause is not in the Act: it was proposed but not enacted in these words. Do not state it "
                "as law. The Act may treat the topic in other words, so do not say the Act is silent unless the evidence shows it.")
    if m["status"] == "moved":
        return f"{head} No matching clause in the Act, though similar text appears elsewhere in it: check the Act before relying on this."
    tail = "The Act's wording differs" if m["status"] == "changed" else "Same text as"
    return f"{head} {tail} {m['act_key']} s.{m['act_section']}" + (": rely on the Act." if m["status"] == "changed" else ".")
