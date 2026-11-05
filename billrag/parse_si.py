"""Official Sinhala text of a bill, section by section, so answers can use the bill's own wording.

Only bills whose Sinhala PDF uses a Unicode font (IskoolaPota; documents.gov.lk from 2025 on) are supported. Older Sinhala
PDFs (<= 2024) use the FM Abhaya legacy font (Sinhala glyphs on Latin code points) and extract as Latin gibberish: they need
a separate FM -> Unicode conversion (not done). Tamil is noisier still (doubled consonants + viramas) and is not handled.

Extraction uses PyMuPDF: pypdf and pdfminer both scramble the pre-base vowel sign (හෝ comes out as "තෝහා"). The page layout:
  - a running header (page number + short title) at the top of every page        -> dropped by position
  - line numbers 5/10/15/20/25 in their own narrow blocks left of the text        -> dropped (digits only)
  - margin notes (side headings) as blocks outside the text column                -> kept as the section heading hint
  - each section number printed twice: "13." on its own line, then "13. text"    -> de-duplicated
The font's glyph map also doubles vowel signs (රාාජ්‍යයය ආරක්ෂාා), so `clean` collapses repeated marks.

Sections are linked to the English index by (bill key, section number). A bill is linked only when its Sinhala text has
exactly the English bill's sections 1..N; anything else is rejected (the caller falls back to machine translation).
"""
import re
from dataclasses import dataclass, field

SI = "඀-෿"
JUNK = re.compile(rf"[^{SI}‌‍\x20-\x7e\n\t‘’“”–—]")
MARKS = "[ඁ-ඃ්-ෟෲෳ]"          # anusvara/visarga, virama, vowel signs
CONS = "[ක-ෆ]"
ENACT = re.compile(r"පනව\s*නු\s*ලැ\s*බේ")                    # "... be it enacted" (enacting clause; may wrap mid-word)
SCHEDULE = re.compile(r"^(?:\S+\s+)?උපලේඛනය\s*(?:\[.*)?$|^උපලේඛනය")  # "පළමුවන උපලේඛනය" = First Schedule


def clean(text: str) -> str:
    text = JUNK.sub("", text.replace(" ", " "))
    text = re.sub(rf"({MARKS})\1+", r"\1", text)                  # ාා -> ා, ෙෙ -> ෙ, ්් -> ්, ංං -> ං
    text = re.sub(rf"({CONS})\1{{2,}}", r"\1\1", text)          # යයය -> යය (ligature emitted twice)
    return text


def sinhala_share(text: str) -> float:
    letters = sum(c.isalpha() for c in text) or 1
    return sum(1 for c in text if "඀" <= c <= "෿") / letters


@dataclass
class SiSection:
    no: int
    page: int
    lines: list = field(default_factory=list)
    heading: str = ""

    @property
    def text(self):
        return re.sub(r"[ \t]+", " ", "\n".join(self.lines)).strip()


def page_lines(blocks, width, height):
    """blocks: [(x0, y0, x1, y1, text)] of one page -> (body lines, margin notes)."""
    body = [b for b in blocks if b[2] - b[0] > 0.3 * width and b[3] > 0.1 * height]
    if not body:
        return [], []
    left, right = min(b[0] for b in body), max(b[2] for b in body)
    lines, notes = [], []
    for x0, y0, x1, y1, t in sorted(blocks, key=lambda b: (b[1], b[0])):
        if y1 <= 0.1 * height:                                     # running header
            continue
        t = clean(t)
        if not t.strip() or re.fullmatch(r"[\s\d]*", t):           # line numbers 5/10/15...
            continue
        if x0 >= right - 4 or x1 <= left + 4:                      # margin note
            notes.append(" ".join(t.split()))
            continue
        lines += [s for s in (ln.strip(" \t\u200c\u200d") for ln in t.split("\n")) if s]  # stray ZWJ hides "17."
    return lines, notes


def section_start(line: str, n: int):
    """Text after the number if `line` starts section n ('13.', '13. text', '13.13. text'), else None."""
    m = re.match(rf"^{n}\s?\.(?:\s*{n}\s?\.)?\s*(.*)$", line)
    if not m:
        return None
    rest = m.group(1).lstrip("\u200c\u200d ")
    if rest and not (rest[0] in "(\"'“‘" or rest[0].isdigit() or "඀" <= rest[0] <= "෿"):
        return None
    return rest


def enact_end(flat):
    """Index of the first line after the enacting clause, which may wrap over two lines; None if there is none."""
    for i, (_, ln) in enumerate(flat):
        if ENACT.search(ln):
            return i + 1
        if i + 1 < len(flat) and ENACT.search(ln + flat[i + 1][1]):
            return i + 2
    return None


def parse_pages(pages, n_expected=None):
    """pages: [(page_no, lines, notes)] -> list of SiSection (1..n) or [] if the enacting clause is missing."""
    flat = [(pno, ln) for pno, lines, _ in pages for ln in lines]
    start = enact_end(flat)
    if start is None:
        return []
    notes = {pno: ns for pno, _, ns in pages}
    sections, cur, expected, dup = [], None, 1, None
    for pno, ln in flat[start:]:
        if n_expected and expected > n_expected and len(ln) < 60 and SCHEDULE.match(ln):
            break                                                 # schedules are not sections
        if dup is not None:
            d = re.match(rf"^{dup}\s?\.\s*", ln)
            dup = None
            if d:
                ln = ln[d.end():]
                if ln:
                    cur.lines.append(ln)
                continue
        if not n_expected or expected <= n_expected:
            rest = section_start(ln, expected)
            if rest is not None:
                cur = SiSection(expected, pno, [rest] if rest else [])
                sections.append(cur)
                dup = None if rest else expected                  # "13." alone: the next line repeats "13."
                expected += 1
                continue
        if cur is not None:
            cur.lines.append(ln)
    starts = {}
    for s in sections:
        starts.setdefault(s.page, []).append(s)
    for pno, ss in starts.items():                                # like the English parser: only an unambiguous note
        if len(ss) == 1 and notes.get(pno):
            ss[0].heading = " ".join(notes[pno])
    return sections


def visual_order(text):
    """Some 2025 PDFs use a font that stores Sinhala in visual order (ෙ before the consonant: ෙමෙසේ for මෙසේ) and drops
    the ්‍ර conjunct (ශී ලංකා): reordering could be automated, missing letters cannot, so such bills are rejected."""
    words = len(re.findall(rf"[{SI}]+", text)) or 1
    return len(re.findall(r"(?:^|[\s(])\u0DD9", text)) / words > 0.01


def check(sections, n_expected):
    """None if the Sinhala sections line up with the English bill's 1..n_expected, else the reason they do not."""
    if sections and visual_order(" ".join(s.text for s in sections)):
        return "visual-order font (drops letters)"
    if not sections:
        return "enacting clause or sections not found"
    if [s.no for s in sections] != list(range(1, n_expected + 1)):
        return f"found {len(sections)} sections, English has {n_expected}"
    empty = [s.no for s in sections if len(s.text) < 15]
    if empty:
        return f"empty sections {empty[:5]}"
    if sinhala_share(" ".join(s.text for s in sections)) < 0.8:
        return "not Unicode Sinhala (legacy font?)"
    return None


def parse_pdf(path, n_expected):
    """-> (sections, None) when the Sinhala PDF lines up with the English bill, else ([], reason)."""
    import pymupdf
    doc = pymupdf.open(path)
    pages = []
    for i, page in enumerate(doc):
        blocks = [b[:5] for b in page.get_text("blocks") if b[6] == 0]
        pages.append((i + 1, *page_lines(blocks, page.rect.width, page.rect.height)))
    if visual_order(" ".join(ln for _, lines, _ in pages for ln in lines)):
        return [], "visual-order font (drops letters)"
    sections = parse_pages(pages, n_expected)
    why = check(sections, n_expected)
    return (sections, None) if not why else ([], why)
