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
from dataclasses import dataclass, field

from . import resolve, translate
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
    letters = sum(c.isalpha() for c in txt)
    return letters == 0 or (len(txt) >= 40 and letters < 0.4 * len(txt))


def strip_reasoning(txt):
    """-> (text, leaked). gpt-oss sometimes returns its reasoning ("analysis: ...") instead of, or before, the answer."""
    if not REASONING.match(txt):
        return txt, False
    m = FINAL.search(txt)
    if m and txt[m.end():].strip():
        return txt[m.end():].strip(), False
    return txt, True


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
                 translate_mode="script", translator=None):
        self.store, self.retriever, self.llm = store, retriever, llm
        self.k_clauses, self.k_bills, self.extra_rounds = k_clauses, k_bills, extra_rounds
        self.keep_max, self.evidence_chars = keep_max, evidence_chars
        self.on_event = on_event or (lambda *_: None)
        self.grade_effort, self.answer_effort = grade_effort, answer_effort
        self.answer_llm = answer_llm or llm  # the answer step may use a different model than grading
        self.translate_mode = translate_mode  # off | script | ai (see translate.py)
        self.translator = translator or translate.Translator(llm, grade_effort)  # may use a dedicated translation model
        self.history = []

    # ------------------------------------------------------------ steps
    def _llm(self, system, user, max_tokens, res, effort=None, json_out=False, llm=None):
        def call(u):
            res.llm_calls += 1
            txt = (llm or self.llm).chat(system, [{"role": "user", "content": u}], max_tokens=max_tokens, effort=effort).text
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
        return fresh

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

    def summarise(self, question, evidence, enough, missing, res):
        if not evidence:
            return "I could not find anything in the indexed bills that answers this question."
        blocks = []
        for e in evidence:
            c = e.chunk
            label = f"[{e.key} §{e.section}]" if e.section is not None else f"[{e.key}]"
            kind = "bill-level description" if c["kind"] == "overview" else f"p.{c['page']}"
            also = f" (identical text also in {', '.join(c['also_in'])})" if c.get("also_in") else ""
            blocks.append(f"{label} {c['title']} ({c['bill_kind']}, {c['date'][:10]}), {kind}{also}\n{c['text']}")
        note = "" if enough else (f"\n\nNOTE: the grader judged this evidence INCOMPLETE. Missing: {missing or 'unspecified'}. "
                                   "Answer what the evidence supports and state clearly what is missing.")
        user = f"QUESTION: {question}\n\nEVIDENCE:\n\n" + "\n\n".join(blocks) + note
        return self._llm(ANSWER_SYSTEM, user, 1800, res, self.answer_effort, llm=self.answer_llm)

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
        tr.calls = 0
        if mode != "off":
            lang = translate.detect(question)  # Sinhala / Tamil by script: free
            if lang != "en":
                q_en = tr.to_english(question, lang)
            elif mode == "ai":  # any other language (or romanised Sinhala/Tamil): one call detects and translates
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
        res = self._ask(q_en)
        res.question, res.lang, res.answer_en = question, lang, res.answer
        res.seconds["translate_in"] = t_in
        t = time.time()
        refs = self.official_texts(res.evidence, lang)
        out, reasons = tr.from_english(res.answer_en, lang, [(f"{k} \u00a7{n}", t) for k, n, t in refs])
        res.official = [(k, n) for k, n, _ in refs]
        res.seconds["translate_out"] = time.time() - t
        res.llm_calls += tr.calls
        if out:
            res.answer = out
        else:
            res.warnings.append(f"could not translate the answer into {name} reliably ({'; '.join(reasons) or 'no output'}); "
                                "showing English")
        return res

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
                    out.append((e.key, e.section, row["text"]))
                    break
        return out

    def _ask(self, question: str) -> Result:
        res = Result(question=question, answer="")
        plan = resolve.resolve_question(self.store, question)
        if plan["action"] == "reply":  # greeting, or an abbreviation we cannot ground: no search, no model call
            res.answer = plan["text"]
            self.on_event("resolve", {"reply": True})
            self.history.append((question, res.answer))
            return res
        notes = plan["notes"]
        if notes:
            self.on_event("resolve", {"notes": notes})
        t = time.time()
        q = self.condense(plan["question"], res)
        res.seconds["condense"] = time.time() - t
        keys = [k for k in {normalize_ref(m) for m in re.findall(r"\d{1,3}\s*/\s*\d{4}", q)} if k and self.store.bill(k)] or None
        seen_ids, graded, queries = set(), [], [q]
        enough, missing = True, ""
        for rnd in range(1 + self.extra_rounds):
            t = time.time()
            cands = []
            for qq in queries:
                cands += self.retrieve(qq, seen_ids, keys)
            res.seconds[f"retrieve{rnd}"] = time.time() - t
            self.on_event("retrieve", {"queries": queries, "candidates": len(cands)})
            if not cands:
                break
            t = time.time()
            scores, enough, missing, more = self.evaluate(q, cands, res)
            res.seconds[f"evaluate{rnd}"] = time.time() - t
            graded += [Evidence(c, scores.get(i, 0)) for i, c in enumerate(cands)]
            self.on_event("evaluate", {"kept": sum(1 for e in graded if e.score >= 2), "enough": enough, "missing": missing})
            if enough or not more or rnd == self.extra_rounds:
                break
            queries = more
            res.queries += more
        res.enough, res.queries, res.graded = enough, [q] + res.queries, graded
        res.evidence = self.build_evidence(graded)
        t = time.time()
        res.answer = self.summarise(q, res.evidence, enough, missing, res)
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
