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
    return re.sub(r"\s+(?:-\s+(?:Bill|GS)|Act,?\s+No\.?\s*\d+\s+of\s+\d{4})\s*$", "", title or "", flags=re.I).strip()


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


# "Tell me about the new bill" names no bill: every word is filler. Searching on it matches whatever the word "new" happens to
# hit (live: a 2018 "New Villages ... Authority" bill, then "nothing newer exists"), so a first question like that is asked
# back instead. In a running chat the same words refer to the previous turn, and the condense step resolves them.
VAGUE_FILLER = set("""tell me about the this that these those new newest latest recent recently proposed upcoming current a an
bill bills act acts law laws draft drafts legislation please explain describe summarise summarize summary overview what is are
whats give info information details detail say more can you could i want to know show kindly regarding on of it its any
there which one ones introduced published presented parliament sri lanka lankan in""".split())
VAGUE_NEEDS = re.compile(r"\b(bills?|acts?|laws?|drafts?|legislation)\b", re.I)


def is_vague_bill_question(question):
    """True if the question refers to "the new/this bill" but contains no title word, bill number or year."""
    if re.search(r"\d", question) or not VAGUE_NEEDS.search(question):
        return False
    words = re.findall(r"[a-z]+", question.lower())
    return bool(words) and all(w in VAGUE_FILLER for w in words)


def which_bill_reply(store, limit=5):
    """Ask which bill is meant and list the newest ones (one line per title, GS + Bill versions together)."""
    lines, seen = [], set()
    for b in store.find_bills(limit=60):
        if b["kind"] == "Act":
            continue
        t = base_title(b["title"]).lower()
        if t in seen:
            continue
        seen.add(t)
        lines.append(f"- {base_title(b['title'])} ({b['key']}, {b['date'][:10]})")
        if len(lines) == limit:
            break
    text = ("I can't tell which bill you mean: the question doesn't name one, and many bills are new. Please give the bill's "
            "title or number")
    if lines:
        return text + ". The most recent bills in the index are:\n" + "\n".join(lines)
    return text + " (for example 078/2026)."


def resolve_question(store, question, has_history=False):
    """-> {"action": "answer", "question": str, "notes": [str]} | {"action": "reply", "text": str}
    has_history: the chat already has earlier turns, so "this bill" may refer to one of them (not asked back)."""
    if is_smalltalk(question):
        return {"action": "reply", "text": smalltalk_reply(question)}
    if not has_history and is_vague_bill_question(question):
        return {"action": "reply", "text": which_bill_reply(store), "clarify": True}
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
