"""English <-> official Sinhala glossary, mined from the bills' own text (no model calls).

The official Sinhala sections (`local_sections`, see parse_si) are linked one-to-one with the English sections, so each
pair of sections is a small parallel text. Three kinds of entry are taken from it deterministically:

  title     each bill's short-title section: "This Act may be cited as the X Act" <-> "... දරන Y පනත යනුවෙන් හඳුන්වනු ලැබේ"
  defined   interpretation sections: '"X" means ...' <-> '"Y" යන්නෙන් ...', paired in order, only when both sides of a
            section have the same number of definitions
  referred  "(hereinafter referred to as the "X")" <-> '"Y" යනුවෙන් සඳහන් කරනු ලබන', same pairing rule

Uses: (1) a Sinhala question that names a bill or a defined term gets the official English name as a hint for the
question -> English step (Llama once rendered "ත්‍රස්තවාදයෙන් රාජ්‍යය ආරක්ෂා කිරීමේ" as "Prevention of Terrorism Act"); (2) the
Sinhala answer prompts get the official Sinhala terms for English terms that occur in the evidence - also for bills that
have no linked Sinhala text, because legal terms recur across bills.

Known artefact: a font in 68 bills emits ය after ්‍ය once too often (අමාත්‍යයවරයා for අමාත්‍යවරයා), and the raw PDF text cannot tell
it apart from a real ්‍යය (රාජ්‍යය). For glossary terms only, the spelling found in the bills without the artefact wins
(`fix_double_ya`); each such change is recorded in `note`.
"""
import json
import re
from collections import Counter, defaultdict

from . import parse_si

Q, QE, T = "[“\"‘]", "[”\"’]", "([^”\"“’\\n]{2,90}?)"
PATTERNS = {
    "defined": (re.compile(rf"{Q}{T}{QE}\s*,?\s*(?:in relation to [^,;]{{1,60}},\s*)?(?:means|includes|shall mean|"
                           r"shall include|has the (?:same )?meaning|shall have the (?:same )?meaning)\b"),
                re.compile(rf"{Q}{T}{QE}\s*(?:යන්නෙන්|යන්නට)")),
    "referred": (re.compile(rf"referred to as (?:the |an? )?{Q}{T}{QE}"),
                 re.compile(rf"{Q}{T}{QE}\s*යනුවෙන්\s+(?:සඳහන්|හැඳින්)")),
}
EN_TITLE = re.compile(r"cited as (?:the )+(.+?\b(?:Act|Ordinance|Law|Code|Amendment to the Constitution))\s*,?\s*"
                      r"(?:No\b|of\b|\d|\.|$)", re.I)
SI_TITLE = re.compile(r"(?:මේ|මෙම) පනත\s+(?:\d{4}\s*අංක\s*[\d.…]*\s*දරන\s+)?(.+?)\s+(?:යනුවෙන්|ලෙස)\s+(?:හ\S*[ඳද]ුන්|හැ[ඳද]ින්)")
ARTEFACT = ("අමාත්‍යයවරයා", "ජාත්‍යයන්තර", "ව්‍යයවස්ථා")  # present only in text from the font that doubles ය after ්‍ය
WORD = re.compile(r"[඀-෿‍]+")
EN_SUFFIX = re.compile(r"\s+(?:Act|Ordinance|Law|Code)$")
SI_SUFFIX = re.compile(r"\s*(?:පනත|ආඥාපනත|නීතිය)$")


def norm(text):
    """One line, single spaces, the line-wrap repair of parse_si.join_marks, no stray quote marks, no stray ZWJ (a ZWJ
    belongs only after the virama, in conjuncts like ්‍ර), no space inside a broken word ("යනු‍වෙ න්")."""
    t = re.sub(r"(?<!\u0dca)\u200d", "", parse_si.join_marks(text or ""))
    t = re.sub(r"(?<=[\u0d80-\u0dff]) (?=[\u0dca-\u0ddf])", "", t)  # a vowel sign never starts a word
    return " ".join(t.split()).replace("යනුවෙ න්", "යනුවෙන්").strip(" “”\"‘’,;:")


def title_pair(en_s1, si_s1):
    """-> (English title, Sinhala title) from the two short-title sections, or None."""
    e, s = EN_TITLE.search(norm(en_s1)), SI_TITLE.search(norm(si_s1))
    if not (e and s):
        return None
    en = e.group(1).strip()
    if en == en.lower():  # appropriation bills are printed in lower case
        en = " ".join(w if w in ("of", "and", "the", "to") else w.capitalize() for w in en.split())
    return en, s.group(1).strip()


def term_pairs(en_text, si_text):
    """-> [(kind, en, si)] defined / referred-to terms of one section, paired in order (equal counts only)."""
    en, si, out = norm(en_text), norm(si_text), []
    for kind, (pe, ps) in PATTERNS.items():
        e, s = [norm(x) for x in pe.findall(en)], [norm(x) for x in ps.findall(si)]
        if e and len(e) == len(s):
            out += [(kind, a, b) for a, b in zip(e, s) if a and b]
    return out


def fix_double_ya(term, clean_words):
    """අමාත්‍යයවරයා -> අමාත්‍යවරයා when only the single-ය spelling occurs in bills without the font artefact."""
    def fix(m):
        w = m.group(0)
        alt = w.replace("්‍යය", "්‍ය")
        return alt if alt != w and alt in clean_words and w not in clean_words else w
    return WORD.sub(fix, term)


def build(store, lang="si"):
    """Mine the glossary from the linked sections and store it. -> stats dict."""
    rows = store.db.execute("SELECT bill_key, section_no, text FROM local_sections WHERE lang=?", (lang,)).fetchall()
    clean_words, artefact_bills = set(), set()
    for k, _n, t in rows:
        if any(a in t for a in ARTEFACT):
            artefact_bills.add(k)
    for k, _n, t in rows:
        if k not in artefact_bills:
            clean_words.update(WORD.findall(norm(t)))
    found = defaultdict(set)  # (kind, en, si) -> bills
    for k, n, si in rows:
        en = store.section_text(k, n)
        if not en:
            continue
        if n == 1 and (tp := title_pair(en["text"], si)):
            found[("title", *tp)].add(k)
        for kind, a, b in term_pairs(en["text"], si):
            found[(kind, a, b)].add(k)
    merged, notes = defaultdict(set), {}
    for (kind, a, b), bills in found.items():
        b2 = fix_double_ya(b, clean_words)
        if b2 != b:
            notes[(kind, a, b2)] = f"spelling from bills without the doubled-ය font artefact (was {b})"
        merged[(kind, a, b2)] |= bills
    out = [(kind, a, b, len(bills), json.dumps(sorted(bills)), notes.get((kind, a, b)))
           for (kind, a, b), bills in merged.items()]
    store.replace_glossary(lang, out)
    store.commit()
    c = Counter(kind for kind, *_ in out)
    return {"entries": len(out), **c, "spelling_fixes": len(notes), "artefact_bills": len(artefact_bills)}


class Glossary:
    """Lookups over the stored glossary (loaded once; a few hundred rows)."""

    def __init__(self, store, lang="si"):
        self.rows = store.glossary(lang) if hasattr(store, "glossary") else []
        best = {}  # one Sinhala form per (kind, English): the most frequent
        for r in self.rows:
            key = (r["kind"], r["en"].lower())
            if key not in best or r["n"] > best[key]["n"]:
                best[key] = r
        self.best = list(best.values())

    def __bool__(self):
        return bool(self.rows)

    def in_question(self, question, limit=6):
        """[(local, en, kind)] titles and multi-word terms whose Sinhala form occurs in a Sinhala question, longest first.
        Titles match without their trailing පනත ("... පනත් කෙටුම්පත" = "... bill")."""
        q, hits = norm(question), []
        for r in self.best:
            local = SI_SUFFIX.sub("", r["local"]) if r["kind"] == "title" else r["local"]
            en = EN_SUFFIX.sub("", r["en"]) if r["kind"] == "title" else r["en"]
            if (r["kind"] == "title" or " " in local) and len(local) >= 4 and local in q:
                hits.append((local, en, r["kind"]))
        hits.sort(key=lambda h: (-len(h[0]), h[2] != "title"))
        out = []
        for local, en, kind in hits:  # drop matches inside a longer match
            if not any(local in o for o, _e, _k in out):
                out.append((local, en, kind))
        return out[:limit]

    def for_english(self, text, limit=15):
        """[(en, local)] entries whose English term occurs (whole words, any case) in the English evidence text."""
        low, hits = " ".join(text.split()).lower(), []
        for r in self.best:
            en = r["en"]
            if (" " not in en and r["n"] < 3) or len(en) < 4:  # one-word terms ("person", "body") only if common
                continue
            if re.search(rf"(?<![\w-]){re.escape(en.lower())}(?![\w-])", low):
                hits.append((r["n"], len(en), en, r["local"]))
        hits.sort(key=lambda h: (-h[0], -h[1]))
        seen, out = set(), []
        for _n, _l, en, local in hits:
            if en.lower() not in seen:
                seen.add(en.lower())
                out.append((en, local))
        return out[:limit]


def terms_block(pairs, lang="Sinhala"):
    """Prompt block; no [ ] so nothing in it can pass as a citation."""
    if not pairs:
        return ""
    return (f"\n\nOFFICIAL {lang.upper()} TERMS used in the bills' own {lang} text (use these words for these English "
            "terms):\n" + "\n".join(f"- {en} = {local}" for en, local in pairs))
