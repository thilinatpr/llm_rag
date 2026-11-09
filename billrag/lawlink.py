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


def passages(store, retriever, query, k=4, chars=SECTION_CHARS):
    """-> {"passages": [{cite, key, section, title, status, text}], "notes": {key: note}}.
    Certified Acts and consolidated texts come before bills among the relevant hits: an officer needs the law in force, a bill is a proposal."""
    k = max(1, min(int(k), MAX_K))
    hits = retriever.search(query, k=k * 3, kinds=CLAUSE_KINDS)
    order = sorted(range(len(hits)), key=lambda i: (not _is_law(hits[i]["bill_key"]), i))
    out, seen = [], set()
    for i in order:
        c = hits[i]
        key, sec = c["bill_key"], c.get("section_no")
        if (key, sec) in seen:
            continue
        seen.add((key, sec))
        whole = store.section_text(key, sec) if sec is not None and hasattr(store, "section_text") else None
        text = shown_text({**c, "text": whole["text"]}) if whole else shown_text(c)
        if len(text) > chars:
            text = text[:chars].rsplit(" ", 1)[0] + " (... the rest of this section is not shown ...)"
        out.append({"cite": f"[{key} §{sec}]" if sec is not None else f"[{key}]", "key": key, "section": sec,
                    "title": version_tag(c), "status": acts.status_lines(store, key, sec), "text": text})
        if len(out) >= k:
            break
    keys = [p["key"] for p in out]
    notes = {}
    for k_ in dict.fromkeys(keys):
        for fn in (consolidated.note, baseacts.note, transcribed.note):
            n = fn(store, [k_] + [x for x in keys if x != k_ and x.startswith("Cons ")])
            if n:
                notes.setdefault(k_, []).append(n)
    return {"passages": out, "notes": {k_: " ".join(v) for k_, v in notes.items()}}


CITE = re.compile(r"\[((?:Act|Cons) \d{1,3}/\d{4}|\d{3}/\d{4})(?:\s*§\s*(\d+))?")
