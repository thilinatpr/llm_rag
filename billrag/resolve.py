"""Ground the user's question before retrieval: greetings, and abbreviations like "PTSA" that name a bill.

The model used to guess what an unknown abbreviation stands for ("Public Transport Services Act"), then retrieve and answer
about the wrong law. Here an abbreviation is only accepted if it matches the initials of a real bill title (allowing a
transposition such as PTSA ~ PSTA); otherwise the user is asked, with the closest titles listed.
"""
import difflib
import re

SMALLTALK = re.compile(
    r"^\s*(hi+|hello+|hey+|hiya|yo|good\s+(morning|afternoon|evening)|greetings|thanks?(\s+you)?|thank\s+you(\s+(so\s+)?much)?|"
    r"ok(ay)?|cool|great|nice|bye|goodbye|help|who\s+are\s+you|what\s+(can|do)\s+you\s+do|how\s+are\s+you|test(ing)?)\b[\s!.?,]*"
    r"(there|again|bot|assistant)?[\s!.?]*$", re.I)

HELP = ("Hello! I answer questions about bills of the Parliament of Sri Lanka (from documents.gov.lk), quoting the clauses "
        "I rely on. Try:\n"
        "- How does the 2026 terrorism bill limit detention?\n"
        "- Summarise the Protection of the State from Terrorism Bill\n"
        "- What changed between 070/2026 and 078/2026?\n"
        "You can name a bill by its title or number (e.g. 078/2026). I explain the text; this is not legal advice.")

STOP = {"of", "the", "and", "for", "from", "in", "to", "on", "a", "an", "by", "with", "at", "no"}
GENERIC = {"act", "bill", "amendment", "gs"}


THANKS = re.compile(r"^\s*(thanks?|thank\s+you|bye|goodbye|cool|great|nice|ok(ay)?)\b", re.I)


def is_smalltalk(question):
    return bool(SMALLTALK.match(question))


def smalltalk_reply(question):
    if THANKS.match(question):
        return "You're welcome! Ask me about any other bill whenever you like."
    return HELP


def base_title(title):
    return re.sub(r"\s+-\s+(Bill|GS)\s*$", "", title or "", flags=re.I).strip()


def _initials(words):
    return "".join(w[0] for w in words).upper()


def acronym_variants(title):
    """Plausible abbreviations of a bill title: with/without bracketed parts, with/without a trailing A (for Act)."""
    t = base_title(title)
    out = set()
    for text in (t, re.sub(r"\([^)]*\)", " ", t)):
        words = [w for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in STOP and w.lower() not in GENERIC]
        if not words:
            continue
        base = _initials(words)
        out |= {base, base + "A"}
    return {v for v in out if len(v) >= 2}


def acronym_score(token, variant):
    token, variant = token.upper(), variant.upper()
    if token == variant:
        return 1.0
    if sorted(token) == sorted(variant):  # same letters, order slipped (PTSA vs PSTA)
        return 0.9
    return round(difflib.SequenceMatcher(None, token, variant).ratio(), 3)


def match_acronym(token, bills, year=None, limit=3):
    """bills: dicts with key/title/year. Returns [(score, title, [keys])] best first, grouped by title."""
    best = {}
    for b in bills:
        if year and int(b["year"]) != int(year):
            continue
        s = max((acronym_score(token, v) for v in acronym_variants(b["title"])), default=0)
        if s < 0.7:
            continue
        t = base_title(b["title"])
        cur = best.setdefault(t.lower(), [0, t, []])
        cur[0] = max(cur[0], s)
        cur[2].append(b["key"])
    rows = sorted(best.values(), key=lambda r: (-r[0], r[1]))
    return [(s, t, sorted(set(k))) for s, t, k in rows[:limit]]


def candidate_tokens(question):
    """ALL-CAPS tokens of 3-8 letters that look like abbreviations (not 'GS'/'LKR'-style 2-letter ones)."""
    return [t for t in dict.fromkeys(re.findall(r"\b[A-Z]{3,8}\b", question))]


def known_in_corpus(store, token):
    try:
        row = store.db.execute("SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1", (f'"{token}"',)).fetchone()
    except Exception:
        return True  # if we cannot check, do not interfere
    return row is not None


def all_bills(store):
    return [dict(r) for r in store.db.execute("SELECT key, title, year FROM bills")]


def resolve_question(store, question):
    """-> {"action": "answer", "question": str, "notes": [str]} | {"action": "reply", "text": str}"""
    if is_smalltalk(question):
        return {"action": "reply", "text": smalltalk_reply(question)}
    unknown = [t for t in candidate_tokens(question) if not known_in_corpus(store, t)]
    if not unknown:
        return {"action": "answer", "question": question, "notes": []}
    bills = all_bills(store)
    ym = re.search(r"\b(19|20)\d{2}\b", question)
    year = int(ym.group(0)) if ym else None
    q, notes = question, []
    for tok in unknown:
        cands = match_acronym(tok, bills, year) or (match_acronym(tok, bills) if year else [])
        confident = cands and cands[0][0] >= 0.85 and (len(cands) == 1 or cands[0][0] - cands[1][0] >= 0.05)
        if confident:
            score, title, keys = cands[0]
            q = re.sub(rf"\b{tok}\b", f"{title} ({', '.join(keys)})", q)
            notes.append(f"I read “{tok}” as the {title} ({', '.join(keys)}).")
            continue
        if cands:
            lines = "\n".join(f"- {t} ({', '.join(k)})" for _, t, k in cands)
            text = (f"I can't tell which bill “{tok}” refers to. Did you mean one of these?\n{lines}\n"
                    "Reply with the title or bill number (e.g. 078/2026) and I'll answer.")
        else:
            text = (f"I don't recognise “{tok}” as an abbreviation of any bill in the index"
                    f"{f' for {year}' if year else ''}. Please give the bill's full title or its number (e.g. 078/2026).")
        return {"action": "reply", "text": text}
    return {"action": "answer", "question": q, "notes": notes}


def closest_bills(store, question, year=None, limit=3):
    """Titles sharing the most words with the question; shown when nothing relevant was found."""
    words = [w for w in re.findall(r"[a-z]{4,}", question.lower()) if w not in STOP | GENERIC | {"summarise", "summary", "what", "about", "does", "tell"}]
    score = {}
    for w in set(words):
        for b in store.find_bills(title_contains=w, year=year, limit=40):
            t = base_title(b["title"])
            e = score.setdefault(t.lower(), [0, t, set()])
            e[0] += 1
            e[2].add(b["key"])
    rows = sorted(score.values(), key=lambda r: (-r[0], r[1]))[:limit]
    return [(t, sorted(k)) for _, t, k in rows]
