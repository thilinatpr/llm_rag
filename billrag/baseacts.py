"""Base Acts: the law an amending bill or Act changes must be in the index too.

A bill or Act that "amends the Inland Revenue Act, No. 24 of 2017" says what changes, not what the law now reads. Without the amended
Act in the index we can answer "what did the 2026 amendment do?" but not "what is the rule?". So every Act that a bill or Act (2024 on) names
as amended, repealed or replaced in its long title must be indexed as an Act (key "Act 24/2017"), or be waived on purpose.

`check` sorts each required Act into: held (in the index), fetchable (documents.gov.lk lists it with an English PDF, so the refresh can
fetch it), missing (not listed, or listed without English: someone has to find the text), waived (the owner decided, with a reason, in
`evals/base_acts_waived.json`). `unresolved` is what is neither held, fetchable nor waived: the refresh and the publish step refuse
while it is not empty, and the admin panel shows the list so the owner can find those documents.
Scope: only the long title (a reference in a clause is not followed), documents from CHECK_SINCE on.
"""
import json
import re
from pathlib import Path

CHECK_SINCE = "2024-01-01"  # documents dated from here must have their base Acts: the ones people ask about
WAIVED_FILE = Path(__file__).resolve().parent.parent / "evals" / "base_acts_waived.json"
_AMENDING = re.compile(r"\b(amend|repeal|replace)", re.I)
_NUMBERED = re.compile(r"(Act|Ordinance)\s*,?\s*No\.?\s*(\d+)\s*(?:of|,)\s*(\d{4})")
_CHAPTER = re.compile(r"((?:(?:[A-Z][\w'’&-]*|and|of)\s+){0,8}(?:Act|Ordinance))\s*\(Chapter\s+(\d+)\)")
_NAME_START = re.compile(r"(?:\bamend(?:ed)?\s+|\brepeal\s+|\breplace\s+|\band\s+(?=the\s)|\bthe\s+)", re.I)


def _name(segment: str) -> str:
    """The Act's name from the text before 'Act, No. ..': what follows the last 'the' / 'amend' / 'and the'."""
    parts = _NAME_START.split(segment)
    return re.sub(r"\s+", " ", parts[-1]).strip(" ,;")


def references(long_title: str) -> dict:
    """{id: name} of the Acts a long title amends/repeals/replaces. id is "15/1979" (number/year) or "Chapter 351" (revised edition, no year)."""
    lt = long_title or ""
    if not _AMENDING.search(lt):
        return {}
    out, last = {}, 0
    for m in _NUMBERED.finditer(lt):
        name = _name(lt[last:m.start()])
        out[f"{int(m.group(2))}/{m.group(3)}"] = f"{name} {m.group(1)}".strip()
        last = m.end()
    for m in _CHAPTER.finditer(lt):
        out.setdefault(f"Chapter {m.group(2)}", re.sub(r"^(?:and|of)\s+", "", m.group(1)))
    return out


def required(store, since=CHECK_SINCE) -> dict:
    """{id: {"name", "cited_by": [keys]}} for every bill / Gazette text / Act dated `since` or later."""
    out = {}
    for r in store.db.execute("SELECT key, kind, long_title FROM bills WHERE kind IN ('Bill','GS','Act') AND date >= ? ORDER BY key", (since,)):
        for ident, name in references(r["long_title"]).items():
            e = out.setdefault(ident, {"name": name, "cited_by": []})
            e["cited_by"].append(r["key"])
    return out


def held(store) -> set:
    return {r[0][4:] for r in store.db.execute("SELECT key FROM bills WHERE kind='Act'")}  # "Act 24/2017" -> "24/2017"


def load_listing(path) -> dict | None:
    """documents.gov.lk acts.json -> {"24/2017": {"title", "english": has an English PDF}}; None when there is no listing."""
    p = Path(path)
    if not p.is_file():
        return None
    out = {}
    for a in json.loads(p.read_text(encoding="utf-8")):
        if a.get("actNo"):
            out[f"{a['actNo']}/{a['actSubNo']}"] = {"title": (a.get("descriptionEnglish") or "").strip(),
                                                      "english": any(c.get("language") == "ENGLISH" for c in a.get("contents", []))}
    return out


def load_waived(path=None) -> dict:
    p = Path(path or WAIVED_FILE)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def check(store, listing=None, waived=None, since=CHECK_SINCE) -> dict:
    """-> {"required": n, "held": n, "fetchable": {id: item}, "waived": {id: item}, "unresolved": {id: item}}; item = {name, cited_by, why}.
    listing None = we do not know what can be fetched, so everything not held or waived is unresolved."""
    waived = load_waived() if waived is None else waived
    have = held(store)
    res = {"required": 0, "held": 0, "fetchable": {}, "waived": {}, "unresolved": {}}
    for ident, e in required(store, since).items():
        res["required"] += 1
        if ident in have:
            res["held"] += 1
        elif ident in waived:
            res["waived"][ident] = {**e, "why": waived[ident]}
        elif listing is not None and listing.get(ident, {}).get("english"):
            res["fetchable"][ident] = {**e, "why": "documents.gov.lk has the English PDF"}
        else:
            if listing is None:
                why = "not in the index"
            elif ident not in listing:
                why = "not on documents.gov.lk (it lists Acts from 1980 and not all of those; chapter-numbered Acts are not listed)"
            else:
                why = "listed on documents.gov.lk without an English PDF"
            res["unresolved"][ident] = {**e, "why": why}
    return res


def describe(ident: str, item: dict) -> str:
    n = len(item["cited_by"])
    shown = ", ".join(item["cited_by"][:3]) + (" ..." if n > 3 else "")
    return f"{item['name'] or '?'} ({ident}), amended by {n} document{'s' if n != 1 else ''} ({shown}): {item['why']}"


def problem(res: dict) -> str | None:
    """One line for a failed check, or None."""
    if not res["unresolved"]:
        return None
    names = "; ".join(f"{v['name'] or '?'} ({k})" for k, v in sorted(res["unresolved"].items()))
    return (f"{len(res['unresolved'])} base Act(s) named by indexed bills/Acts are not in the index and cannot be fetched: {names}. "
            f"Find the text and add it (python -m billrag add-base-act), or record why not in evals/base_acts_waived.json")
