"""IRD consolidated texts of tax Acts held next to the certified Act (owner's decision, 2026-11-09).

The Inland Revenue Department (ird.gov.lk, "the official source for tax related matters") publishes consolidated versions of the tax
Acts it administers: every amendment folded in, each change noted in the margin ("Sec 2(1) of 10 of 2026"). They say of themselves:
"not a statutory consolidation, but rather a convenient reference". In the index:
- key "Cons N/YYYY", kind "Cons", next to the certified "Act N/YYYY" (never instead of it); citations read [Cons 25/2022 §4];
- table `consolidations` (side table: chunks and vectors untouched): which Act, amendments included up to which date, which Acts;
- every answer that uses one gets a fixed note (web.py); the model's STATUS line says the same (acts.status_lines);
- made by hand from the PDF (billrag/consolidation.py), checked section by section against the certified Act, accepted by the owner,
  stored in sources/consolidations/ and added by the refresh build (scripts/refresh_bills.py).
"""
import json
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS consolidations(bill_key TEXT PRIMARY KEY, act_key TEXT NOT NULL, up_to TEXT NOT NULL, amendments TEXT,
  source TEXT, prepared TEXT);
"""
KIND = "Cons"
SOURCE_TAG = " - IRD consolidated text"


def cons_key(no, year):
    return f"Cons {int(no)}/{int(year)}"


def record(store, key, act_key, up_to, amendments=(), source=None, prepared=None):
    store.db.executescript(SCHEMA)
    store.db.execute("INSERT OR REPLACE INTO consolidations VALUES(?,?,?,?,?,?)",
                     (key, act_key, up_to, json.dumps(list(amendments)), source, prepared))


def info(store, key):
    try:
        r = store.db.execute("SELECT act_key, up_to, amendments, source, prepared FROM consolidations WHERE bill_key=?", (key,)).fetchone()
    except Exception:  # an index without the table
        return None
    if not r:
        return None
    return {"act_key": r[0], "up_to": r[1], "amendments": json.loads(r[2] or "[]"), "source": r[3], "prepared": r[4]}


def _readable(key):
    m = re.match(r"Act (\d+)/(\d{4})$", key)
    return f"Act No. {m.group(1)} of {m.group(2)}" if m else key


def _date(iso):
    from datetime import date
    d = date.fromisoformat(iso[:10])
    return f"{d.day} {d.strftime('%B')} {d.year}"


def later_amendments(store, key):
    """Indexed Acts that amend the consolidated Act and were certified after the consolidation's cut-off date (not included in it)."""
    from . import baseacts
    i = info(store, key)
    if not i:
        return []
    out = []
    for k in baseacts.amended_by(store, i["act_key"]):
        b = store.bill(k)
        if k.startswith("Act ") and b and b["date"][:10] > i["up_to"] and k not in i["amendments"]:
            out.append(k)
    return out


def describe(store, key):
    i = info(store, key)
    b = store.bill(key) if hasattr(store, "bill") else None
    if not i or not b:
        return ""
    act = store.bill(i["act_key"])
    act_title = (act or {}).get("title") or _readable(i["act_key"])
    later = later_amendments(store, key)
    tail = (" Amendments after that date, not included: " + ", ".join(_readable(k) for k in later) + ".") if later else ""
    return (f"Inland Revenue Department consolidated text of the {act_title}, with amendments up to {_date(i['up_to'])}: a convenient "
            f"reference, not a statutory text.{tail}")


STATUS_TAIL = (" Use it for the law as amended; where the evidence also has the certified Act's wording, that is the official text. "
               "Cite this passage as it is labelled, and say an amendment is included only when an IRD note or the text shows it.")
NOTE_TAIL = "Before a high-stakes decision, check the certified Act and its amending Acts."


def note(store, evidence_keys):
    """Fixed note under an answer whose evidence includes a consolidated text (shown by web.py); None if none."""
    keys = [k for k in dict.fromkeys(evidence_keys) if info(store, k)]
    if not keys:
        return None
    return " ".join("Parts of this answer use the " + describe(store, k) for k in keys) + " " + NOTE_TAIL
