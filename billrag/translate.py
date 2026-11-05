"""Translation layer: Sinhala / Tamil questions in, same-language answers out. The index and retrieval stay English-only.

Language is detected deterministically from the Unicode script (no model call, so English questions cost nothing extra).
For a non-English question the pipeline makes two extra LLM calls: question -> English before retrieval, and the finished
English answer -> the user's language. Citations like [077/2026 §12] are language-neutral and must survive translation
byte-for-byte; if they do not, we return the (verified) English answer instead of a translation that may have altered them.
"""
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


def language_name(code):
    return SCRIPTS[code][0]


def cites(text):
    return sorted(m.replace("【", "[").replace("】", "]") for m in CITE.findall(text))


def to_english(llm, text, code, effort=None):
    out = llm.chat(TO_EN, [{"role": "user", "content": text}], max_tokens=400, effort=effort).text.strip().strip('"')
    return out if out and detect(out) == "en" else None  # untranslated (or empty) output is not usable for retrieval


def from_english(llm, answer, code, effort=None):
    """-> translated answer, or None when it is unusable or altered the citations."""
    out = llm.chat(FROM_EN.format(lang=language_name(code)), [{"role": "user", "content": answer}], max_tokens=2400,
                   effort=effort).text.strip()
    if not out or detect(out) != code or cites(out) != cites(answer):
        return None
    return out
