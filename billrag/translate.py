"""Translation layer: questions in any language in, same-language answers out. The index and retrieval stay English-only.

Switch (BILLRAG_TRANSLATE / Pipeline(translate_mode=...)):
  off     never translate.
  script  (default) Sinhala/Tamil are recognised from their Unicode script - no model call, so English costs nothing extra.
  ai      like script, but any other question first goes through ONE model call that names its language and, if it is not
          English, translates it (+1 call on every non-Sinhala/Tamil question, English included). Covers any language and
          romanised Sinhala/Tamil.
For a non-English question the pipeline then makes one more call: the finished English answer -> the user's language.
Citations like [077/2026 §12] are language-neutral and must survive translation byte-for-byte; if they do not, we return
the (verified) English answer instead of a translation that may have altered them.

Translation model (BILLRAG_TRANSLATE_MODEL, optional; default = the main chat model):
  a chat model on the same provider/endpoint, e.g. @cf/google/gemma-3-12b-it or @cf/meta/llama-3.3-70b-instruct-fp8-fast
  (instruction-following, no reasoning step), or
  a dedicated machine-translation model whose id contains "m2m100", e.g. @cf/meta/m2m100-1.2b, called through Workers AI
  /ai/run. Citations are cut out and re-inserted verbatim around it, so it cannot alter them.
Whatever is used, a failed translation falls back to the main chat model once, and the reason is reported.
"""
import json
import re
import unicodedata

SCRIPTS = {"si": ("Sinhala", re.compile(r"[඀-෿]")), "ta": ("Tamil", re.compile(r"[஀-௿]"))}
CITE = re.compile(r"[\[【]\d{3}/\d{4}(?:\s*§\s*\d+(?:\([^)\]]*\))*)?[\]】]")

# Translate literally. "Use the English name of an Act when you know it" made the model swap "terrorism act" for the real-world
# "Prevention of Terrorism Act" (not in the corpus), and the answer then hedged about an Act the user never named (3 of 4 live
# runs; 0 of 4 with this wording, same correct penalty in all 8).
LITERAL = ("Translate literally: use the ordinary English words for what the question says (for example 'terrorism act', not a "
           "specific real-world Act's title). Only write the exact title of an Act or body when the question itself clearly "
           "names it and you are sure of the title.")
NO_ADD = ("Copy any bill number, year or section number exactly as the user wrote it, and NEVER add a bill number, year or "
          "bill name that the question does not contain.")
TO_EN = ("Translate the user's question about a Sri Lankan bill into clear English for a search engine. " + NO_ADD + " "
         + LITERAL + " Reply with ONLY the English question.")
FROM_EN = ("Translate the answer below into {lang}. Keep every citation in square brackets (like [NNN/YYYY §N]) EXACTLY as "
           "written, in ASCII brackets, untranslated. Keep bill titles, Act names and numbers in English, followed by a {lang} "
           "gloss in parentheses only if it helps. Do not add, remove or soften any statement. Reply with ONLY the translation.")


def detect(text):
    """-> 'si' | 'ta' | 'en'. A script wins when it makes up a meaningful share of the letters."""
    letters = sum(c.isalpha() for c in text) or 1
    for code, (_, rx) in SCRIPTS.items():
        if len(rx.findall(text)) >= max(3, 0.2 * letters):
            return code
    return "en"


# ---- "answer that in Tamil" follow-ups ---------------------------------------------------------------------------------
# A short message that only names a language (optionally with filler like "can you say this in ...") asks for the PREVIOUS answer
# in that language. Without this it was searched as a new question (live: "In tamil" after a budget answer searched "can you say
# this in Tamil?" and answered about the "Sinhala text shall prevail" clause). Anything else in the message (a bill, a topic) means
# it is a real question: "tamil bill", "is english an official language" are not switches.
LANG_WORDS = {
    "en": {"english", "inglish", "ingrisi", "ingreesi", "\u0d89\u0d82\u0d9c\u0dca\u200d\u0dbb\u0dd3\u0dc3\u0dd2",
           "\u0d89\u0d82\u0d9c\u0dca\u200d\u0dbb\u0dd3\u0dc3\u0dd2\u0dba\u0dd9\u0db1\u0dca",
           "\u0b86\u0b99\u0bcd\u0b95\u0bbf\u0bb2\u0bae\u0bcd", "\u0b86\u0b99\u0bcd\u0b95\u0bbf\u0bb2\u0ba4\u0bcd\u0ba4\u0bbf\u0bb2\u0bcd"},
    "si": {"sinhala", "sinhalese", "sinhalen", "sinhalenma", "singhala", "\u0dc3\u0dd2\u0d82\u0dc4\u0dbd", "\u0dc3\u0dd2\u0d82\u0dc4\u0dbd\u0dd9\u0db1\u0dca",
           "\u0dc3\u0dd2\u0d82\u0dc4\u0dbd\u0dd9\u0db1\u0dca\u0db8", "\u0b9a\u0bbf\u0b99\u0bcd\u0b95\u0bb3\u0bae\u0bcd", "\u0b9a\u0bbf\u0b99\u0bcd\u0b95\u0bb3\u0ba4\u0bcd\u0ba4\u0bbf\u0bb2\u0bcd"},
    "ta": {"tamil", "tamilil", "thamil", "demala", "demalen", "\u0daf\u0dd9\u0db8\u0dc5", "\u0daf\u0dd9\u0db8\u0dc5\u0dd9\u0db1\u0dca",
           "\u0daf\u0dd9\u0db8\u0dc5\u0dd9\u0db1\u0dca\u0db8", "\u0ba4\u0bae\u0bbf\u0bb4\u0bcd", "\u0ba4\u0bae\u0bbf\u0bb4\u0bbf\u0bb2\u0bcd", "\u0ba4\u0bae\u0bbf\u0bb4\u0bbf\u0bb2\u0bc1\u0bae\u0bcd"},
}
SWITCH_FILLER = {
    "in", "into", "to", "say", "tell", "answer", "reply", "give", "write", "me", "it", "this", "that", "please", "pls", "plz",
    "again", "also", "translate", "can", "could", "you", "the", "same", "version", "mata", "eka", "denna", "kiyanna", "kiyala",
    "puluwanda", "puluwan", "once", "more",
    "\u0d9a\u0dd2\u0dba\u0db1\u0dca\u0db1", "\u0daf\u0dd9\u0db1\u0dca\u0db1", "\u0db4\u0dd2\u0dc5\u0dd2\u0dad\u0dd4\u0dbb", "\u0db4\u0dd4\u0dc5\u0dd4\u0dc0\u0db1\u0dca\u0daf",
    "\u0db8\u0da7", "\u0db8\u0dd9\u0dba", "\u0d91\u0dba", "\u0d9a\u0dbb\u0db1\u0dca\u0db1", "\u0db4\u0dbb\u0dd2\u0dc0\u0dbb\u0dca\u0dad\u0db1\u0dba",
    "\u0b9a\u0bca\u0bb2\u0bcd\u0bb2\u0bc1\u0b99\u0bcd\u0b95\u0bb3\u0bcd", "\u0b9a\u0bca\u0bb2\u0bcd\u0bb2\u0bc1", "\u0ba4\u0bbe\u0bb0\u0bc1\u0b99\u0bcd\u0b95\u0bb3\u0bcd",
    "\u0ba4\u0bb0\u0bb5\u0bc1\u0bae\u0bcd", "\u0baa\u0ba4\u0bbf\u0bb2\u0bcd", "\u0b87\u0ba4\u0bc8", "\u0ba4\u0baf\u0bb5\u0bc1\u0b9a\u0bc6\u0baf\u0bcd\u0ba4\u0bc1",
    "\u0bae\u0bc1\u0b9f\u0bbf\u0baf\u0bc1\u0bae\u0bbe", "\u0bae\u0bca\u0bb4\u0bbf\u0baa\u0bc6\u0baf\u0bb0\u0bcd\u0b95\u0bcd\u0b95\u0bb5\u0bc1\u0bae\u0bcd",
}
_WORD = re.compile(r"[a-z0-9\u0b80-\u0bff\u0d80-\u0dff\u200d]+")


def language_switch(text):
    """-> 'en' | 'si' | 'ta' when `text` only asks for the previous answer in that language, else None."""
    words = _WORD.findall((text or "").lower())
    if not words or len(words) > 8:
        return None
    named = {code for code, names in LANG_WORDS.items() for w in words if w in names}
    if len(named) != 1:
        return None
    rest = [w for w in words if not any(w in names for names in LANG_WORDS.values())]
    return named.pop() if all(w in SWITCH_FILLER for w in rest) else None


# Shown under answers that are a machine translation of the English answer (not written from the bill's own text). Written by the
# assistant, NOT reviewed by a speaker yet. Never shown for English answers or for answers built from official text.
MACHINE_NOTE = {
    "ta": "\u0b87\u0ba4\u0bc1 \u0b86\u0b99\u0bcd\u0b95\u0bbf\u0bb2\u0baa\u0bcd \u0baa\u0ba4\u0bbf\u0bb2\u0bbf\u0bb2\u0bbf\u0bb0\u0bc1\u0ba8\u0bcd\u0ba4\u0bc1 \u0b87\u0baf\u0ba8\u0bcd\u0ba4\u0bbf\u0bb0\u0bae\u0bcd \u0bae\u0bc2\u0bb2\u0bae\u0bcd \u0bae\u0bca\u0bb4\u0bbf\u0baa\u0bc6\u0baf\u0bb0\u0bcd\u0b95\u0bcd\u0b95\u0baa\u0bcd\u0baa\u0b9f\u0bcd\u0b9f\u0ba4\u0bc1. "
          "\u0b9a\u0bb0\u0bbf\u0baf\u0bbe\u0ba9 \u0b9a\u0bca\u0bb1\u0bcd\u0b95\u0bb3\u0bc1\u0b95\u0bcd\u0b95\u0bc1 \u0bae\u0b9a\u0bcb\u0ba4\u0bbe\u0bb5\u0bbf\u0ba9\u0bcd \u0b85\u0ba4\u0bbf\u0b95\u0bbe\u0bb0\u0baa\u0bcd\u0baa\u0bc2\u0bb0\u0bcd\u0bb5 \u0b89\u0bb0\u0bc8\u0baf\u0bc8\u0baa\u0bcd \u0baa\u0bbe\u0bb0\u0bcd\u0b95\u0bcd\u0b95\u0bb5\u0bc1\u0bae\u0bcd.",
    "si": "\u0db8\u0dd9\u0dba \u0d89\u0d82\u0d9c\u0dca\u200d\u0dbb\u0dd3\u0dc3\u0dd2 \u0db4\u0dd2\u0dc5\u0dd2\u0dad\u0dd4\u0dbb\u0d9a\u0dd2\u0db1\u0dca \u0dba\u0db1\u0dca\u0dad\u0dca\u200d\u0dbb\u0dba\u0dd9\u0db1\u0dca \u0db4\u0dbb\u0dd2\u0dc0\u0dbb\u0dca\u0dad\u0db1\u0dba \u0d9a\u0dbb\u0db1 \u0dbd\u0daf\u0dca\u0daf\u0d9a\u0dd2. "
          "\u0db1\u0dd2\u0dc0\u0dd0\u0dbb\u0daf\u0dd2 \u0dc0\u0da0\u0db1 \u0dc3\u0db3\u0dc4\u0dcf \u0db4\u0db1\u0dad\u0dca \u0d9a\u0dd9\u0da7\u0dd4\u0db8\u0dca\u0db4\u0dad\u0dda \u0db1\u0dd2\u0dbd \u0db4\u0dcf\u0da8\u0dba \u0db6\u0dbd\u0db1\u0dca\u0db1.",
}


def machine_note(lang, path, answer):
    """Text to show under a machine-translated answer, else None. `answer` must really be in `lang` (a failed translation shows
    English and needs no note)."""
    if path in ("translated", "language_switch") and lang in MACHINE_NOTE and detect(answer or "") == lang:
        return MACHINE_NOTE[lang]
    return None


MODES = ("off", "script", "ai")

DETECT_AND_TRANSLATE = ("You prepare questions about Sri Lankan bills for an English-only search engine. Identify the language of "
                        "the user's question (romanised Sinhala/Tamil counts as Sinhala/Tamil). If it is not English, translate it "
                        "into clear English. " + NO_ADD + " " + LITERAL + " Reply with ONLY JSON: "
                        '{"language": "<English name of the language>", "english": "<the question in English>"}')


def language_name(code):
    return SCRIPTS[code][0] if code in SCRIPTS else code.title()


def detect_ai(llm, text, effort=None):
    """One model call -> (language code, English question). ('en', text) for English; None if the reply is unusable."""
    for _ in range(2):  # reasoning models sometimes answer in prose: retry once, then give up
        raw = llm.chat(DETECT_AND_TRANSLATE, [{"role": "user", "content": text}], max_tokens=500, effort=effort).text
        dec = json.JSONDecoder()
        for m in re.finditer(r"\{", raw or ""):
            try:
                obj, _end = dec.raw_decode(raw[m.start():])
            except json.JSONDecodeError:
                continue
            lang, eng = str(obj.get("language", "")).strip().lower(), str(obj.get("english", "")).strip()
            if isinstance(obj, dict) and lang and eng:
                code = {v[0].lower(): k for k, v in SCRIPTS.items()}.get(lang, lang)
                return ("en", text) if lang in ("english", "en") else (code, drop_invented_refs(text, eng))
    return None


REF = re.compile(r"\b\d{1,3}\s*/\s*\d{4}\b")


def drop_invented_refs(src, out):
    """Remove bill numbers (NNN/YYYY) the translation added: a bill number in the question restricts retrieval to that bill,
    and models copy numbers from examples (Llama added '077/2026' to a question that named no bill)."""
    have = {re.sub(r"\s", "", m) for m in REF.findall(src)}
    for m in REF.findall(out):
        if re.sub(r"\s", "", m) not in have:
            out = re.sub(r"\s*(?:\bno\.?\s*)?" + re.escape(m), "", out, flags=re.I)
    return re.sub(r"\s+([?.,])", r"\1", out).strip()


def cites(text):
    return sorted(m.replace("【", "[").replace("】", "]") for m in CITE.findall(text))


OFFICIAL_NOTE = (" After the answer you are given the OFFICIAL {lang} TEXT of the bill sections it relies on, published with "
                 "the bill. Use that text's exact wording for legal terms and names, and when the answer quotes a clause, quote "
                 "the official {lang} text instead of translating the English quote. Translate ONLY the answer: do not add "
                 "anything from the reference that the answer does not say, and do not reproduce the reference labels.")


def official_block(official, code, max_chars=2500, max_items=4):
    """Reference block appended after the answer. Labels carry no [ ] so they can never pass as citations."""
    if not official:
        return ""
    parts = [f"{label.strip('[]')}:\n{text[:max_chars]}" for label, text in list(official)[:max_items]]
    return (f"\n\n=== OFFICIAL {language_name(code).upper()} TEXT (reference only; do not translate or copy this block) ===\n"
            + "\n\n".join(parts))


LEAKED = re.compile(r"^\s*(analysis|assistant\s*final|we need to)\b", re.I)
RETRY = "\n\nReply with ONLY the translation - no analysis, no notes, nothing before or after it."


def _chat(llm, system, text, max_tokens, effort, want_cut=False):
    r = llm.chat(system, [{"role": "user", "content": text}], max_tokens=max_tokens, effort=effort)
    out = (r.text or "").strip()
    return (out, getattr(r, "truncated", False)) if want_cut else out


# Degenerate model output (seen live in a Llama 3.3 Sinhala answer: "ආන්ඩ්‍රාත්‍රාත්‍රාත්‍රාය"): a short unit that contains a
# letter repeated 3+ times in a row, one letter/sign 5+ times, or a "word" far longer than any real one. Thresholds were
# checked against all 3,456 official Sinhala sections (no hits), see AGENTS.md.
REPEAT_UNIT = re.compile(r"(\S{2,12}?)\1{2,}")
REPEAT_CHAR = re.compile(r"(\S)\1{4,}")
MAX_TOKEN = 45
# Letters of other scripts in a Sinhala/Tamil answer, incl. accented Latin (a live answer had "උදාහරණként", Hungarian -ként).
# Plain ASCII stays allowed: bill titles and Act names are kept in English.
FOREIGN = re.compile(r"[\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff\u0900-\u0d7f\u0e00-\u0fff\u1100-\u11ff"
                     r"\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0600-\u06ff\u0400-\u04ff]")


def garbled(text):
    """-> None, or why `text` looks like degenerate model output rather than prose (any language)."""
    body = CITE.sub(" ", text or "")
    body = re.sub(r"https?://\S+", " ", body)
    for m in REPEAT_UNIT.finditer(body):
        if any(c.isalpha() for c in m.group(1)):
            return f"repeated text {m.group(0)[:40]!r}"
    for m in REPEAT_CHAR.finditer(body):
        if m.group(1).isalpha() or unicodedata.category(m.group(1)) in ("Mn", "Mc"):
            return f"repeated character {m.group(0)[:20]!r}"
    bad = [c for c in body if unicodedata.category(c) in ("Cn", "Co")]  # unassigned / private-use code points
    if bad:
        return f"invalid characters ({' '.join(f'U+{ord(c):04X}' for c in bad[:3])})"
    for tok in re.split(r"[\s/|]+", body):
        if len(tok.strip(".,;:()[]*\"'-")) > MAX_TOKEN:
            return f"overlong word {tok[:30]!r}..."
    return None


# The glossary block ("OFFICIAL SINHALA TERMS ...\n- Board = ...") is prompt material. A model sometimes copies it into its
# answer (live: appended, translated, to a Sinhala answer), so an answer that contains the header or 2+ "English term = term"
# list lines is rejected like any other unusable output.
TERM_ECHO = re.compile(r"^\s*[-\u2022*]\s*[A-Za-z][A-Za-z ()'/,-]{1,60}?\s=\s+\S", re.M)
TERM_HEADER = re.compile(r"OFFICIAL\s+\w+\s+(TERMS|NAMES)|OFFICIAL\s+\w+\s+TEXT\s*\(", re.I)


def echoes_prompt(out):
    return bool(TERM_HEADER.search(out or "")) or len(TERM_ECHO.findall(out or "")) >= 2


def check_script(out, code):
    """Script-level checks for text that should be in language `code`: -> None or the reason it is unusable."""
    if not out or LEAKED.match(out):
        return "empty or reasoning text"
    if echoes_prompt(out):
        return "copied the glossary / reference list from the prompt"
    if code in SCRIPTS and detect(out) != code:
        return f"not in {language_name(code)} script"
    if code in SCRIPTS:
        other = [c for c in FOREIGN.findall(out) if not SCRIPTS[code][1].match(c)]
        if other:
            return f"characters from another script ({''.join(other[:5])})"
    return garbled(out)


def check_out(out, answer, code):
    """-> None if `out` is a usable translation of `answer` into `code`, else the reason it is not."""
    why = check_script(out, code)
    if why:
        return why
    if cites(out) != cites(answer):
        return "citations changed"
    return None


QUOTE = re.compile(r"[“\"]([^”\"\n]{15,600})[”\"]")


def fix_quotes(text, refs, min_ratio=0.93, code=None):
    """-> (text, fixes). A quotation that is ALMOST a passage of the official text (gpt-oss spells ථ්‍රස්තවාදී for
    ත්‍රස්තවාදී even when copying) is replaced by that passage: what is quoted should be the official wording, so swapping
    in the official words cannot add content. Exact quotes are left alone.
    With `code` (the answer's language): a quote in that language that matches NO official passage loses its quotation marks
    (fixes entry (quote, None)) - quotation marks claim the bill's own wording, and live answers put paraphrases of the
    auto-generated summary in quotes. Quotes in other scripts (English titles) are never touched."""
    import difflib
    words = [w for r in refs for w in ([None] + r.split())]  # None separates documents: no window spans two
    joined = [" ".join(r.split()) for r in refs]
    fixes = []

    def fix(m):
        inner, unmatched = m.group(1), []
        parts = re.split(r"(\s*(?:…|\.\.\.)\s*)", inner)  # "a … b": each piece is a quotation of its own
        out = "".join(piece if i % 2 or len(piece.strip()) < 15 else one(piece, unmatched) for i, piece in enumerate(parts))
        if code and unmatched and detect(inner) == code:
            fixes.append((" ".join(inner.split())[:60], None))
            return out.strip(" ….")
        return m.group(0)[0] + out + m.group(0)[-1]

    def one(piece, unmatched):
        q = " ".join(piece.split())
        if any(q in j for j in joined):
            return piece
        n, best = len(q.split()), (0.0, None)
        for i in range(len(words) - n + 1):
            win = words[i:i + n]
            if None in win:
                continue
            cand = " ".join(win)
            sm = difflib.SequenceMatcher(None, q, cand, autojunk=False)
            if sm.real_quick_ratio() >= min_ratio and sm.quick_ratio() >= min_ratio:
                r = sm.ratio()
                if r > best[0]:
                    best = (r, cand)
        if best[0] >= min_ratio:
            fixes.append((q[:60], best[1][:60]))
            return best[1].strip(".,;:")
        unmatched.append(q)
        return piece

    return QUOTE.sub(fix, text), fixes


class M2M:
    """Dedicated machine-translation model on Workers AI (e.g. @cf/meta/m2m100-1.2b): {text, source_lang, target_lang}."""

    def __init__(self, model, account, token, client=None):
        from .llm import _Cloudflare
        self.model, self.cf = model, client or _Cloudflare(account, token)

    def translate(self, text, src, tgt):
        if not any(c.isalpha() for c in text):
            return text
        return (self.cf._post(self.model, {"text": text, "source_lang": src, "target_lang": tgt}) or {}).get("translated_text", "")

    def translate_keeping_cites(self, text, src, tgt):
        """Translate line by line; citations, bullets and bold markers are kept out of the model's hands."""
        out = []
        for line in text.split("\n"):
            m = re.match(r"^(\s*(?:[-*\u2022]|\d+\.)?\s*)(.*)$", line)
            lead, body = m.group(1), m.group(2).replace("**", "")
            parts, last = [], 0
            for c in CITE.finditer(body):
                parts += [self.translate(body[last:c.start()], src, tgt).strip(), c.group(0).replace("\u3010", "[").replace("\u3011", "]")]
                last = c.end()
            parts.append(self.translate(body[last:], src, tgt).strip())
            out.append(lead + re.sub(r"\s+([.,;:!?)])", r"\1", " ".join(p for p in parts if p)))
        return "\n".join(out)


def lang_arg(code):
    return code if code in SCRIPTS or code == "en" else code.lower()  # m2m100 takes codes or English language names


class Translator:
    """to_english / from_english / detect_ai, with an optional dedicated translation model and a fallback to the main model."""

    def __init__(self, llm, effort=None, model_llm=None, mt=None):
        self.llm, self.effort = llm, effort       # main chat model (fallback, and detect_ai when only an MT model is set)
        self.model_llm, self.mt = model_llm, mt   # BILLRAG_TRANSLATE_MODEL: a chat model, or an M2M machine-translation model
        self.calls = 0

    def _chats(self):
        """(llm, effort) to try in order: the translation chat model (no reasoning effort) then the main model."""
        return ([(self.model_llm, None)] if self.model_llm else []) + [(self.llm, self.effort)]

    def detect_ai(self, text):
        for llm, effort in self._chats():
            self.calls += 1
            try:
                got = detect_ai(llm, text, effort)
            except Exception:  # noqa: BLE001 - a translation-model outage must not break the chat
                got = None
            if got:
                return got
        return None

    def to_english(self, text, code, hints=()):
        """hints: [(local phrase, official English name)] from the glossary for phrases in the question (chat models only)."""
        prompt = text + ("\n\nOFFICIAL ENGLISH NAMES of phrases in this question (use them exactly; reference only, do not "
                         "translate this list):\n" + "\n".join(f"- {loc} = {en}" for loc, en in hints) if hints else "")
        if self.mt:
            self.calls += 1
            try:
                out = drop_invented_refs(text, self.mt.translate(text, lang_arg(code), "en"))
                if out and detect(out) == "en":
                    return out
            except Exception:  # noqa: BLE001
                pass
        for llm, effort in self._chats():
            self.calls += 1
            try:
                out = _chat(llm, TO_EN, prompt, 400, effort).strip('"')
            except Exception:  # noqa: BLE001
                continue
            if out and not LEAKED.match(out) and detect(out) == "en":
                return drop_invented_refs(text, out)
        return None

    def from_english(self, answer, code, official=(), terms=""):
        """-> (translation or None, reasons for every failed attempt).

        official: [(label, text)] the bill's own text in the target language for the sections the answer relies on (see
        parse_si); chat models get it as a terminology/quote reference, the MT model ignores it."""
        reasons = []
        if self.mt:
            self.calls += 1
            try:
                out = self.mt.translate_keeping_cites(answer, "en", lang_arg(code)).strip()
                why = check_out(out, answer, code)
            except Exception as e:  # noqa: BLE001
                out, why = "", f"error: {e}"[:160]
            if not why:
                return out, reasons
            reasons.append(f"{self.mt.model}: {why}")
        system = FROM_EN.format(lang=language_name(code)) + (OFFICIAL_NOTE.format(lang=language_name(code)) if official else "")
        answer_in = answer + official_block(official, code) + terms  # terms: glossary.terms_block(...)
        for i, (llm, effort) in enumerate(self._chats()):
            for prompt in ((answer_in, answer_in + RETRY) if i == len(self._chats()) - 1 else (answer_in,)):  # one firmer retry on the last model
                self.calls += 1
                try:
                    out, cut = _chat(llm, system, prompt, 4000, effort, want_cut=True)  # Sinhala: several tokens a word
                    why = "cut off at the output limit" if cut else check_out(out, answer, code)
                except Exception as e:  # noqa: BLE001
                    out, why = "", f"error: {e}"[:160]
                if not why:
                    return out, reasons
                reasons.append(f"{getattr(llm, 'model', 'model')}: {why}")
        return None, reasons


def make_translator(settings, llm):
    """Translator for the pipeline from Settings (BILLRAG_TRANSLATE_MODEL)."""
    tm = getattr(settings, "translate_model", None)
    effort = getattr(settings, "grade_effort", None)
    if not tm or tm == settings.chat_model:
        return Translator(llm, effort)
    if "m2m100" in tm.lower():
        return Translator(llm, effort, mt=M2M(tm, settings.cf_account_id, settings.cf_api_token))
    import dataclasses
    from .llm import make_llm
    return Translator(llm, effort, model_llm=make_llm(dataclasses.replace(settings, chat_model=tm)))


def to_english(llm, text, code, effort=None):
    return Translator(llm, effort).to_english(text, code)


def from_english(llm, answer, code, effort=None):
    return Translator(llm, effort).from_english(answer, code)[0]
