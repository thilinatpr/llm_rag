"""Translation layer: questions in any language in, same-language answers out. The index and retrieval stay English-only.

Switch (BILLRAG_TRANSLATE / Pipeline(translate_mode=...)):
  off     never translate.
  script  (default) Sinhala/Tamil are recognised from their Unicode script - no model call, so English costs nothing extra.
  ai      like script, but any other question first goes through ONE model call that names its language and, if it is not
          English, translates it (+1 call on every non-Sinhala/Tamil question, English included). Covers any language and
          romanised Sinhala/Tamil.
For a non-English question the pipeline then makes one more call: the finished English answer -> the user's language. Citations like [077/2026 §12] are language-neutral and must survive translation
byte-for-byte; if they do not, we return the (verified) English answer instead of a translation that may have altered them.
"""
import json
import re

SCRIPTS = {"si": ("Sinhala", re.compile(r"[඀-෿]")), "ta": ("Tamil", re.compile(r"[஀-௿]"))}
CITE = re.compile(r"[\[【]\d{3}/\d{4}(?:\s*§\s*\d+(?:\([^)\]]*\))*)?[\]】]")

TO_EN = ("Translate the user's question about a Sri Lankan bill into clear English for a search engine. Keep bill numbers "
         "(like 077/2026), years and section numbers exactly. Use the English name of an Act or body when you know it. "
         "Reply with ONLY the English question.")
FROM_EN = ("Translate the answer below into {lang}. Keep every citation in square brackets (like [077/2026 §12(2)]) EXACTLY as "
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
                        "into clear English: keep bill numbers (like 077/2026), years and section numbers exactly, and use the "
                        "English name of an Act or body when you know it. Reply with ONLY JSON: "
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
                return ("en", text) if lang in ("english", "en") else (code, eng)
    return None


def cites(text):
    return sorted(m.replace("【", "[").replace("】", "]") for m in CITE.findall(text))


def to_english(llm, text, code, effort=None):
    out = llm.chat(TO_EN, [{"role": "user", "content": text}], max_tokens=400, effort=effort).text.strip().strip('"')
    return out if out and detect(out) == "en" else None  # untranslated (or empty) output is not usable for retrieval


def from_english(llm, answer, code, effort=None):
    """-> translated answer, or None when it is unusable or altered the citations."""
    out = llm.chat(FROM_EN.format(lang=language_name(code)), [{"role": "user", "content": answer}], max_tokens=2400,
                   effort=effort).text.strip()
    if not out or (code in SCRIPTS and detect(out) != code) or cites(out) != cites(answer):
        return None
    return out
