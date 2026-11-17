"""Question -> evidence (bill rows + speech passages) -> ONE cited answer -> checks.

Labels (what an answer may cite, and what `verify` accepts):
  [Bill s9]            section 9 of the Appropriation Bill 2027 (080/2026)
  [Bill Head 118]      First Schedule, Head 118 (all its programmes)
  [Bill Charged 24]    Second Schedule, Head 24 (expenditure charged on the Consolidated Fund)
  [Bill Ministry: X]   a ministry's total in the First Schedule
  [Bill Totals]        key figures of the Bill
  [Speech Cover]       date and speaker of the Budget Speech
  [Speech §17.2]       Budget Speech 2027, chapter 17.2 (printed page in the passage header)
  [Speech Annex II #12] Annexure II, new expenditure proposal no. 12
  [Speech Annex V]     another annexure
Amounts: the Bill prints rupees; the Speech and Annexure II print Rs. million. Every figure in an answer must occur in the
evidence (as printed, or converted between rupees / million / billion): `verify_numbers`. Sums the model makes itself are
therefore flagged, not trusted.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from . import index as ix

BILL = "Bill 080/2026"
SPEECH = "Speech 2027"

SYSTEM = """You answer questions about Sri Lanka's 2027 Budget, using ONLY the passages given.

Sources:
- Bill = the Appropriation Bill for the financial year 2027 (Bill 080/2026, presented to Parliament on 7 October 2026; Gazette text 069/2026 is the same text). It is a BILL: Parliament can change it at committee stage. Amounts in the Bill are in rupees (Rs.). The First Schedule (Heads and Programmes) is the money this Bill votes: recurrent and capital expenditure. The Second Schedule is expenditure charged on the Consolidated Fund by the Constitution and other laws. The Third Schedule (limits of advance accounts) is not in your passages.
- Speech = the Budget Speech 2027, delivered by the Minister of Finance, Planning and Economic Development on 12 November 2026. It announces proposals and gives amounts in Rs. million (read each passage's own unit). Annexure II lists the new expenditure proposals with a recurrent or capital amount in Rs. million.
Rules:
- Cite every fact in square brackets with the label shown above the passage, e.g. [Bill Head 118], [Speech §17.2], [Speech Annex II #12]. Copy the label exactly, one label per bracket. Cite only labels that were shown, and never to support that something is NOT in the sources (just say it is not in the passages).
- Quote amounts exactly as printed, with their unit. You may restate rupees as billion or million (Rs. 4,768,000,000 = Rs. 4,768 million). Do NOT add up or compare figures yourself: use a total that a passage gives (ministry totals, key figures). If the answer needs a sum that no passage gives, give the parts, say that no total is printed, and do not calculate one.
- Say which source a figure comes from. The Speech and the Bill are different documents: a proposal in the Speech is not money voted until it is in the Bill; if they differ or one is silent, say so plainly.
- Treat the Bill's figures as "as presented", not final.
- If the passages do not answer the question, say so in one sentence. Never guess, never use outside knowledge, and do not discuss other years' budgets.
- Be brief: 1-3 sentences, then at most 4 short bullets. Answer in the language of the question's English rendering; plain words, no headings."""

BILL_NOTE = ("Note: the Appropriation Bill 2027 is a Bill as presented to Parliament on 7 October 2026; Parliament may change it at committee stage, "
             "and this service does not yet reflect any change. citizenlk.com is not the Treasury.")
SPEECH_NOTE = ("Note: the Budget Speech announces proposals; money is authorised only by the Appropriation Act. Check the figures against the Treasury's documents.")


@dataclass
class Answer:
    question: str
    answer: str = ""
    evidence: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    seconds: float = 0.0

    def sources(self):
        return [u["label"] + " " + u["where"] for u in self.evidence]


def unit_from(row) -> dict:
    kind, ref, doc = row["kind"], row["ref"], row["doc"]
    if doc == BILL:
        if kind == "section":
            label, where = f"[Bill s{ref[1:]}]", f"Appropriation Bill 2027, section {ref[1:]}"
        elif kind == "estimate":
            label, where = f"[Bill Head {ref.split()[1]}]", f"Appropriation Bill 2027, First Schedule, p.{row['page']} of the Bill's schedule pages"
        elif kind == "charged":
            label, where = f"[Bill Charged {ref.split()[1]}]", "Appropriation Bill 2027, Second Schedule"
        elif kind == "group":
            label, where = f"[Bill Ministry: {ref[len('group '):]}]", "Appropriation Bill 2027, First Schedule ministry total"
        else:
            label, where = "[Bill Totals]", "Appropriation Bill 2027, key figures"
    elif kind == "facts":
        label, where = "[Speech Cover]", "Budget Speech 2027, cover page"
    elif kind == "speech":
        label, where = f"[Speech §{ref}]", f"Budget Speech 2027, p.{row['page']}"
    else:
        m = re.match(r"annex II #(\d+)", ref)
        if m:
            label, where = f"[Speech Annex II #{m.group(1)}]", f"Budget Speech 2027, Annexure II, p.{row['page']}"
        else:
            label, where = f"[Speech Annex {ref.split()[1].split('.')[0]}]", "Budget Speech 2027, Annexure"
    return {"label": label, "where": where, "heading": row["heading"], "text": row["text"], "kind": kind, "doc": doc, "ref": ref}


def head_numbers(q: str) -> list[int]:
    return [int(n) for n in re.findall(r"\bheads?\s*(?:no\.?\s*)?(\d{1,3})\b", q, re.I)]


def gather(db, question: str, k: int = 10) -> list[dict]:
    """Facts the question points at (head numbers, 'total'), then the best-matching passages."""
    units, seen = [], set()

    def add(row):
        if row["id"] in seen:
            return
        seen.add(row["id"])
        units.append(unit_from(row))

    for n in head_numbers(question):
        for r in db.execute("SELECT * FROM chunks WHERE doc=? AND kind IN ('estimate','charged') AND ref IN (?,?)", (BILL, f"head {n}", f"charged {n}")):
            add(r)
    if re.search(r"\b(total|overall|whole|entire|how much is the budget|size of the budget|borrow)", question, re.I):
        for r in db.execute("SELECT * FROM chunks WHERE doc=? AND kind='facts'", (BILL,)):
            add(r)
    # separate lanes: the Bill's number tables, the Bill's clauses, the Speech: each is always represented
    for r in ix.search(db, question, 4, ("group", "estimate", "charged", "facts"), doc=BILL):
        add(r)
    for r in ix.search(db, question, 2, ("section",), doc=BILL):
        add(r)
    for r in ix.search(db, question, k - 2, ("speech", "annex", "facts"), doc=SPEECH):
        add(r)
    return units[: k + 6]


def render(units) -> str:
    out = []
    for u in units:
        out.append(f"{u['label']} {u['heading']}\n{u['text']}")
    return "\n\n".join(out)


# ----------------------------------------------------------------- checks
CITE = re.compile(r"\[(Bill|Speech)\s+([^\]]+)\]")
_NUM = re.compile(r"(?<![\w.])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.,])\d{3,}(?:\.\d+)?(?![\w,])")


def _canon(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


def verify(answer: str, units) -> list[str]:
    """Citations that name no passage shown to the model."""
    shown = {_canon(u["label"].strip("[]")) for u in units}
    bad = []
    cites = []
    for who, ref in CITE.findall(answer):
        cur = who
        for i, part in enumerate(p.strip() for p in ref.split(";")):  # "[Bill Head 1; Speech §2]" = two citations
            m = re.match(r"(Bill|Speech)\s+(.*)", part)
            if i and m:
                cur, part = m.group(1), m.group(2)
            cites.append((cur, part))
    for who, ref in cites:
        c = _canon(f"{who} {ref}")
        # "[Speech §17.2, p.40]" and "[Speech §17.2 p.40]" both mean §17.2
        c = re.sub(r"[, ]+p\.?\s*\d+$", "", c)
        if c not in shown and not any(s.startswith(c + " ") for s in shown):
            bad.append(f"{who} {ref}")
    return list(dict.fromkeys(bad))


def evidence_numbers(units) -> set[str]:
    nums = set()
    for u in units:
        for m in _NUM.findall(u["text"]):
            v = m.replace(",", "")
            nums.add(v)
            try:
                f = float(v)
            except ValueError:
                continue
            for div, _ in ((1e3, "thousand"), (1e6, "million"), (1e9, "billion"), (1e12, "trillion")):
                x = f / div
                if x >= 0.1:
                    nums.add(("%f" % x).rstrip("0").rstrip("."))
                    nums.add(str(round(x, 1)).rstrip("0").rstrip(".") if x != int(x) else str(int(x)))
    return nums


def verify_numbers(answer: str, units) -> list[str]:
    """Figures in the answer that occur in no passage (not as printed, not converted between rupees / million / billion).
    Citation labels and years are not figures. A figure the model calculated shows up here."""
    body = CITE.sub(" ", answer)
    have = evidence_numbers(units)
    bad = []
    for m in _NUM.findall(body):
        v = m.replace(",", "")
        if re.fullmatch(r"(19|20)\d\d", v):
            continue
        if v not in have and v.rstrip("0").rstrip(".") not in have:
            bad.append(m)
    return list(dict.fromkeys(bad))


def broken(text: str) -> str | None:
    t = text.strip()
    if len(t) < 15:
        return "empty or too short"
    if re.search(r"(.)\1{25,}", t):
        return "repetition"
    return None


def ask(db, llm, question: str, k: int = 10, effort: str = "low") -> Answer:
    from billrag.pipeline import strip_reasoning
    t = time.time()
    res = Answer(question)
    res.evidence = gather(db, question, k)
    if not res.evidence:
        res.answer = "I could not find anything about this in the 2027 Appropriation Bill or the Budget Speech."
        return res
    user = f"Question: {question}\n\nPassages:\n\n{render(res.evidence)}"
    for attempt in range(2):
        reply = llm.chat(SYSTEM, [{"role": "user", "content": user}], max_tokens=5000, effort=effort if attempt == 0 else "off")
        answer, leaked = strip_reasoning(reply.text)
        why = "reasoning instead of an answer" if leaked else broken(answer) or ("cut off at the token limit" if reply.truncated else None)
        if not why:
            break
        res.warnings.append(f"attempt {attempt + 1} rejected: {why}")
        user += "\n\nWrite only the final answer: plain sentences and bullets, no reasoning, no repetition."
    else:
        res.answer = "Sorry, the model did not return a usable answer. Please ask again."
        res.seconds = round(time.time() - t, 1)
        return res
    res.answer = answer.strip().replace("【", "[").replace("】", "]")
    cited = {w for w, _ in CITE.findall(res.answer)}
    if not cited and re.search(r"\d{1,3}(?:,\d{3})+|\bRs\.?\s*\d", res.answer):  # a refusal needs none; a figure does
        res.warnings.append("answer has figures but no citations")
    if "Bill" in cited:  # fixed notes: never left to the model
        res.answer += "\n\n" + BILL_NOTE
    if "Speech" in cited:
        res.answer += "\n\n" + SPEECH_NOTE
    bad = verify(res.answer, res.evidence)
    if bad:
        res.warnings.append("unsupported citations: " + ", ".join(bad))
    nums = verify_numbers(answer, res.evidence)
    if nums:
        res.warnings.append("figures not found in the passages (calculated or wrong): " + ", ".join(nums))
    res.seconds = round(time.time() - t, 1)
    return res
