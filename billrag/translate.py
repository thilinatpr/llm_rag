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

SCRIPTS = {"si": ("Sinhala", re.compile(r"[඀-෿]")), "ta": ("Tamil", re.compile(r"[஀-௿]"))}
CITE = re.compile(r"[\[【]\d{3}/\d{4}(?:\s*§\s*\d+(?:\([^)\]]*\))*)?[\]】]")

NO_ADD = ("Copy any bill number, year or section number exactly as the user wrote it, and NEVER add a bill number, year or "
          "bill name that the question does not contain.")
TO_EN = ("Translate the user's question about a Sri Lankan bill into clear English for a search engine. " + NO_ADD + " "
         "Use the English name of an Act or body when you know it. "
         "Reply with ONLY the English question.")
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


MODES = ("off", "script", "ai")

DETECT_AND_TRANSLATE = ("You prepare questions about Sri Lankan bills for an English-only search engine. Identify the language of "
                        "the user's question (romanised Sinhala/Tamil counts as Sinhala/Tamil). If it is not English, translate it "
                        "into clear English. " + NO_ADD + " Use the English name of an Act or body when you know it. Reply with ONLY JSON: "
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


LEAKED = re.compile(r"^\s*(analysis|assistant\s*final|we need to)\b", re.I)
RETRY = "\n\nReply with ONLY the translation - no analysis, no notes, nothing before or after it."


def _chat(llm, system, text, max_tokens, effort):
    return (llm.chat(system, [{"role": "user", "content": text}], max_tokens=max_tokens, effort=effort).text or "").strip()


def check_out(out, answer, code):
    """-> None if `out` is a usable translation of `answer` into `code`, else the reason it is not."""
    if not out or LEAKED.match(out):
        return "empty or reasoning text"
    if code in SCRIPTS and detect(out) != code:
        return f"not in {language_name(code)} script"
    if cites(out) != cites(answer):
        return "citations changed"
    return None


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

    def to_english(self, text, code):
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
                out = _chat(llm, TO_EN, text, 400, effort).strip('"')
            except Exception:  # noqa: BLE001
                continue
            if out and not LEAKED.match(out) and detect(out) == "en":
                return drop_invented_refs(text, out)
        return None

    def from_english(self, answer, code):
        """-> (translation or None, reasons for every failed attempt)."""
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
        system = FROM_EN.format(lang=language_name(code))
        for i, (llm, effort) in enumerate(self._chats()):
            for prompt in ((answer, answer + RETRY) if i == len(self._chats()) - 1 else (answer,)):  # one firmer retry on the last model
                self.calls += 1
                try:
                    out = _chat(llm, system, prompt, 2400, effort)
                    why = check_out(out, answer, code)
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
