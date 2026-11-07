"""Two-step RAG pipeline: retrieve -> AI evaluation of the evidence -> AI summary (answer with citations).

1. Retrieve   hybrid search (BM25 + dense + cross-encoder) for clauses, plus bill-level candidates. No LLM.
2. Evaluate   ONE LLM call grades every candidate 0-3 and says whether the kept evidence is enough. If not, it proposes
              better queries and we do (at most) one more retrieval round.
3. Summarise  ONE LLM call writes the answer from the full text of the kept clauses, citing [bill §section].
4. Verify     deterministic check that every citation in the answer points at evidence we actually showed the model.

Typical cost: 2 LLM calls (3 with a follow-up round, +1 to condense a follow-up question in a chat).
"""
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field

from . import acts, calltrace, glossary as gloss, meta, parse_si, resolve, titles as title_index, translate
from .store import ACT_REF, normalize_ref

CLAUSE_KINDS = ("section", "schedule")
CITE = re.compile(r"[\[【](Act \d{1,3}/\d{4}|\d{3}/\d{4})(?:\s*§\s*(\d+))?")  # also the full-width brackets some models emit
REASONING = re.compile(r"^\s*analysis\s*[:\uff1a]?", re.I)
FINAL = re.compile(r"assistant\s*final\s*[:\uff1a]?\s*|\bfinal(?:\s+answer)?\s*[:\uff1a]\s*", re.I)
RETRY_NOTE = "\n\nReply with ONLY the final answer, in the format requested. Do not include any analysis or reasoning text."
UNUSABLE = "the model returned reasoning or unusable text instead of an answer (twice)"  # no answer was given: see web.unanswered
LEAK_FALLBACK = ("I could not produce a reliable written answer this time. The sources below are the passages I found; "
                 "please try asking again.")


def degenerate(txt):
    """Text that is not a written answer at all (e.g. a long run of '!!!!'): too few letters to be sentences."""
    txt = translate.CITE.sub("", txt).strip()  # citation labels are not prose either way
    letters = sum(c.isalpha() or unicodedata.category(c) in ("Mn", "Mc") for c in txt)  # Sinhala vowel signs are marks
    return letters == 0 or (len(txt) >= 40 and letters < 0.4 * len(txt))


def strip_reasoning(txt):
    """-> (text, leaked). gpt-oss sometimes returns its reasoning ("analysis: ...") instead of, or before, the answer."""
    if not REASONING.match(txt):
        return txt, False
    m = FINAL.search(txt)
    if m and txt[m.end():].strip():
        return txt[m.end():].strip(), False
    return txt, True


# "this new bill", "the proposed Act", "this law": the user means one bill but does not name it (seen live: the answer then
# leaned on a lower-scored lookalike bill). Only checked when the question has no NNN/YYYY.
_ADJ = r"(?:new|proposed|latest|recent|draft|upcoming)\s+"
UNNAMED = re.compile(rf"\b(?:(?:this|that)\s+(?:{_ADJ}){{0,2}}|the\s+(?:{_ADJ}){{1,2}})(?:bill|act|law|draft)\b"
                     r"(?!\s+(?:no\.?|number)\b)", re.I)
FOCUS_NOTE = ("\n\nNOTE: the question refers to a bill without naming it. The best-matching evidence is from {title} ({keys}): "
              "answer about THAT bill, name it in your first sentence, and lead with its clauses. Mention another bill only if "
              "it adds something the question asks for, and then say clearly that it is a different bill.")
# "key provisions of Act No. 6 of 2026", "summarise bill 060/2026", "what does it do": no single clause matches such a question, so
# ranked clauses alone leave the grader saying "no overview" and the answer hedges. When one instrument is named, its own overview
# (auto summary) is always part of the evidence.
OVERVIEW_Q = re.compile(r"\b(?:key|main|major|important|principal)\s+(?:provisions?|points?|features?|clauses?|changes?)\b|"
                        r"\b(?:summari[sz]e|summary|overview|outline|gist|explain|describe|what\s+(?:does|is)\b.{0,40}\b(?:do|about))\b|"
                        r"\btell\s+me\s+about\b", re.I)
_TITLE_STOP = {"act", "bill", "amendment", "the", "of", "and", "for", "to", "in", "no", "a", "gs", "sri", "lanka"}
LEAK = re.compile(r"<\|(?:call|start|end|channel|message|return|constrain)\|>|\bto=functions\b")

EVAL_PROMPT = """You are a strict evidence grader for a question-answering system over Sri Lankan bills.

QUESTION: {question}

Below are numbered candidate passages. Score each one for relevance to the question, judging ONLY from its text:
3 = directly answers it or states the key fact; 2 = useful supporting context; 1 = tangential; 0 = irrelevant.
Passages marked [BILL-LEVEL] describe a whole bill (title, long title, auto-generated summary): score them 3 only when the
question asks which bill/law covers something or what a bill is for.
Questions often paraphrase a bill's name, so do NOT down-score a passage just because the bill title differs slightly from the
wording in the question. But many bills share near-identical boilerplate clauses: when several passages say almost the same
thing, score highest the one from the bill whose title best matches the name used in the question.
Then decide whether the passages scored 2 or 3 are ENOUGH to answer the question completely and accurately. If not,
say what is missing and give up to 2 better search queries (different wording; name the likely Act, body or topic).

Return ONLY JSON: {{"scores": [{{"i": 0, "s": 3}}, ...], "enough": true, "missing": "", "queries": []}}

CANDIDATES:
{candidates}"""

ANSWER_SYSTEM = """You answer questions about bills of the Parliament of Sri Lanka using ONLY the evidence passages you are given.

Rules:
- Cite every substantive claim with the passage label in plain ASCII square brackets exactly as shown, e.g. [077/2026 §12]. Never cite
  anything that is not in the evidence. Never invent clauses, numbers or section references.
- Quote short exact phrases for figures, time limits, powers, offences and penalties; distinguish what the text says from
  your interpretation.
- The question may paraphrase a bill's name. Prefer evidence from the bill whose title best matches it; if you rely on a bill
  whose title differs from the name in the question, say so explicitly and name it, because many bills share near-identical
  boilerplate clauses.
- If the evidence is incomplete or does not answer the question, say so plainly and say what is missing. Do not guess.
- Evidence headers say "Gazette text" (the bill as published in the Gazette before presentation), "Bill" (the version presented
  to Parliament) or "certified" (an Act: the law as passed). If versions appear and differ, say so. Never write the code "GS" in an
  answer: say "Gazette text".
- A STATUS line under a passage says what is known about it: follow it, in your own words (never write the word STATUS; cite the
  passage as usual). Never present a bill as law. Say whether what you rely on
  is an Act or a bill, and if a bill became an Act, answer from the Act and mention a difference that bears on the question. If a
  STATUS says a clause's wording is not in the Act, say it was proposed but not enacted in those words. An amending Act lists changes to an earlier Act, whose
  full text is usually not in the evidence: say what the amendment does, not what the earlier Act now reads.
- Users mostly care about recently published bills: when several bills match and the question gives no year, lead with the
  newest and state the dates. Summaries marked auto-generated are hints only: rely on clause text for details.
- Format and length: BE BRIEF. Start with the direct answer in 1-2 sentences. Then at most 3 one-line bullets, each with its
  citation, only for details the question needs (a time limit, a penalty, a power). Aim for under 120 words in total (up to
  180 for a "summarise" request, at most 6 bullets). Do not restate the question, list every consequence or definition, or
  repeat a point. Add a one-line "Caveats" only if something is uncertain or missing. You explain; you do not give legal advice."""

LOCAL_NOTE = """

LANGUAGE: the user asked in {lang}. Write the WHOLE answer in {lang} (same rules, same brevity, same citation labels in ASCII
square brackets exactly as shown, e.g. [NNN/YYYY §N]; citation labels and bill numbers stay as they are).
For sections marked OFFICIAL {LANG} TEXT you are given the bill's own {lang} text, published with the bill (the {lang} text is
authoritative): take legal terms, names and any quotation from it word for word; do not re-translate the English for those
parts. For offences, penalties, time limits, powers and definitions, quote the official {lang} phrase exactly, in quotation
marks, copying its spelling character by character. Use the English text only to understand the clause. Bill titles may stay
in English. Write correct, natural {lang}, using only {lang} letters, digits, punctuation and plain English (ASCII) for names:
never characters of any other script. Use quotation marks ONLY for words copied from the official {lang} text."""

CONDENSE_PROMPT = """Rewrite the follow-up question as a standalone question that can be understood without the conversation.
Keep bill names, numbers and years. If it is already standalone, return it unchanged. Return ONLY the question.

CONVERSATION:
{history}

FOLLOW-UP: {question}"""


@dataclass
class Evidence:
    chunk: dict
    score: int = 0

    @property
    def key(self):
        return self.chunk["bill_key"]

    @property
    def section(self):
        return self.chunk.get("section_no")


KIND_LABEL = {"GS": "Gazette text", "Bill": "Bill", "Act": "certified"}  # what users (and the answering model) see instead of the internal kind codes


def version_tag(c):
    """'Title (Gazette text, 2026-10-16)' for a chunk: the title without its internal ' - GS' / ' - Bill' suffix, and the version in
    plain words. 'GS' is our own code and means nothing to readers (a live user asked "what is GS"); kinds without a plain name
    (other documents) show only the date."""
    title = re.sub(r"\s*-\s*(Bill|GS)\s*$", "", c["title"]).strip()
    label = KIND_LABEL.get(c.get("bill_kind"), "")
    return f"{title} ({label + ', ' if label else ''}{c['date'][:10]})"


_HEAD = re.compile(r"\u2014 (.*?)(?: - (?:GS|Bill))? \((GS|Bill|Act|Other), (\d{4}-\d\d-\d\d)\)")


def shown_text(c):
    """The chunk text as the answering model reads it. A bill-level (overview) chunk starts with a stored header like
    'Bill 084/2026 \u2014 The X - GS (GS, 2026-10-16).'; its internal codes get the same plain words as version_tag. Stored text
    (and so the vectors) is not changed. Clause text is returned as is."""
    t = c["text"]
    if c.get("kind") != "overview":
        return t
    return _HEAD.sub(lambda m: f"\u2014 {m.group(1)} ({KIND_LABEL[m.group(2)] + ', ' if m.group(2) in KIND_LABEL else ''}{m.group(3)})", t, count=1)


@dataclass
class Result:
    question: str
    answer: str
    evidence: list = field(default_factory=list)
    graded: list = field(default_factory=list)  # every candidate + its grade (diagnostics)
    enough: bool = True
    queries: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    llm_calls: int = 0
    seconds: dict = field(default_factory=dict)
    lang: str = "en"  # language of the user's question; the answer is in the same language when translation worked
    answer_en: str = ""  # the verified English answer (set only for translated results)
    official: list = field(default_factory=list)  # [(bill, section)] whose official local-language text informed the answer
    glossary_hints: list = field(default_factory=list)  # "local = English" names given to the question -> English step
    glossary_terms: list = field(default_factory=list)  # English terms whose official local form the answer prompt got
    clarify: bool = False  # the answer asks which bill was meant (nothing is kept in the chat history for it)
    added_overview: str = ""  # bill/Act whose overview was added to the evidence for a "key provisions / summarise" question
    added_sections: list = field(default_factory=list)  # sections added to bill-level-only evidence (bill_sections)
    followed: list = field(default_factory=list)  # sections added because a kept clause refers to them (follow_section_refs)
    acts: list = field(default_factory=list)  # how bill evidence was related to Acts: {"bill", "section", "act", "act_section", "status"}
    focus: dict = None  # {"title", "bills", "score"}: the bill an unnamed "this new bill" was taken to mean
    quote_fixes: list = field(default_factory=list)  # [(quoted, official)] misspelt quotations replaced by the official text
    direct_rejected: str = ""  # the rejected one-step answer (diagnostics)
    direct_fallback: str = ""  # why a one-step local-language answer was rejected (then: English answer + translation)
    path: str = "en"  # how the answer was written: en | direct (in the user's language from official text) | translated

    def sources(self):
        out = []
        for e in self.evidence:
            c = e.chunk
            where = f"§{e.section}" if e.section is not None else c["kind"]
            official = " - official Sinhala text used" if (e.key, e.section) in self.official else ""
            out.append(f"[{e.key} {where}, p.{c['page']}] {version_tag(c)}{official}")
        return out


def parse_json(text):
    """First valid JSON object in a model reply (tolerates chatter/reasoning text and stray braces); None if none."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


STRICT = "\n\nReply with the JSON object ONLY - no analysis, no explanation, no text before or after it."


class Pipeline:
    def __init__(self, store, retriever, llm, k_clauses=14, k_bills=4, extra_rounds=1, keep_max=8,
                 evidence_chars=14000, on_event=None, grade_effort=None, answer_effort=None, answer_llm=None,
                 translate_mode="script", translator=None, direct_local=True, official_chars=2500, prefer_latest=True, follow_refs=True, trust_top=False):
        self.store, self.retriever, self.llm = store, retriever, llm
        self.k_clauses, self.k_bills, self.extra_rounds = k_clauses, k_bills, extra_rounds
        self.keep_max, self.evidence_chars = keep_max, evidence_chars
        self.on_event = on_event or (lambda *_: None)
        self.grade_effort, self.answer_effort = grade_effort, answer_effort
        self.answer_llm = answer_llm or llm  # the answer step may use a different model than grading
        self.translate_mode = translate_mode  # off | script | ai (see translate.py)
        self.translator = translator or translate.Translator(llm, grade_effort)  # may use a dedicated translation model
        self.direct_local = direct_local  # Sinhala question + official Sinhala text -> answer in Sinhala in one step
        self.official_chars = official_chars
        self.follow_refs, self.trust_top, self.max_refs = follow_refs, trust_top, 2
        self.prefer_latest = prefer_latest  # no year in the question -> newest instance of a recurring title
        self._last_cut = False  # did the last _llm reply hit max_tokens
        self._glossary = None  # loaded on first non-English question
        self.history = []
        self.last = None  # the previous Result of this conversation (for "answer that in Tamil")
        self.emit = None  # optional emit(kind, data): "status" (step name), "token" (answer text), "reset" (drop the draft)

    # ------------------------------------------------------------ steps
    def _step(self, name):
        calltrace.step(name)
        if self.emit:
            self.emit("status", name)

    def _streamer(self):
        """on_token for a streamed answer call. Holds back the first characters until it is clear the reply is an answer,
        not leaked reasoning ("analysis: ..."); leaked reasoning is never shown (the final, checked answer replaces it)."""
        self.emit("reset", None)
        buf, mode = [], ["wait"]

        def on_token(piece):
            if mode[0] == "live":
                self.emit("token", piece)
            elif mode[0] == "wait":
                buf.append(piece)
                head = "".join(buf)
                if len(head.lstrip()) >= 12 or "\n" in head.lstrip():
                    mode[0] = "quiet" if REASONING.match(head) else "live"
                    if mode[0] == "live":
                        self.emit("token", head)

        def flush():  # a reply shorter than the hold-back window
            if mode[0] == "wait" and buf and not REASONING.match("".join(buf)):
                self.emit("token", "".join(buf))
        return on_token, flush

    def _llm(self, system, user, max_tokens, res, effort=None, json_out=False, llm=None, stream=False):
        def call(u):
            res.llm_calls += 1
            kw = {}
            if stream and self.emit:
                kw["on_token"], flush = self._streamer()
            r = (llm or self.llm).chat(system, [{"role": "user", "content": u}], max_tokens=max_tokens, effort=effort, **kw)
            if kw:
                flush()
            txt, self._last_cut = r.text, getattr(r, "truncated", False)
            txt = LEAK.split(txt)[0].strip() or txt.strip()
            if json_out:
                return txt, False
            txt, leaked = strip_reasoning(txt)
            return txt, leaked or degenerate(txt)

        txt, leaked = call(user)
        if leaked:  # (JSON replies from the grader are parsed, and retried, by evaluate())
            txt, leaked = call(user + RETRY_NOTE)
            if leaked:
                res.warnings.append(UNUSABLE)
                return LEAK_FALLBACK
        return txt

    def condense(self, question, res):
        if not self.history:
            return question
        hist = "\n".join(f"Q: {q}\nA: {a[:600]}" for q, a in self.history[-2:])
        out = self._llm("You rewrite questions.", CONDENSE_PROMPT.format(history=hist, question=question), 300, res,
                         self.grade_effort)
        return out.strip().strip('"') or question

    def retrieve(self, query, seen_ids, keys=None):
        found, fresh = [], []
        clauses = self.retriever.search(query, k=self.k_clauses, keys=keys, kinds=CLAUSE_KINDS)
        bills = [] if keys else self.retriever.search(query, k=self.k_bills, kinds=("overview",))
        for c in clauses + bills:
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                fresh.append(c)
        if self.prefer_latest and not keys and not re.search(r"\b(19|20)\d{2}\b", query):
            fresh = self.newest_instance(query, fresh, seen_ids)
        return fresh

    def newest_instance(self, query, cands, seen_ids):
        """Titles recur ("Appropriation", "Finance (Amendment)"); when the question gives no year and the hits come from an
        older instance, swap them for the same search inside the newest instance (billrag.meta)."""
        old, groups = set(), {}
        for c in cands:
            m = meta.meta(self.store, c["bill_key"])
            if m and not m["is_latest"]:
                old.add(c["id"]); groups[m["title_norm"]] = m
        if not old:
            return cands
        out = [c for c in cands if c["id"] not in old]
        for tn in groups:
            keys = meta.latest_family_keys(self.store, tn)
            added = [c for c in self.retriever.search(query, k=3, keys=keys, kinds=CLAUSE_KINDS + ("overview",))
                     if c["id"] not in seen_ids]
            for c in added:
                seen_ids.add(c["id"])
            out += added
            self.on_event("newest_instance", {"title": tn, "keys": keys, "dropped": len(old), "added": len(added)})
        return out

    def evaluate(self, question, cands, res):
        """Grade candidates; returns (scores by candidate index, enough, missing, queries)."""
        blocks = []
        for i, c in enumerate(cands):
            if c["kind"] == "overview":
                tag, txt = "[BILL-LEVEL] ", shown_text(c)[:650]
            else:
                tag, txt = "", c["text"][:750]
            where = f"§{c['section_no']}" if c.get("section_no") is not None else c["kind"]
            blocks.append(f"[{i}] {tag}{c['bill_key']} {where} — {version_tag(c)}\n{txt}")
        prompt = EVAL_PROMPT.format(question=question, candidates="\n\n".join(blocks))
        d = parse_json(self._llm("You output only valid JSON.", prompt, 1500, res, self.grade_effort, json_out=True))
        if not d or not isinstance(d.get("scores"), list):  # reasoning models sometimes answer in prose: retry once, firmer
            d = parse_json(self._llm("You output only valid JSON.", prompt + STRICT, 2000, res, None, json_out=True))
        if not d or not isinstance(d.get("scores"), list):
            res.warnings.append("evaluation step returned unusable output; using retrieval order")
            return {i: (3 if i < 5 else 1) for i in range(len(cands))}, True, "", []
        scores = {}
        for s in d["scores"]:
            try:
                scores[int(s["i"])] = max(0, min(3, int(s["s"])))
            except (KeyError, TypeError, ValueError):
                continue
        return scores, bool(d.get("enough", True)), str(d.get("missing", "")), [q for q in d.get("queries", []) if isinstance(q, str)][:2]

    SECTION_CHARS = 3000  # most of a section the answering model is shown (the whole evidence has its own budget)

    def expand(self, ev, hits=None):
        """Replace a clause snippet by the section text (a section can span several chunks). A section that fits the budget is shown
        whole. A longer one is shown as a WINDOW: the chunk(s) that matched (`hits`, default the chunk itself), then the parts next to
        them while the budget lasts, in reading order, with a plain note where parts are left out - so a match late in a long section
        is no longer cut off, and the model can say what it has not seen."""
        c = ev.chunk
        if c["kind"] == "section" and c.get("section_no") is not None:
            parts = self.store.section_parts(c["bill_key"], c["section_no"]) if hasattr(self.store, "section_parts") else []
            if parts:
                c = {**c, "text": self.section_window(parts, hits or {c.get("id")}, c["section_no"])}
            else:
                s = self.store.section_text(c["bill_key"], c["section_no"])
                if s:
                    c = {**c, "text": s["text"][:self.SECTION_CHARS]}
        return Evidence(c, ev.score)

    def section_window(self, parts, hits, no):
        cap = self.SECTION_CHARS
        if sum(len(p["text"]) + 1 for p in parts) - 1 <= cap:
            return "\n".join(p["text"] for p in parts)
        chosen = {i for i, p in enumerate(parts) if p["id"] in hits} or {0}
        size = sum(len(parts[i]["text"]) + 1 for i in chosen)
        lo, hi, more = min(chosen), max(chosen), True
        while more:  # grow outwards, the continuation first, while the next part still fits
            more = False
            if hi + 1 < len(parts) and size + len(parts[hi + 1]["text"]) + 1 <= cap:
                hi += 1; size += len(parts[hi]["text"]) + 1; more = True
            if lo > 0 and size + len(parts[lo - 1]["text"]) + 1 <= cap:
                lo -= 1; size += len(parts[lo]["text"]) + 1; more = True
        text = "\n".join(p["text"] for p in parts[lo:hi + 1])
        if lo > 0:
            text = f"(... the first {lo} of {len(parts)} parts of section {no} are not shown ...)\n" + text
        if hi + 1 < len(parts):
            text += f"\n(... {len(parts) - hi - 1} more part(s) of section {no} are not shown ...)"
        return text

    def build_evidence(self, graded):
        kept = [e for e in graded if e.score >= 2]
        if not kept:  # nothing graded useful: fall back to the best of the rest so the model can say "not found" honestly
            kept = [e for e in graded if e.score == 1][:3]
        # best first; among equals prefer newer bills (users mostly ask about recent ones)
        kept.sort(key=lambda e: (-e.score, e.chunk["date"]), reverse=False)
        kept.sort(key=lambda e: (e.score, e.chunk["date"]), reverse=True)
        out, seen, used = [], set(), 0
        hits = {}  # every kept chunk of a section, so a long section's window covers all the parts that matched
        for e in kept[: self.keep_max]:
            if e.section is not None and e.chunk.get("id") is not None:
                hits.setdefault((e.key, e.section), set()).add(e.chunk["id"])
        for e in kept[: self.keep_max]:
            ident = (e.key, e.section) if e.section is not None else (e.key, e.chunk["kind"])
            if ident in seen:
                continue
            e = self.expand(e, hits.get(ident))
            if used + len(e.chunk["text"]) > self.evidence_chars and out:
                continue
            seen.add(ident)
            used += len(e.chunk["text"])
            out.append(e)
        return out

    PENALTY_Q = re.compile(r"\b(penalt\w*|punish\w*|sentenc\w*|fines?|fined|imprison\w*|liable|jail\w*|prison\w*|offen[cs]es?)\b", re.I)
    PENALTY_TEXT = re.compile(r"imprison|\bfine\b|penalt|punish|liable|sentence", re.I)

    def follow_section_refs(self, evidence, question=""):
        """Sections of the same bill that belong with a kept clause but were not retrieved, at most `max_refs` of each kind,
        added after the graded clauses (score 2):
        - forward: sections the clause points at ("the penalty in section 3(2)(c)");
        - backward, only for a question about penalties/offences: sections that point AT the clause and speak of
          imprisonment, fines or liability. An offence section ("hostage-taking is a consequence in s.3(2)(c)") never says
          what the penalty is; the penalty section (s.4) points back at it and names no offence."""
        have = {(e.key, e.section) for e in evidence if e.section is not None}
        out = []

        def add(key, n):
            if (key, n) in have:
                return False
            r = self.store.db.execute("SELECT id FROM chunks WHERE bill_key=? AND section_no=? AND kind='section' "
                                      "ORDER BY ord LIMIT 1", (key, n)).fetchone()
            ch = self.store.chunks_by_ids([r["id"]]).get(r["id"]) if r else None
            if not ch:
                return False
            have.add((key, n))
            out.append(self.expand(Evidence(ch, 2)))
            return True

        fwd = back = 0
        penalty = bool(self.PENALTY_Q.search(question or ""))
        # only the top-ranked clause's bill (and its own GS / Bill versions): other bills' boilerplate adds noise, not answers
        top = next((e for e in evidence if e.section is not None and e.score >= 2), None)
        if top is None:
            return out
        b = self.store.bill(top.key)
        family = {v["key"] for v in self.store.versions(b["title_norm"], b["date"])} | {top.key} if b else {top.key}
        for e in evidence:
            if e.section is None or e.score < 2 or e.key not in family:
                continue
            for n in meta.refs_from(self.store, e.key, e.section):
                if fwd < self.max_refs and add(e.key, n):
                    fwd += 1
            if not penalty:
                continue
            for n in meta.refs_to(self.store, e.key, e.section):
                if back >= self.max_refs:
                    break
                sec = self.store.section_text(e.key, n)
                if sec and self.PENALTY_TEXT.search(sec["text"]) and add(e.key, n):
                    back += 1
        if out:
            self.on_event("followed_refs", {"sections": [f"{x.key} \u00a7{x.section}" for x in out]})
        return out

    def summarise(self, question, evidence, enough, missing, res, local=None, focus=None, extra="", stream=False):
        """local: (lang code, {(bill, section): official text}) -> write the answer directly in that language."""
        if not evidence:
            return "I could not find anything in the indexed bills that answers this question."
        code, official = local or (None, {})
        lang = translate.language_name(code) if code else ""
        blocks = []
        for e in evidence:
            c = e.chunk
            label = f"[{e.key} §{e.section}]" if e.section is not None else f"[{e.key}]"
            kind = "bill-level description" if c["kind"] == "overview" else f"p.{c['page']}"
            also = f" (identical text also in {', '.join(c['also_in'])})" if c.get("also_in") else ""
            status = c.get("status") or acts.status_lines(self.store, e.key, e.section) if hasattr(self.store, "db") else ""
            block = f"{label} {version_tag(c)}, {kind}{also}\n" + (f"STATUS: {status}\n" if status else "") + shown_text(c)
            if (e.key, e.section) in official:  # label without [ ] in the header so it is not a second citation form
                block += f"\n--- OFFICIAL {lang.upper()} TEXT of this section ---\n{official[(e.key, e.section)][:self.official_chars]}"
            blocks.append(block)
        note = "" if enough else (f"\n\nNOTE: the grader judged this evidence INCOMPLETE. Missing: {missing or 'unspecified'}. "
                                   "Answer what the evidence supports and state clearly what is missing.")
        if focus:
            note += FOCUS_NOTE.format(title=focus["title"], keys=", ".join(focus["bills"]))
        user = f"QUESTION: {question}\n\nEVIDENCE:\n\n" + "\n\n".join(blocks) + note + extra
        system = ANSWER_SYSTEM + (LOCAL_NOTE.format(lang=lang, LANG=lang.upper()) if code else "")
        return self._llm(system, user, 4000 if code else 1800, res, self.answer_effort, llm=self.answer_llm, stream=stream)

    COMPARE = re.compile(r"\b(differ\w*|chang\w*|compar\w*|versus|vs\.?|committee|amended\s+in\s+parliament|original\s+bill|as\s+proposed|proposed\s+in\s+the\s+bill)\b", re.I)

    def _act_section_evidence(self, key, no, score):
        r = self.store.db.execute("SELECT id FROM chunks WHERE bill_key=? AND section_no=? AND kind='section' ORDER BY ord LIMIT 1",
                                  (key, int(no))).fetchone()
        ch = self.store.chunks_by_ids([r["id"]]).get(r["id"]) if r else None
        return self.expand(Evidence(ch, score)) if ch else None

    def _act_overview_evidence(self, key, score):
        r = self.store.db.execute("SELECT id FROM chunks WHERE bill_key=? AND kind='overview' LIMIT 1", (key,)).fetchone()
        ch = self.store.chunks_by_ids([r["id"]]).get(r["id"]) if r else None
        return Evidence(ch, score) if ch else None

    def apply_acts(self, question, evidence, keys, res):
        """If a bill in the evidence became an Act, answer from the Act. A clause with the same text is replaced by the Act's section
        (its number can differ); a changed clause is shown after the Act's version; a clause not in the Act stays, marked by its
        STATUS line. A question that names the bill number or compares versions keeps the bill's clause too. Deterministic: no model."""
        if not hasattr(self.store, "db") or not acts.has_tables(self.store):
            return evidence
        keep_bills = bool(keys) or bool(self.COMPARE.search(question or ""))
        have = {(e.key, e.section) for e in evidence}
        out = []

        def add(ev):
            if ev and (ev.key, ev.section) not in have:
                have.add((ev.key, ev.section))
                out.append(ev)
                return True
            return False

        def log(e, act, sec, status):
            res.acts.append({"bill": e.key, "section": e.section, "act": act, "act_section": sec, "status": status})

        for e in evidence:
            if acts.is_act(e.key):
                out.append(e)
                continue
            a = acts.act_for_bill(self.store, e.key)
            if not a or not a["direct"]:
                out.append(e)
                continue
            m = acts.bill_section(self.store, e.key, e.section) if e.section is not None else None
            if not m or m["act_section"] is None:  # bill-level text, a schedule, or a clause with no counterpart in the Act
                add(self._act_overview_evidence(a["key"], e.score))
                out.append(e)
                log(e, a["key"], None, m["status"] if m else "bill-level")
                continue
            add(self._act_section_evidence(m["act_key"], m["act_section"], e.score))
            log(e, m["act_key"], m["act_section"], m["status"])
            if m["status"] != "same" or keep_bills:
                out.append(e)
        if res.acts:
            self.on_event("acts", {"related": len(res.acts)})
        return out

    def answer_direct(self, question, user_question, evidence, enough, missing, res, code, focus=None):
        """One-step answer in the user's language when the bill's official text in that language exists for the evidence.
        -> (answer, official refs) or (None, reason) so the caller falls back to English answer + translation."""
        refs = self.official_texts(evidence, code)
        if not refs:
            return None, "no official text"
        official = {(k, n): t for k, n, t in refs}
        terms = gloss.terms_block(self.glossary_terms(evidence, code, res), translate.language_name(code))
        q = f"{user_question}\n(English rendering used for search: {question})"
        reasons = []
        for _attempt in range(2):  # one retry: a direct answer is ~15 s, the translate-out fallback 30-100 s and worse
            out = self.summarise(q, evidence, enough, missing, res, local=(code, official), focus=focus, extra=terms,
                                 stream=True)
            out = out.replace("\u200b", "")  # stray zero-width spaces (seen live); ZWJ U+200D is part of Sinhala conjuncts
            why = ("unusable output" if out == LEAK_FALLBACK else "cut off at the output limit" if self._last_cut
                   else translate.check_script(out, code))
            if not why and not CITE.search(out):
                why = "no citations"
            if not why and self.verify(out, evidence):
                why = "citations not in the evidence: " + ", ".join(self.verify(out, evidence))
            if not why:
                out, fixes = translate.fix_quotes(out, list(official.values()), code=code)
                res.quote_fixes += fixes
                if reasons:
                    res.direct_fallback = "retried after: " + reasons[0]
                return out, refs
            reasons.append(why)
            res.direct_rejected = out[:3000]  # logged, so a rejection can be diagnosed
        return None, "; ".join(reasons)

    def bill_sections(self, question, evidence, k=4):
        """Kept evidence is only bill-level descriptions (typical for "summarise / tell me about the X bill"): add the bill's
        own best-matching sections (retrieval inside that bill and its GS/Bill versions, no extra LLM call), so the answer
        rests on clause text - and, for a Sinhala question, on the official Sinhala text - not only on the auto summary."""
        top = max(evidence, key=lambda e: (e.score, e.chunk["date"]))
        b = self.store.bill(top.key) if hasattr(self.store, "bill") else None
        keys = [top.key]
        if b and hasattr(self.store, "versions") and b.get("kind") != "Act":
            keys = [v["key"] for v in self.store.versions(b["title_norm"], b["date"])] or keys
        out, seen = [], set()
        for c in self.retriever.search(question, k=k * 2, keys=keys, kinds=("section",)):
            if c.get("section_no") is None or c["section_no"] in seen:
                continue
            seen.add(c["section_no"])
            out.append(self.expand(Evidence(c, 2)))
            if len(out) == k:
                break
        out.sort(key=lambda e: e.section)
        if out:
            self.on_event("bill_sections", {"bill": top.key, "sections": [f"{e.key} \u00a7{e.section}" for e in out]})
        return out

    def titled_bills(self, question, evidence):
        """The bills/Acts in the evidence whose title the question names in words ("summarise port city economic commision act"):
        every distinctive title word must appear in the question (a close spelling counts), at least two words or one of 6+ letters.
        Returns one key per title (the newest), those with the most matching words only; one entry = named, several = ambiguous."""
        import difflib
        qw = set(re.findall(r"[a-z]{3,}", question.lower()))
        best = {}
        for k in {e.key for e in evidence}:
            b = self.store.bill(k) if hasattr(self.store, "bill") else None
            if not b:
                continue
            title = re.sub(r",?\s*No\.?\s*\d+\s+of\s+\d{4}|\s-\s(?:Bill|GS)$", "", b["title"])
            words = [w for w in re.findall(r"[a-z]{3,}", title.lower()) if w not in _TITLE_STOP]
            if not words or (len(words) < 2 and len(words[0]) < 6):
                continue
            if all(w in qw or difflib.get_close_matches(w, qw, 1, 0.85) for w in words):
                tn = b.get("title_norm") or title
                if tn not in best or b["date"] > best[tn][2]:
                    best[tn] = (k, len(words), b["date"], b["title"])
        if not best:
            return []
        top = max(v[1] for v in best.values())
        return sorted((v for v in best.values() if v[1] == top), key=lambda v: v[2], reverse=True)

    def title_ranker(self):
        """The title-index ranker, loaded once; None when the index has no title_index table (older index): then titled_bills is used."""
        if getattr(self, "_title_ranker", 0) == 0:
            ok = hasattr(self.store, "db") and title_index.has_index(self.store)
            self._title_ranker = title_index.Ranker(self.store) if ok else None
        return self._title_ranker

    def which_of(self, cands, total=None):
        """A reply that asks which bill was meant, with the candidates (no model call)."""
        lines = [f"- {re.sub(r' - (?:Bill|GS)$', '', t)} ({k}, {d[:10]})" for k, _n, d, t in cands[:5]]
        if (total or len(cands)) > 5:
            lines.append(f"- ... and {(total or len(cands)) - 5} more with similar titles")
        return ("Your question could mean more than one bill. Which did you mean?\n\n" + "\n".join(lines) +
                "\n\nReply with the bill number (for example, the one in brackets) or the full title.")

    def focus_bill(self, question, evidence, keys=None):
        """For a question that says "this new bill" without naming one: the bill (all its GS/Bill versions together) whose
        evidence the grader scored highest; ties -> more total score, then newer. None if the question names a bill or the
        evidence comes from one bill anyway."""
        if keys or not UNNAMED.search(question) or not evidence:
            return None
        fams = {}
        for e in evidence:
            b = self.store.bill(e.key) if hasattr(self.store, "bill") else None
            fam = (b or {}).get("title_norm") or re.sub(r"\s*-\s*(gs|bill)\s*$", "", e.chunk["title"].lower()).strip()
            f = fams.setdefault(fam, {"title": re.sub(r"\s*-\s*(GS|Bill)\s*$", "", e.chunk["title"]).strip(), "bills": [],
                                      "max": 0, "sum": 0, "date": ""})
            if e.key not in f["bills"]:
                f["bills"].append(e.key)
            f["max"], f["sum"], f["date"] = max(f["max"], e.score), f["sum"] + e.score, max(f["date"], e.chunk["date"])
        if len(fams) < 2:
            return None
        best = max(fams.values(), key=lambda f: (f["max"], f["sum"], f["date"]))
        return {"title": best["title"], "bills": sorted(best["bills"]), "score": best["max"]}

    @staticmethod
    def verify(answer, evidence):
        """Citations in the answer that do not point at evidence we showed the model. Bill and Act section numbers are unrelated,
        which is why `also_in` never crosses between them (retrieve.py)."""
        ok_sec = {(e.key, e.section) for e in evidence if e.section is not None}
        ok_key = {e.key for e in evidence}
        for e in evidence:  # identical text in sibling versions (GS/Bill) is also fair to cite
            for k in e.chunk.get("also_in", []):
                ok_key.add(k)
                if e.section is not None:
                    ok_sec.add((k, e.section))
        bad = []
        for key, sec in CITE.findall(answer):
            if (sec and (key, int(sec)) not in ok_sec) or (not sec and key not in ok_key):
                bad.append(f"{key} §{sec}" if sec else key)
        return sorted(set(bad))

    # ------------------------------------------------------------ driver
    def ask(self, question: str) -> Result:
        """One turn. A bare "in Tamil" / "answer in English" re-renders the previous answer (no search); anything else goes
        through the translation layer."""
        target = translate.language_switch(question) if self.last is not None and self.translate_mode != "off" else None
        res = self.switch_language(question, target) if target else self.ask_translated(question)
        self.last = res
        return res

    def last_english(self, last):
        """The previous answer in English: kept for English/translated answers; a direct local-language answer is translated back."""
        if last.path in ("translated", "language_switch") and last.answer_en:
            return last.answer_en
        if last.path == "direct":
            return self.translator.to_english(last.answer, last.lang)
        return last.answer

    def switch_language(self, question, target):
        """The user only asked for the previous answer in another language: translate it again, with the same official terms."""
        last, tr = self.last, self.translator
        tr.calls = 0
        res = Result(question=question, answer="", lang=target, evidence=list(last.evidence), enough=True, path="language_switch")
        self._step("translate_out")
        t = time.time()
        english = self.last_english(last)
        if not english:
            res.answer = last.answer
            res.warnings.append("could not translate the previous answer; showing it unchanged")
        elif target == "en":
            res.answer = res.answer_en = english
        else:
            res.answer_en = english
            refs = self.official_texts(res.evidence, target)
            terms = self.glossary_terms(res.evidence, target, res)
            out, reasons = tr.from_english(english, target, [(f"{k} \u00a7{n}", x) for k, n, x in refs],
                                           gloss.terms_block(terms, translate.language_name(target)))
            res.official = [(k, n) for k, n, _ in refs]
            if out:
                res.answer, fixes = translate.fix_quotes(out, [x for _, _, x in refs], code=target if refs else None)
                res.quote_fixes += fixes
            else:
                res.answer = english
                res.warnings.append(f"could not translate the answer into {translate.language_name(target)} reliably "
                                    f"({'; '.join(reasons) or 'no output'}); showing English")
        res.seconds["translate_out"] = time.time() - t
        res.llm_calls = tr.calls
        self.history.append((question, english or last.answer))
        return res

    def ask_translated(self, question: str) -> Result:
        """Translation layer around `_ask`: any language -> English -> pipeline -> answer in the question's language."""
        tr = self.translator
        mode, lang, q_en, t = self.translate_mode, "en", question, time.time()
        calltrace.step("translate_in")  # also the language-detection call of `ai` mode for an English question
        glossary_hints = []
        tr.calls = 0
        if mode != "off":
            lang = translate.detect(question)  # Sinhala / Tamil by script: free
            if lang != "en":
                self._step("translate_in")
                hints = self.glossary(lang).in_question(question) if lang == "si" else []
                q_en = tr.to_english(question, lang, [(loc, en) for loc, en, _k in hints])
                q_en = self.keep_titles(q_en, hints)
                glossary_hints = [f"{loc} = {en}" for loc, en, _k in hints]
            elif mode == "ai":  # any other language (or romanised Sinhala/Tamil): one call detects and translates
                self._step("translate_in")
                got = tr.detect_ai(question)
                if got is None:
                    res = self._ask(question)
                    res.llm_calls += tr.calls
                    res.warnings.append("language detection failed; treated the question as English")
                    return res
                lang, q_en = got
        t_in = time.time() - t
        if lang == "en":
            res = self._ask(question)
            res.llm_calls += tr.calls
            if mode == "ai":
                res.seconds["detect_language"] = t_in
            return res
        name = translate.language_name(lang)
        if not q_en:
            return Result(question=question, lang=lang, llm_calls=tr.calls, answer=(
                "Sorry, I could not translate that question into English to search the bills. Please try again or ask in English."))
        res = self._ask(q_en, local=(lang, question))
        res.question, res.lang = question, lang
        res.glossary_hints = glossary_hints
        res.seconds["translate_in"] = t_in
        if res.path == "direct":  # written in the user's language from the official text: nothing to translate
            res.llm_calls += tr.calls
            return res
        res.answer_en, res.path = res.answer, "translated"
        t = time.time()
        self._step("translate_out")
        refs = self.official_texts(res.evidence, lang)
        terms = self.glossary_terms(res.evidence, lang, res)
        out, reasons = tr.from_english(res.answer_en, lang, [(f"{k} \u00a7{n}", t) for k, n, t in refs],
                                       gloss.terms_block(terms, translate.language_name(lang)))
        res.official = [(k, n) for k, n, _ in refs]
        res.seconds["translate_out"] = time.time() - t
        res.llm_calls += tr.calls
        if out:
            res.answer, fixes = translate.fix_quotes(out, [t for _, _, t in refs], code=lang if refs else None)
            res.quote_fixes += fixes
        else:
            res.warnings.append(f"could not translate the answer into {name} reliably ({'; '.join(reasons) or 'no output'}); "
                                "showing English")
        return res

    def glossary(self, lang):
        if self._glossary is None:
            self._glossary = gloss.Glossary(self.store, lang)
        return self._glossary

    @staticmethod
    def keep_titles(q_en, hints):
        """If the translation dropped or renamed a bill the question names, add its official English title."""
        if not q_en:
            return q_en
        for _loc, en, kind in hints:
            if kind == "title" and en.lower() not in q_en.lower():
                q_en = f"{q_en.rstrip()} ({en})"
        return q_en

    def glossary_terms(self, evidence, lang, res):
        """[(en, local)] official terms for English terms in the evidence (Sinhala only); recorded on the result."""
        if lang != "si" or not evidence:
            return []
        terms = self.glossary(lang).for_english("\n".join(e.chunk["text"] for e in evidence))
        res.glossary_terms = [en for en, _ in terms]
        return terms

    def official_texts(self, evidence, lang):
        """[(bill, section, text)]: the bill's own text in `lang` for the evidence sections (Sinhala only so far; parse_si).
        A sibling GS/Bill version with identical English text may supply it when the cited version has none."""
        if lang != "si" or not hasattr(self.store, "local_section"):
            return []
        out, seen = [], set()
        for e in evidence:
            if e.section is None:
                continue
            for key in [e.key] + list(e.chunk.get("also_in", [])):
                row = self.store.local_section(key, e.section, lang)
                if row and (e.key, e.section) not in seen:
                    seen.add((e.key, e.section))
                    out.append((e.key, e.section, parse_si.join_marks(row["text"])))
                    break
        return out

    def _ask(self, question: str, local=None) -> Result:
        """local: (lang code, the user's original question) for a non-English question; see answer_direct."""
        res = Result(question=question, answer="")
        plan = resolve.resolve_question(self.store, question, has_history=bool(self.history))
        if plan["action"] == "reply":  # greeting, or an abbreviation we cannot ground: no search, no model call
            res.answer = plan["text"]
            self.on_event("resolve", {"reply": True, "clarify": bool(plan.get("clarify"))})
            if not plan.get("clarify"):  # a "which bill?" question leaves no context the next turn could refer to
                self.history.append((question, res.answer))
            return res
        notes = plan["notes"]
        if notes:
            self.on_event("resolve", {"notes": notes})
        t = time.time()
        self._step("condense")
        q = self.condense(plan["question"], res)
        res.seconds["condense"] = time.time() - t
        act_refs = {normalize_ref(m.group(0)) for m in ACT_REF.finditer(q)}  # "Act No. 25 of 2026"; other numbers are bill numbers
        bare = ACT_REF.sub(" ", q)
        refs = act_refs | {normalize_ref(m) for m in re.findall(r"\d{1,3}\s*/\s*\d{4}", bare)}
        keys = [k for k in refs if k and self.store.bill(k)] or None
        ov_key = keys[0] if keys and len(keys) == 1 else None
        if not keys and OVERVIEW_Q.search(q):
            ranker = self.title_ranker()
            if ranker:  # a gist question: which bill or Act does it name? (title index, see titles.py)
                decision, cands = ranker.rank(q)
                if decision == "many":  # several titles fit equally: ask, before spending a search or a model call
                    res.answer, res.enough, res.path, res.clarify = self.which_of(
                        [(c["key"], 0, c["date"] or "", c["title"]) for c in cands], len(cands)), True, "en", True
                    self.on_event("resolve", {"reply": True, "clarify": True, "candidates": [c["key"] for c in cands[:5]]})
                    return res
                if decision == "one":
                    keys, ov_key = json.loads(cands[0]["keys"]), cands[0]["key"]
                    self.on_event("resolve", {"titled": cands[0]["title"], "keys": keys})
        seen_ids, graded, queries = set(), [], [q]
        enough, missing = True, ""
        for rnd in range(1 + self.extra_rounds):
            t = time.time()
            self._step(f"retrieve{rnd}")
            cands = []
            for qq in queries:
                cands += self.retrieve(qq, seen_ids, keys)
            res.seconds[f"retrieve{rnd}"] = time.time() - t
            self.on_event("retrieve", {"queries": queries, "candidates": len(cands)})
            if not cands:
                break
            t = time.time()
            self._step(f"evaluate{rnd}")
            scores, enough, missing, more = self.evaluate(q, cands, res)
            if self.trust_top:  # the retriever's best clause is rarely tangential: a grader score of 1 is promoted to 2
                top = next((i for i, c in enumerate(cands) if c["kind"] in CLAUSE_KINDS), None)
                if top is not None and scores.get(top) == 1:
                    scores[top] = 2
            res.seconds[f"evaluate{rnd}"] = time.time() - t
            graded += [Evidence(c, scores.get(i, 0)) for i, c in enumerate(cands)]
            self.on_event("evaluate", {"kept": sum(1 for e in graded if e.score >= 2), "enough": enough, "missing": missing})
            if enough or not more or rnd == self.extra_rounds:
                break
            queries = more
            res.queries += more
        res.enough, res.queries, res.graded = enough, [q] + res.queries, graded
        res.evidence = self.build_evidence(graded)
        if self.follow_refs and res.evidence:
            res.followed = self.follow_section_refs(res.evidence, q)
            res.evidence += res.followed
        if local and self.direct_local and res.evidence and all(e.section is None for e in res.evidence):
            t = time.time()
            self._step("bill_sections")
            res.added_sections = self.bill_sections(q, res.evidence)
            res.evidence += res.added_sections
            res.seconds["bill_sections"] = time.time() - t
        if ov_key is None and not keys and OVERVIEW_Q.search(q) and res.evidence and not self.title_ranker():
            named = self.titled_bills(q, res.evidence)  # named by title, not number
            if len(named) > 1:  # two different titles fit: ask instead of guessing
                res.answer, res.enough, res.path = self.which_of(named), True, "en"
                res.evidence, res.clarify = [], True
                self.on_event("resolve", {"reply": True, "clarify": True, "candidates": [v[0] for v in named]})
                return res
            ov_key = named[0][0] if named else None
        if ov_key and OVERVIEW_Q.search(q) and not any(e.chunk["kind"] == "overview" and e.key == ov_key for e in res.evidence):
            ov = self._act_overview_evidence(ov_key, 3)
            if ov:
                res.evidence.insert(0, ov)  # the instrument's own description first
                res.added_overview = ov_key
                enough, missing, res.enough = True, "", True  # the question asks for the gist, not for every clause
                self.on_event("overview_added", {"bill": ov_key})
        res.evidence = self.apply_acts(q, res.evidence, keys, res)
        focus = res.focus = self.focus_bill(q, res.evidence, keys)
        if focus:  # its clauses first (stable sort keeps the grader's order within each group)
            res.evidence.sort(key=lambda e: e.key not in focus["bills"])
            self.on_event("focus", focus)
        if local and self.direct_local and res.evidence:
            t = time.time()
            self._step("answer_direct")
            user_q = local[1] + ("\n(State this first: " + " ".join(notes) + ")" if notes else "")
            out, info = self.answer_direct(q, user_q, res.evidence, enough, missing, res, local[0], focus)
            res.seconds["answer_direct"] = time.time() - t
            if out:
                res.answer, res.official, res.path = out, [(k, n) for k, n, _ in info], "direct"
            elif info == "no official text":
                del res.seconds["answer_direct"]
            else:
                res.direct_fallback = info
                self.on_event("answer_direct", {"fallback": info})
        if res.path != "direct":
            t = time.time()
            self._step("summarise")
            res.answer = self.summarise(q, res.evidence, enough, missing, res, focus=focus,
                                        stream=local is None)  # a translated answer is streamed as statuses only
            res.seconds["summarise"] = time.time() - t
            if notes:
                res.answer = " ".join(notes) + "\n\n" + res.answer
        if not res.evidence:
            near = resolve.closest_bills(self.store, question, int(m.group(0)) if (m := re.search(r"\b(19|20)\d{2}\b", question)) else None)
            if near:
                res.answer += "\n\nClosest bills by title: " + "; ".join(f"{t} ({', '.join(k)})" for t, k in near) + "."
        bad = self.verify(res.answer, res.evidence)
        if bad:
            res.warnings.append("citations not backed by the retrieved evidence: " + ", ".join(bad))
        self.history.append((question, res.answer))
        return res
