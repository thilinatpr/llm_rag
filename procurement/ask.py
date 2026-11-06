"""Question -> evidence (guideline clauses + the manual sections that explain them) -> ONE cited answer -> citation check.

Labels: [PG2024 §2.9] = Procurement Guidelines 2024 clause 2.9; [PM2024 §2.9] = Procurement Manual 2024 section that explains
clause 2.9. The 2006 documents are searched only when the question is about the old regime (they apply only to procurement
actions started before 2025-01-01, and only up to the award of the contract).
"""
import re
import time
from dataclasses import dataclass, field

from .index import HIERARCHICAL
from .sources import ALL, CURRENT, DOCS, GUIDANCE, UNOFFICIAL

# "supplement N" is NOT an old-regime marker any more: NPC's Supplements 01-07 (2025-26) are current.
OLD = re.compile(r"\b2006\b|before\s+(?:1st\s+)?jan\w*\s+2025|before\s+2025|pre-?2025|\bold\s+(?:guidelines|manual|rules)"
                 r"|\bNPA\b|national\s+procurement\s+agency|transition|\bTEC\b|\bCAPC\b", re.I)
_IDS = "|".join(sorted((d["id"] for d in DOCS), key=len, reverse=True))
_CANON = {d["id"].lower(): d["id"] for d in DOCS}
TRANSLATED = {d["id"] for d in DOCS if d.get("note") == UNOFFICIAL}  # our own translations of Sinhala-only circulars (FRA, PFC0120)
NON_BINDING = {d["id"] for d in DOCS if d.get("note") == GUIDANCE}
NON_BINDING_NOTE = ("Note: {docs} is non-binding guidance from 2017, written before the 2024 Guidelines; "
                    "the contract's own conditions and PG2024/PM2024 prevail.")
TRANSLATION_NOTE = ("Note: {docs} {verb} an unofficial English translation of Sinhala Treasury circulars made for this tool; "
                    "the Sinhala text is authoritative.")  # citations are matched case-insensitively ("PFC0326i")
CITE = re.compile(rf"\b({_IDS})\b(?:\s*(?:§|Art(?:icle)?\.?)\s*(Definitions|156[A-Z]|\d{{1,2}}(?:\.\d{{1,2}}){{0,3}})"
                  rf"|(?:(?<=FR)|(?<=FRA))\s*(\d{{1,3}}[A-Z]?)\b)?", re.I)  # Financial Regulations: [FR 135], [FRA 108]

SYSTEM = """You answer questions from Sri Lankan public officers about government procurement rules, using ONLY the passages given.

Sources:
- PG2024 = Procurement Guidelines 2024 (National Procurement Commission, Gazette 2412/01, in force from 1 January 2025). Binding rules.
- PM2024 = Procurement Manual 2024: procedures, authority limits, thresholds, time frames and formats for applying the Guidelines.
  A manual section is labelled with the guideline clause it explains.
- CON = the Constitution, Chapter XIXB (Articles 156B-156H): the National Procurement Commission's composition, functions,
  powers, meetings, staff and the meaning of "Government institution". English text as amended up to 31 October 2022.
  Cite it as [CON Art. 156C].
- PG2006 / PM2006 = the old 2006 Guidelines and Manual (NPA). Repealed from 1 January 2025; they still govern only procurement
  actions that began before that date, up to the award of the contract. If you use them, say so explicitly.
- "Amended by Gazette 2489/10" notes are official amendments to PG2024: apply them.
- SUP01-SUP07 = NPC Supplements to the Procurement Manual 2024 (2025-2026). Each is labelled with the Manual section it adds to or
  replaces, e.g. [SUP05 §5.6.1]. Where a supplement and the Manual differ, the supplement is the current rule: say so and cite
  both. Notes on Manual passages ("Replaced by Supplement 02 ...") point to them.
- CG2007 = Guidelines for Selection and Employment of Consultants (August 2007), still applied to CONSULTING services, with
  CGS01 (Supplement 01 to the Consulting Services Manual, 12.12.2025) revising its thresholds and payments. PG2024/PM2024
  cover goods, works and non-consulting services; do not mix the two regimes.
- NPCC2024 = NPC Circular 01/2024 (the 2024 Guidelines in force from 1 January 2025). NPCC2023 = NPC Circular 01/2023
  (interim, historical). Treasury Public Finance Circulars (all issued with NPC concurrence where about procurement):
  PFC0326 = 03/2026 special relief measures (conflict, Ditwah cyclone; valid until 30.09.2026) and PFC0326i = its amendment
  03/2026 (i); PFC0126 = 01/2026 (procurement before budget allocation; no award without confirmed budget); PFC0426 = 04/2026
  (beneficial ownership disclosure, Ditwah procurements over Rs. 20 Mn); PFC0324 = 03/2024 (PFM Act in force; FR 1992 continue
  where not inconsistent). Cite circulars as [PFC0126].
- PFMA = Public Financial Management Act, No. 44 of 2024, cited [PFMA §67]. An Act of Parliament: it prevails over the
  Financial Regulations and circulars where they are inconsistent.
- FR = Financial Regulations of the Government (1992 edition), cited [FR 135]: delegation of financial authority (FR 135),
  approval, commitments, payments, advances, losses, stores. The procurement chapter (XIII) is not in this edition and the
  NPC Guidelines govern procurement.
- FRA = the amendments to the Financial Regulations by Public Finance Circulars 01/2020, 01/2024, 02/2024 and 01/2024 (I), cited
  [FRA 108] by FR number; PFC0120 = Part II of PF Circular 01/2020 with its amendments (telephone limits, service fees, urgent
  services, air tickets, insurance, ...), cited [PFC0120 §4] by Part II paragraph. Both are UNOFFICIAL English translations of
  Sinhala circulars: when you rely on them, say so in one short phrase ("unofficial translation of PF Circular 02/2024"). Where an
  FRA passage and the 1992 FR text differ, FRA is the current rule; give its date. Cite amended text as [FRA 108], never as
  [FR 108], and give the current amount, not one marked "before". If an FR passage you use says it was amended
  and the amendment is not shown, say the amount may be out of date.
- GPMCM = Ministry of Finance Guide to Project Management and Contract Management (Works), February 2017, cited [GPMCM §8.3]. NON-BINDING
  guidance on contract administration, written mainly for FIDIC/ICTAD contracts and before the 2024 Guidelines: when you rely on it, say
  it is guidance; it never overrides PG2024, PM2024 or the contract. NPC's own Contract Management Guidelines (PG2024 §1.4(c), §9.1) are
  not among these sources: if a question turns on them or on the contract's conditions, say so.
- LCC2026 = NPC Guidance Note on Life Cycle Cost / Total Cost of Ownership. ESM2026 = NPC Sector Specific Procurement Manual
  2026 for investor selection in electricity generation, storage, transmission and ancillary services (from 22.10.2026).

Abbreviations: {abbrev}.

Rules:
- Start with a direct answer in 1-2 sentences, then at most 5 short bullets with the conditions, limits or steps that matter.
- Cite every claim with the passage label in ASCII square brackets exactly as shown, e.g. [PG2024 §2.9] or [PM2024 §2.9]. Never cite
  a label that is not shown.
- Quote amounts, limits, time periods and committee names exactly as written. Do not convert or round numbers.
- Tables were extracted from PDFs and their columns may be misaligned. A unit in a table heading (e.g. "LKR Million") may
  apply only to the value ranges, not to the amounts. If you cannot tell which column or unit a figure belongs to, give the
  row as written and say the table should be checked on the cited page.
- If the passages do not answer the question, say so plainly and name the closest clause given. Never fill gaps from general
  knowledge or from other countries' rules.
- This explains the rules; it is not legal advice. Do not add disclaimers beyond one short line when the answer depends on facts
  the question does not give."""


from .index import ACRONYMS  # noqa: E402

SYSTEM = SYSTEM.replace("{abbrev}", "; ".join(f"{a} = {b}" for a, b in ACRONYMS.items()))


@dataclass
class Answer:
    question: str
    answer: str = ""
    evidence: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    docs: tuple = CURRENT
    seconds: float = 0.0
    standalone: str = ""  # what a follow-up was rewritten to; empty when the question was used as asked

    def sources(self):
        return [f"{e['label']} {e['heading']} (p. {e['page']})" for e in self.evidence]


def label(doc, ref):
    if not ref:
        return f"[{doc}]"
    if doc in ("FR", "FRA"):
        return f"[{doc} {ref}]"
    return f"[{doc} Art. {ref}]" if doc == "CON" else f"[{doc} §{ref}]"


def window(parts, hit, max_chars):
    """A long clause, cut to max_chars around the part that matched (a table at the end of a 4-page section must not be
    cut off). Parts are kept in reading order; gaps are marked [...]."""
    if sum(len(p["text"]) for p in parts) <= max_chars:
        return "\n".join(p["text"] for p in parts)
    i = next((n for n, p in enumerate(parts) if p["part"] == hit), 0)
    keep, size, lo, hi = {i}, len(parts[i]["text"]), i - 1, i + 1
    while lo >= 0 or hi < len(parts):
        for j in (hi, lo):  # grow forward first: the matched part usually introduces what follows
            if 0 <= j < len(parts) and j not in keep and size + len(parts[j]["text"]) <= max_chars:
                keep.add(j); size += len(parts[j]["text"])
        lo, hi = lo - 1, hi + 1
    out, prev = [], -1
    for j in sorted(keep):
        if j != prev + 1:
            out.append("[...]")
        out.append(parts[j]["text"]); prev = j
    if prev != len(parts) - 1:
        out.append("[...]")
    text = "\n".join(out)
    return text if len(text) <= max_chars + 200 else text[:max_chars] + " [...]"


# "clause 2.3", "Guidelines 4.2.1 and 4.2.2", "paragraph 3.4": a pointer to another clause of the same document ("Guideline(s)"
# in a Manual points at the Guidelines). A bare number such as "Rs. 2.5 million" has no keyword and is never followed.
XREF = re.compile(r"\b(guidelines?|(?:sub-?)?clauses?|paragraphs?|paras?\.?|sections?)\s+"
                  r"(\d{1,2}(?:\.\d{1,2}){1,3}(?:\s*(?:,|and|or|&)\s*\d{1,2}(?:\.\d{1,2}){1,3})*)", re.I)
MAX_XREF = 3  # extra clauses added through pointers, in all
MAX_GUIDE = 2  # non-binding passages per question: a live test filled every slot with them and the answer invented the rules
GUIDE_BINDING = (("PM2024", "9.1"), ("PG2024", "9.1"))  # contract management in the rules themselves


def xrefs(doc, text):
    """[(doc, ref)] that `text` points at, in reading order, without duplicates."""
    out = []
    for kw, refs in XREF.findall(text):
        target = "PG" + doc[2:] if kw.lower().startswith("guideline") and doc.startswith("PM") else doc
        out += [(target, r) for r in re.findall(r"\d{1,2}(?:\.\d{1,2}){1,3}", refs)]
    return list(dict.fromkeys(out))


def gather(index, question, k=8, docs=CURRENT, max_units=10, max_chars=3500, first=()):
    """Top-k chunks -> whole clauses/sections, each guideline clause paired with its manual section(s) and vice versa."""
    units, seen = [], set()

    def add(doc, ref, heading=None, hit=None, cap=max_units, max_chars=max_chars):
        by_heading = doc.startswith(("PM", "SUP")) or doc == "FRA"  # sections that share a number are separate units
        key = (doc, ref, heading if by_heading else None)
        if key in seen or len(units) >= cap:
            return
        parts = index.clause(doc, ref, heading if by_heading else None)
        if not parts:
            return
        seen.add(key)
        note = next((p["note"] for p in parts if p.get("note")), None)
        units.append(dict(doc=doc, ref=ref, heading=parts[0]["heading"], chapter=parts[0]["chapter"],
                          page=parts[0]["page"], label=label(doc, ref), text=window(parts, hit, max_chars), note=note,
                          chain=index.chain(doc, ref)))

    guide_shown = 0
    for doc, ref in first:  # always shown, e.g. PG2024 §1.4 (the transition rule) for questions about the old regime
        add(doc, ref)
    for c in index.search(question, k=k, docs=docs):
        if c["doc"] in NON_BINDING:  # guidance never crowds out the rules, and always comes with the binding clause on the subject
            if guide_shown >= MAX_GUIDE:
                continue
            guide_shown += 1
            for doc, ref in GUIDE_BINDING:
                add(doc, ref)
        add(c["doc"], c["ref"], c["heading"], hit=c["part"])
        for sup in index.supplements_for(c["doc"], c["ref"]):  # the supplement that changes this section comes right after it
            add(sup["doc"], sup["ref"], sup["heading"], cap=max_units + 3)
        if c["doc"] not in ("PG2024", "PM2024", "PG2006", "PM2006"):
            continue
        pair = "PM" + c["doc"][2:] if c["doc"].startswith("PG") else "PG" + c["doc"][2:]
        if c["doc"].startswith("PG"):
            for m in index.manual_for(c["ref"], pair)[:1]:
                add(pair, m["ref"], m["heading"])
        else:
            add(pair, c["ref"])
    # a clause that points at another clause ("as set out in 1.2 below") brings it along, one hop only, and not its own parent
    # (that is shown as the heading chain). The added clause says who pointed at it.
    added = 0
    for u in list(units):
        if u["doc"] not in HIERARCHICAL:
            continue
        for tdoc, tref in xrefs(u["doc"], u["text"]):
            if added >= MAX_XREF:
                break
            if (tdoc, tref) == (u["doc"], u["ref"]) or (u["ref"] or "").startswith(tref + "."):
                continue
            n = len(units)
            add(tdoc, tref, cap=max_units + 3 + MAX_XREF, max_chars=2000)
            if len(units) > n:
                units[-1]["note"] = " ".join(filter(None, [units[-1]["note"], f"Referred to by {u['label']}."]))
                added += 1
    # a passage that points at a Financial Regulation ("attention should be paid to Financial Regulation (FR) 136 (3)") brings
    # that regulation along, so the answer can quote it instead of citing a passage it never saw
    # (and its amendment: an FR note or an FRA passage names "FR 108", whose current text is FRA 108)
    for u in list(units):
        for n in dict.fromkeys(FR_REF.findall(u["text"] + " " + (u["note"] or ""))):
            if (u["doc"], u["ref"]) != ("FR", n):
                add("FR", n, cap=max_units + 3)
            for sup in index.supplements_for("FR", n):  # by heading: FRA 113(3) and 113(6) are separate units
                add(sup["doc"], sup["ref"], sup["heading"], cap=max_units + 3)
    amended = {u["ref"] for u in units if u["doc"] == "FRA"}
    for u in units:  # both texts shown: say plainly which one is in force
        if u["doc"] == "FR" and u["ref"] in amended:
            u["note"] = (f"SUPERSEDED IN PART: the current text of this regulation is [FRA {u['ref']}] (shown here). Where the two "
                         f"differ, the amounts and rules of [FRA {u['ref']}] apply; cite [FRA {u['ref']}] for them.")
    return units


FR_REF = re.compile(r"\b(?:Financial\s+Regulations?\s*(?:\(FR\)\s*)?|F\.\s?R\.?\s?|FR\s)(\d{1,3})\b")


def render(units):
    out = []
    for u in units:
        head = f"{u['label']} {u['heading']} ({u['chapter']}; p. {u['page']})" if u["chapter"] else f"{u['label']} {u['heading']}"
        if u.get("chain"):  # where the clause sits: its text often only makes sense under its parents
            head += "\nUnder: " + " > ".join(f"{r} {h}" for r, h in u["chain"])
        if u["doc"] == "FR" and u["note"]:  # "amended, see FRA ..." must be read before 3,000 characters of 1992 text
            out.append(head + "\n" + u["note"] + "\n" + u["text"])
        else:
            out.append(head + "\n" + u["text"] + (f"\n{u['note']}" if u["note"] else ""))
    return "\n\n---\n\n".join(out)


def verify(answer, units):
    """Citations that point at no passage shown to the model. A citation may name a sub-clause of a shown clause
    (2.7 shown, 2.7.1 cited) only if that number occurs in the clause text, or a parent of a shown clause."""
    bad = []
    for doc, ref, fr in CITE.findall(answer):
        doc, ref = _CANON.get(doc.lower(), doc), ref or fr
        ok = any(u["doc"] == doc and (not ref or not u["ref"] or u["ref"] == ref or u["ref"].startswith(ref + ".")
                                      or (ref.startswith(u["ref"] + ".") and ref in u["text"])) for u in units)
        if not ok:
            bad.append(label(doc, ref).strip("[]"))
    return list(dict.fromkeys(bad))


LEAK = re.compile(r"<\|(?:end|start|channel|message|return)\|>|\bassistant\s*(?:analysis|final)\b", re.I)
REPEAT = re.compile(r"(\b\S{1,12}\b)(?:[\s\-\u2011]+\1\b){7,}|([\-\u2011\u2010~*_=.])\2{30,}")


def broken(text):
    """Why a reply is unusable (gpt-oss sometimes loops "man man man ..." or leaks its channel tags), or None."""
    if LEAK.search(text):
        return "leaked reasoning/channel tags"
    if REPEAT.search(text):
        return "degenerate repetition"
    return None


CONDENSE = """Rewrite the follow-up as one standalone question that can be understood without the conversation. Keep the roles,
amounts, document names and clause numbers the user is talking about. Do not answer it and add no facts. If it is already
standalone, return it unchanged. If it only reacts to the last answer ("that is not clear"), ask again what the last question asked,
and say what the user found unsettled. Return ONLY the question.

CONVERSATION:
{history}

FOLLOW-UP: {question}"""
MAX_HISTORY = 2  # turns
MAX_HISTORY_QUESTION, MAX_HISTORY_ANSWER = 1000, 1500


def clean_history(history):
    """The last turns of the conversation as sent by the page, or [] for anything malformed. The browser owns this data, so it is
    only used to rewrite that user's own question; it is never shown to the model as evidence and never cited."""
    if not isinstance(history, list):
        return []
    out = []
    for t in history[-MAX_HISTORY:]:
        if isinstance(t, dict) and isinstance(t.get("question"), str) and isinstance(t.get("answer"), str) and t["question"].strip():
            out.append({"question": t["question"].strip()[:MAX_HISTORY_QUESTION], "answer": t["answer"].strip()[:MAX_HISTORY_ANSWER]})
    return out


def condense(llm, question, history, effort="low"):
    """A follow-up as a standalone question, one model call. Returns (question, warning). On failure it searches on the last question
    plus the follow-up: "It's not conclusive" alone retrieved appeal clauses and got an off-topic answer."""
    fallback = f"{history[-1]['question']} {question}"
    from billrag.pipeline import strip_reasoning
    hist = "\n".join(f"Q: {t['question']}\nA: {t['answer']}" for t in history)
    try:
        reply = llm.chat("You rewrite questions.", [{"role": "user", "content": CONDENSE.format(history=hist, question=question)}],
                         max_tokens=600, effort=effort)
    except Exception as e:
        return fallback, f"follow-up not rewritten: {type(e).__name__}"
    text, leaked = strip_reasoning(reply.text)
    text = " ".join(text.split()).strip('"')
    if leaked or reply.truncated or broken(text) or not text or len(text) > MAX_HISTORY_QUESTION:
        return fallback, "follow-up not rewritten: unusable model output"
    return text, None


def ask(index, llm, question, k=8, effort="low", docs=None, history=None):
    from billrag.pipeline import strip_reasoning
    t = time.time()
    res = Answer(question)
    history = clean_history(history)
    query = question
    if history:
        query, warn = condense(llm, question, history, effort)
        if warn:
            res.warnings.append(warn)
        elif query != question:
            res.standalone = query
    old = bool(OLD.search(query))
    res.docs = docs = docs or (ALL if old else CURRENT)
    res.evidence = gather(index, query, k=k, docs=docs, first=[("PG2024", "1.4")] if old else ())
    if not res.evidence:
        res.answer = "I could not find anything about this in the Procurement Guidelines or Manual."
        return res
    user = f"Question: {query}\n\nPassages:\n\n{render(res.evidence)}"
    for attempt in range(2):
        reply = llm.chat(SYSTEM, [{"role": "user", "content": user}], max_tokens=2500, effort=effort)
        answer, leaked = strip_reasoning(reply.text)
        why = ("reasoning instead of an answer" if leaked else broken(answer)
               or ("cut off at the token limit" if reply.truncated else None))
        if not why:
            break
        res.warnings.append(f"attempt {attempt + 1} rejected: {why}")
        user += "\n\nWrite only the final answer: plain sentences and bullets, no reasoning, no repetition."
    else:
        res.answer = "Sorry, the model did not return a usable answer. Please ask again."
        res.seconds = round(time.time() - t, 1)
        return res
    res.answer = answer.strip().replace("\u3010", "[").replace("\u3011", "]")
    if not CITE.search(res.answer):
        res.warnings.append("answer has no citations")
    used = sorted({_CANON.get(d.lower(), d) for d, _, _ in CITE.findall(res.answer)} & TRANSLATED)
    if used and "unofficial" not in res.answer.lower():  # the model forgets to say so; this must never depend on it
        res.answer += "\n\n" + TRANSLATION_NOTE.format(docs=" and ".join(used), verb="are" if len(used) > 1 else "is")
    guide = sorted({_CANON.get(d.lower(), d) for d, _, _ in CITE.findall(res.answer)} & NON_BINDING)
    if guide:  # as with translations, this must never depend on the model remembering
        res.answer += "\n\n" + NON_BINDING_NOTE.format(docs=" and ".join(guide))
    bad = verify(res.answer, res.evidence)
    if bad:
        res.warnings.append("unsupported citations: " + ", ".join(bad))
    res.seconds = round(time.time() - t, 1)
    return res
