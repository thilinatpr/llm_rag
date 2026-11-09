"""Law passages for another citizenlk service (Procurement Rules), without a model call: POST /api/evidence.

Procurement Rules holds procurement rules, not tax law; Bill Assistant holds Acts, IRD consolidated texts and bills. When a procurement question
needs the law (who owes a levy, what an Act says), the Procurement container asks this server through the two Workers (service binding +
shared secret, see src/lawlink.js and workers/procurement/handler.js). It gets passages built exactly as Bill Assistant's own answers see
them: the citation label, the version, the STATUS line (certified / as enacted / consolidated / scanned / bill), the clause text after the
read-time cleanup, and the fixed notes, so the same warnings travel with the text.
"""
import re

from . import acts, baseacts, consolidated, transcribed
from .pipeline import CLAUSE_KINDS, shown_text, version_tag

MAX_K = 6
SECTION_CHARS = 2500


def _is_law(key):
    return key.startswith(("Act ", "Cons "))


# words that can look like an abbreviation in a question ("can we pay") but are just words
COMMON = set("""the and for are was can may not but its has had who how why what when where which will with from into this that than
then they them their there here pay paid owe due tax act law rule rules levy fee fees duty bill bills date year years month time cost
any all our your you him her his she one two per vat""".split())


def focus(store, query):
    """(keys, title) of the one law the question names, else ((), None): an abbreviation in any case ("sscl", "PAYE") that is exactly the
    initials of an Act's or consolidation's title, or a clear title match (titles.Ranker). Live case 2026-11-09: "... asks for sscl
    payments" searched as typed found the Electricity Act and SSCL s.21 (appeals), not s.2/s.3."""
    from . import resolve, titles
    law = [dict(r) for r in store.db.execute("SELECT key, title, year FROM bills WHERE kind IN ('Act', 'Cons')")]
    for tok in dict.fromkeys(re.findall(r"\b[A-Za-z]{3,8}\b", query)):
        if tok.lower() in COMMON or not (tok.isupper() or tok.islower()):
            continue
        cands = [c for c in resolve.match_acronym(tok, law, limit=8) if c[0] >= 1.0]
        names = {_law_name(t) for _s, t, _k in cands}
        if len(names) == 1:  # the Act, its amending Acts and its consolidation are one law; two different laws: do not guess
            title = min((t for _s, t, _k in cands), key=len)
            keys = [k for _s, _t, ks in cands for k in ks]
            return tuple(_family(store, title, keys)), _law_name(title, keep_case=True)
    if titles.has_index(store):
        decision, rows = titles.Ranker(store).rank(query)
        if decision == "one":
            import json
            keys = rows[0].get("keys")
            keys = json.loads(keys) if isinstance(keys, str) else (keys or [rows[0]["key"]])
            law_keys = [k for k in keys if _is_law(k)]
            if law_keys:
                return tuple(_family(store, rows[0]["title"], law_keys)), rows[0]["title"]
    return (), None


def _law_name(title, keep_case=False):
    """'Social Security Contribution Levy (Amendment) Act, No. 10 of 2026' / '... Act, No. 25 of 2022 (IRD consolidated text, ...)'
    -> 'social security contribution levy'."""
    t = re.sub(r"\([^)]*(?:amendment|consolidated)[^)]*\)", " ", title or "", flags=re.I)
    t = re.sub(r"\bAct\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,")
    return t if keep_case else t.lower()


def _family(store, title, keys):
    """The Act, its IRD consolidation and the Acts that amend it (their titles share the name: "X (Amendment) Act")."""
    from .resolve import base_title
    name = _law_name(base_title(title))
    out = list(dict.fromkeys(keys))
    for k, t in store.db.execute("SELECT key, title FROM bills WHERE kind IN ('Act', 'Cons')"):
        if k not in out and _law_name(t) == name:
            out.append(k)
    return out


def _section_id(key, sec):
    """The same section of an Act and of its consolidation count once: "Act 25/2022" and "Cons 25/2022" -> "25/2022"."""
    return (key.split(" ", 1)[-1] if _is_law(key) else key, sec)


def passages(store, retriever, query, k=4, chars=SECTION_CHARS):
    """-> {"passages": [{cite, key, section, title, status, text}], "notes": {key: note}, "focus": title or None}.
    The law the question names comes first (searched with its name spelled out); a section held both as enacted and consolidated is
    given once, consolidated (the current text); then other relevant law; bills only when there is too little law."""
    k = max(1, min(int(k), MAX_K))
    keys, title = focus(store, query)
    hits = []
    if keys:
        # the charging provision first: who owes it, on what, at what rate (the question's own words often match recovery or refund
        # sections instead: "payments" -> s.28, s.29, s.36 on the live SSCL question)
        hits += retriever.search(f"{title} imposition of the levy tax charged from every taxable person liable turnover rate per centum",
                                 k=3, keys=list(keys), kinds=CLAUSE_KINDS)
        hits += retriever.search(query + " " + title, k=k * 3, keys=list(keys), kinds=CLAUSE_KINDS)
    hits += retriever.search(query, k=k * 3, kinds=CLAUSE_KINDS)
    # consolidated before as-enacted for the same section; otherwise keep the search order
    cons_first = sorted(range(len(hits)), key=lambda i: (i if not hits[i]["bill_key"].startswith("Cons ") else i - 0.5))
    chosen, seen = [], set()
    for i in cons_first:
        c = hits[i]
        sid = _section_id(c["bill_key"], c.get("section_no"))
        if sid in seen or (c.get("section_no") is None and not keys):  # a schedule or overview without a section: only for the named law
            continue
        seen.add(sid)
        chosen.append(c)
    law = [c for c in chosen if _is_law(c["bill_key"])]
    chosen = (law if len(law) >= 2 else chosen)[:k]
    out = []
    for c in chosen:
        key, sec = c["bill_key"], c.get("section_no")
        whole = store.section_text(key, sec) if sec is not None and hasattr(store, "section_text") else None
        text = shown_text({**c, "text": whole["text"]}) if whole else shown_text(c)
        if len(text) > chars:
            text = text[:chars].rsplit(" ", 1)[0] + " (... the rest of this section is not shown ...)"
        out.append({"cite": f"[{key} \u00a7{sec}]" if sec is not None else f"[{key}]", "key": key, "section": sec,
                    "title": version_tag(c), "status": acts.status_lines(store, key, sec), "text": text})
    keys_out = [p["key"] for p in out]
    notes = {}
    for k_ in dict.fromkeys(keys_out):
        for fn in (consolidated.note, baseacts.note, transcribed.note):
            n = fn(store, [k_] + [x for x in keys_out if x != k_ and x.startswith("Cons ")])
            if n:
                notes.setdefault(k_, []).append(n)
    return {"passages": out, "notes": {k_: " ".join(v) for k_, v in notes.items()}, "focus": title}


CITE = re.compile(r"\[((?:Act|Cons) \d{1,3}/\d{4}|\d{3}/\d{4})(?:\s*§\s*(\d+))?")
