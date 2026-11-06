"""Question -> evidence (guideline clauses + the manual sections that explain them) -> ONE cited answer -> citation check.

Labels: [PG2024 §2.9] = Procurement Guidelines 2024 clause 2.9; [PM2024 §2.9] = Procurement Manual 2024 section that explains
clause 2.9. The 2006 documents are searched only when the question is about the old regime (they apply only to procurement
actions started before 2025-01-01, and only up to the award of the contract).
"""
import re
import time
from dataclasses import dataclass, field

CURRENT, ALL = ("PG2024", "PM2024", "CON"), ("PG2024", "PM2024", "CON", "PG2006", "PM2006")
OLD = re.compile(r"\b2006\b|before\s+(?:1st\s+)?jan\w*\s+2025|before\s+2025|pre-?2025|\bold\s+(?:guidelines|manual|rules)"
                 r"|\bNPA\b|national\s+procurement\s+agency|transition|supplement\s*-?\s*\d|\bTEC\b|\bCAPC\b", re.I)
CITE = re.compile(r"\b(P[GM](?:2024|2006)|CON)\s*(?:§|Art(?:icle)?\.?)\s*(Definitions|156[A-Z]|\d{1,2}(?:\.\d{1,2}){0,3})", re.I)

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

    def sources(self):
        return [f"{e['label']} {e['heading']} (p. {e['page']})" for e in self.evidence]


def label(doc, ref):
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


def gather(index, question, k=8, docs=CURRENT, max_units=8, max_chars=3500, first=()):
    """Top-k chunks -> whole clauses/sections, each guideline clause paired with its manual section(s) and vice versa."""
    units, seen = [], set()

    def add(doc, ref, heading=None, hit=None):
        key = (doc, ref, heading if doc.startswith("PM") else None)
        if key in seen or len(units) >= max_units:
            return
        parts = index.clause(doc, ref, heading if doc.startswith("PM") else None)
        if not parts:
            return
        seen.add(key)
        note = next((p["note"] for p in parts if p.get("note")), None)
        units.append(dict(doc=doc, ref=ref, heading=parts[0]["heading"], chapter=parts[0]["chapter"],
                          page=parts[0]["page"], label=label(doc, ref), text=window(parts, hit, max_chars), note=note))

    for doc, ref in first:  # always shown, e.g. PG2024 §1.4 (the transition rule) for questions about the old regime
        add(doc, ref)
    for c in index.search(question, k=k, docs=docs):
        add(c["doc"], c["ref"], c["heading"], hit=c["part"])
        if c["doc"] == "CON":
            continue
        pair = "PM" + c["doc"][2:] if c["doc"].startswith("PG") else "PG" + c["doc"][2:]
        if c["doc"].startswith("PG"):
            for m in index.manual_for(c["ref"], pair)[:1]:
                add(pair, m["ref"], m["heading"])
        else:
            add(pair, c["ref"])
    return units


def render(units):
    out = []
    for u in units:
        head = f"{u['label']} {u['heading']} ({u['chapter']}; p. {u['page']})" if u["chapter"] else f"{u['label']} {u['heading']}"
        out.append(head + "\n" + u["text"] + (f"\n{u['note']}" if u["note"] else ""))
    return "\n\n---\n\n".join(out)


def verify(answer, units):
    """Citations that point at no passage shown to the model. A citation may name a sub-clause of a shown clause
    (2.7 shown, 2.7.1 cited) only if that number occurs in the clause text, or a parent of a shown clause."""
    bad = []
    for doc, ref in CITE.findall(answer):
        doc = doc.upper()
        ok = any(u["doc"] == doc and (u["ref"] == ref or u["ref"].startswith(ref + ".")
                                      or (ref.startswith(u["ref"] + ".") and ref in u["text"])) for u in units)
        if not ok:
            bad.append(f"{doc} Art. {ref}" if doc == "CON" else f"{doc} §{ref}")
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


def ask(index, llm, question, k=8, effort="low", docs=None):
    from billrag.pipeline import strip_reasoning
    t = time.time()
    old = bool(OLD.search(question))
    docs = docs or (ALL if old else CURRENT)
    res = Answer(question, docs=docs)
    res.evidence = gather(index, question, k=k, docs=docs, first=[("PG2024", "1.4")] if old else ())
    if not res.evidence:
        res.answer = "I could not find anything about this in the Procurement Guidelines or Manual."
        return res
    user = f"Question: {question}\n\nPassages:\n\n{render(res.evidence)}"
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
    bad = verify(res.answer, res.evidence)
    if bad:
        res.warnings.append("unsupported citations: " + ", ".join(bad))
    res.seconds = round(time.time() - t, 1)
    return res
