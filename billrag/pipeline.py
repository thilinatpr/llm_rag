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

from . import calltrace, glossary as gloss, meta, parse_si, resolve, translate
from .store import normalize_ref

CLAUSE_KINDS = ("section", "schedule")
CITE = re.compile(r"[\[【](\d{3}/\d{4})(?:\s*§\s*(\d+))?")  # also the full-width brackets some models emit
REASONING = re.compile(r"^\s*analysis\s*[:\uff1a]?", re.I)
FINAL = re.compile(r"assistant\s*final\s*[:\uff1a]?\s*|\bfinal(?:\s+answer)?\s*[:\uff1a]\s*", re.I)
RETRY_NOTE = "\n\nReply with ONLY the final answer, in the format requested. Do not include any analysis or reasoning text."
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
# Bill-focused mode (whole_bill=True): questions that are clearly about several bills go through the normal pipeline
MULTI_BILL = re.compile(r"\b(?:which|what|any|other|another|all|compare\w*|both|between|list)\s+(?:\w+\s+){0,2}(?:bills|acts|laws)\b"
                        r"|\b(?:which|what|any|another|other)\s+(?:(?:new|recent|older|earlier|previous|later)\s+)?(?:bill|act|law)\b"
                        r"|\bcompar\w*|\bdiffer\w*\s+from\b", re.I)
WHOLE_BILL_HEAD = ("EVIDENCE: the complete text of one bill, every section in order. Answer only from it; if this bill does not "
                   "answer the question, say so.\n\n")

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
- "GS" = bill published in the Gazette before presentation; "Bill" = version presented to Parliament. If both versions
  appear and differ, say so. Committee-stage amendments are not in this corpus.
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
    added_sections: list = field(default_factory=list)  # sections added to bill-level-only evidence (bill_sections)
    followed: list = field(default_factory=list)  # sections added because a kept clause refers to them (follow_section_refs)
    focus: dict = None  # {"title", "bills", "score"}: the bill an unnamed "this new bill" was taken to mean
    quote_fixes: list = field(default_factory=list)  # [(quoted, official)] misspelt quotations replaced by the official text
    direct_rejected: str = ""  # the rejected one-step answer (diagnostics)
    direct_fallback: str = ""  # why a one-step local-language answer was rejected (then: English answer + translation)
    path: str = "en"  # how the answer was written: en | direct (in the user's language from official text) | translated
    #                   | whole_bill (bill-focused follow-up answered from the whole text of one bill)
    whole_bill: dict = None  # {"bill", "sections", "chars"} when path == "whole_bill", or {"fallback": why} when it was dropped

    def sources(self):
        out = []
        for e in self.evidence:
            c = e.chunk
            where = f"§{e.section}" if e.section is not None else c["kind"]
            official = " - official Sinhala text used" if (e.key, e.section) in self.official else ""
            out.append(f"[{e.key} {where}, p.{c['page']}] {c['title']} ({c['bill_kind']}, {c['date'][:10]}){official}")
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
                 translate_mode="script", translator=None, direct_local=True, official_chars=2500, prefer_latest=True, follow_refs=True, trust_top=False,
                 whole_bill=False, whole_bill_chars=120000):
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
        # bill-focused mode: once a chat is about one bill, follow-ups are answered from its whole text (question LAST, so a
        # provider-side prefix cache reuses the bill across turns). focus_key = the bill version the chat is about.
        self.whole_bill, self.whole_bill_chars, self.focus_key = whole_bill, whole_bill_chars, None
        self.history = []
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
                res.warnings.append("the model returned reasoning or unusable text instead of an answer (twice)")
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
                tag, txt = "[BILL-LEVEL] ", c["text"][:650]
            else:
                tag, txt = "", c["text"][:750]
            where = f"§{c['section_no']}" if c.get("section_no") is not None else c["kind"]
            blocks.append(f"[{i}] {tag}{c['bill_key']} {where} — {c['title']} ({c['bill_kind']}, {c['date'][:10]})\n{txt}")
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

    def expand(self, ev):
        """Replace a clause snippet by the full section text (a section can span several chunks)."""
        c = ev.chunk
        if c["kind"] == "section" and c.get("section_no") is not None:
            s = self.store.section_text(c["bill_key"], c["section_no"])
            if s:
                c = {**c, "text": s["text"][:3000]}
        return Evidence(c, ev.score)

    def build_evidence(self, graded):
        kept = [e for e in graded if e.score >= 2]
        if not kept:  # nothing graded useful: fall back to the best of the rest so the model can say "not found" honestly
            kept = [e for e in graded if e.score == 1][:3]
        # best first; among equals prefer newer bills (users mostly ask about recent ones)
        kept.sort(key=lambda e: (-e.score, e.chunk["date"]), reverse=False)
        kept.sort(key=lambda e: (e.score, e.chunk["date"]), reverse=True)
        out, seen, used = [], set(), 0
        for e in kept[: self.keep_max]:
            ident = (e.key, e.section) if e.section is not None else (e.key, e.chunk["kind"])
            if ident in seen:
                continue
            e = self.expand(e)
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

    def summarise(self, question, evidence, enough, missing, res, local=None, focus=None, extra="", stream=False,
                  question_last=False):
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
            block = f"{label} {c['title']} ({c['bill_kind']}, {c['date'][:10]}), {kind}{also}\n{c['text']}"
            if (e.key, e.section) in official:  # label without [ ] in the header so it is not a second citation form
                block += f"\n--- OFFICIAL {lang.upper()} TEXT of this section ---\n{official[(e.key, e.section)][:self.official_chars]}"
            blocks.append(block)
        note = "" if enough else (f"\n\nNOTE: the grader judged this evidence INCOMPLETE. Missing: {missing or 'unspecified'}. "
                                   "Answer what the evidence supports and state clearly what is missing.")
        if focus:
            note += FOCUS_NOTE.format(title=focus["title"], keys=", ".join(focus["bills"]))
        if question_last:  # stable prefix (system prompt + bill text) first, so a prefix cache can reuse it across turns
            user = WHOLE_BILL_HEAD + "\n\n".join(blocks) + note + extra + f"\n\nQUESTION: {question}"
        else:
            user = f"QUESTION: {question}\n\nEVIDENCE:\n\n" + "\n\n".join(blocks) + note + extra
        system = ANSWER_SYSTEM + (LOCAL_NOTE.format(lang=lang, LANG=lang.upper()) if code else "")
        return self._llm(system, user, 4000 if code else 1800, res, self.answer_effort, llm=self.answer_llm, stream=stream)

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
        if b and hasattr(self.store, "versions"):
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

    # ------------------------------------------------------------ bill-focused mode
    def family(self, key):
        """Keys of the bill's own GS / Bill versions (same title, within 300 days), sorted; [key] if unknown."""
        b = self.store.bill(key)
        keys = {v["key"] for v in self.store.versions(b["title_norm"], b["date"])} if b else set()
        return sorted(keys | {key})

    def whole_bill_evidence(self, key):
        """Every section of the bill, in order, full text (a stable, cacheable prompt prefix); None if it has no sections
        or is longer than `whole_bill_chars` (Appropriation schedules, very long Acts: the normal pipeline handles those)."""
        rows = self.store.db.execute("SELECT id, section_no, text FROM chunks WHERE bill_key=? AND kind='section' "
                                     "AND section_no IS NOT NULL ORDER BY section_no, ord", (key,)).fetchall()
        first, texts = {}, {}
        for r in rows:
            first.setdefault(r["section_no"], r["id"])
            texts.setdefault(r["section_no"], []).append(r["text"])
        total = sum(len(t) for ts in texts.values() for t in ts)
        if not first or total > self.whole_bill_chars:
            return None
        chunks = self.store.chunks_by_ids(list(first.values()))
        return [Evidence({**chunks[i], "text": "\n".join(texts[n])}, 3) for n, i in first.items() if i in chunks]

    def bill_focus(self, question, keys, typed=True):
        """The bill version to answer from in bill-focused mode, or (None, reason).
        - The question names bill numbers that are all versions of ONE bill -> the newest one named; but when the number
          came from the condense step (`typed` False) and is a version of the bill already in focus, the focused version
          stays (same bill, and an unchanged prompt prefix keeps the cache warm).
        - A follow-up in a chat that is already about one bill -> that bill, but only if retrieval agrees: the bill's family
          must be among the top 2 families of a reciprocal-rank vote over the retrieved hits (routing top-2 was 96% on the
          72 recent eval items). Otherwise the user moved on and the normal pipeline answers.
        Questions about several bills ("which bills ...", "compare ...") never use it."""
        if MULTI_BILL.search(question):
            return None, "question is about several bills"
        if keys:
            fams = {tuple(self.family(k)) for k in keys}
            if len(fams) != 1:
                return None, "question names several bills"
            if not typed and self.focus_key and self.focus_key in fams.pop():
                return self.focus_key, None
            return max(keys, key=lambda k: (self.store.bill(k) or {}).get("date", "")), None
        if not (self.history and self.focus_key):
            return None, None
        hits = (self.retriever.search(question, k=self.k_clauses, kinds=CLAUSE_KINDS)
                + self.retriever.search(question, k=self.k_bills, kinds=("overview",)))
        vote, fam_of = {}, {}
        for rank, c in enumerate(hits):
            f = fam_of.setdefault(c["bill_key"], self.family(c["bill_key"])[0])
            vote[f] = vote.get(f, 0.0) + 1 / (rank + 3)
        top = sorted(vote, key=lambda f: -vote[f])[:2]
        if self.family(self.focus_key)[0] not in top:
            return None, "retrieval points at another bill"
        return self.focus_key, None

    def single_bill(self, evidence):
        """After a normal answer: the bill version the chat is now about, if all kept clause evidence (or, failing that,
        all bill-level evidence) comes from one bill's versions; else None."""
        clauses = [e for e in evidence if e.section is not None and e.score >= 2]
        pool = clauses or [e for e in evidence if e.score >= 2]
        if not pool or len({tuple(self.family(e.key)) for e in pool}) != 1:
            return None
        return pool[0].key

    def answer_whole_bill(self, q, key, notes, res):
        """-> True when answered (res filled), False to fall back to the normal pipeline (res.whole_bill says why)."""
        ev = self.whole_bill_evidence(key)
        if not ev:
            res.whole_bill = {"fallback": "bill has no sections or is too long", "bill": key}
            return False
        t = time.time()
        self._step("whole_bill")
        answer = self.summarise(q, ev, True, "", res, stream=True, question_last=True)
        res.seconds["whole_bill"] = time.time() - t
        info = {"bill": key, "sections": len(ev), "chars": sum(len(e.chunk["text"]) for e in ev)}
        if answer == LEAK_FALLBACK or not CITE.search(answer):  # cites nothing: maybe not this bill -> normal pipeline
            res.whole_bill = {**info, "fallback": "unusable answer" if answer == LEAK_FALLBACK else "no citations"}
            res.warnings = [w for w in res.warnings if "reasoning or unusable" not in w]
            self.on_event("whole_bill", res.whole_bill)
            if self.emit:
                self.emit("reset", None)
            return False
        cited = {(k, int(n)) for k, n in CITE.findall(answer) if n}
        res.answer = (" ".join(notes) + "\n\n" + answer) if notes else answer
        res.path, res.whole_bill, res.queries = "whole_bill", info, [q]
        res.evidence = [e for e in ev if (e.key, e.section) in cited]  # sources = what the answer cites, not all sections
        bad = self.verify(answer, ev)
        if bad:
            res.warnings.append("citations not backed by the retrieved evidence: " + ", ".join(bad))
        self.on_event("whole_bill", info)
        return True

    @staticmethod
    def verify(answer, evidence):
        """Citations in the answer that do not point at evidence we showed the model."""
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
        keys = [k for k in {normalize_ref(m) for m in re.findall(r"\d{1,3}\s*/\s*\d{4}", q)} if k and self.store.bill(k)] or None
        if self.whole_bill and local is None:  # English only so far (Sinhala: official text would triple the prefix)
            key, why = self.bill_focus(q, keys, typed=bool(re.search(r"\d{1,3}\s*/\s*\d{4}", plan["question"])))
            if key and self.answer_whole_bill(q, key, notes, res):
                self.focus_key = key
                self.history.append((question, res.answer))
                return res
            if why:
                res.whole_bill = {"fallback": why}
                self.on_event("whole_bill", res.whole_bill)
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
        if self.whole_bill:
            self.focus_key = self.single_bill(res.evidence)
        self.history.append((question, res.answer))
        return res
