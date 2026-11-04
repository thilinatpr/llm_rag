"""Structure-aware parsing of a bill PDF into sections and retrieval chunks.

A Sri Lankan bill is laid out as: cover page (title, long title, presenter,
dates, "[Bill No. N]"), then "BE it enacted ... as follows:-", then numbered
sections grouped under PART headings, then schedules. We recover that structure
so that retrieval units are *clauses* (with part/section/page metadata) instead
of arbitrary 1000-character windows.
"""
import logging
import re
from dataclasses import dataclass, field

logging.getLogger("pypdf").setLevel(logging.ERROR)

NOISE = [re.compile(p, re.I) for p in (
    r"^\d{1,3}$",                          # page numbers and the 5/10/15 margin line numbers
    r"^PL\s*-?\s*\d+.*\(\d+/\d{4}\)\s*$",  # printer's job line
    r"^L\.?\s?D\.?\s*-?\s*O.*$",           # "L.D.- O 29/2026"
    r"^Department of Government Printing$",
)]
END_PUNCT = (".", ";", ":", ",", ")", "-", "—")
ROMAN_PART = re.compile(r"^PART\s+([IVXLC]+|\d+)\b\s*(.*)$")
SCHEDULE = re.compile(r"^(?:(?:FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH)\s+)?SCHEDULE\b")
SECTION = re.compile(r"^(\d{1,3})\.\s+(\S.*)$")  # used for part-title guard only


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


@dataclass
class Section:
    kind: str                 # "section" | "schedule"
    no: int | None
    part: str | None
    label: str | None         # schedule label
    page: int
    lines: list = field(default_factory=list)
    heading: str | None = None

    @property
    def text(self) -> str:
        out = ""
        for ln in self.lines:
            if re.match(r"^\(([a-z]{1,3}|\d{1,2})\)\s", ln) and out:
                out += "\n" + ln
            else:
                out += (" " if out else "") + ln
        return out.strip()


@dataclass
class ParsedBill:
    cover: dict
    sections: list
    n_pages: int


COVER_STOP = re.compile(r"^(Presented by|Ordered|Part\s|Price|PRINTED|TO BE|\[Bill|SUPPLEMENT|\(Issued|This (Bill|Gazette)|[\u2014\-_]{4,})", re.I)


def parse_cover(page1: str) -> dict:
    """Works for both layouts: printed Bill (title, A BILL, long title, Presented by ...)
    and Gazette Supplement (A BILL, long title, TITLE, Part II of ..., Ordered to be published by ...)."""
    t = page1.replace("\r", "")
    cover = {}
    lines = [re.sub(r"\s+", " ", l).strip() for l in t.splitlines()]
    lines = [l for l in lines if l]
    for i, l in enumerate(lines):
        if l == "A" and i + 1 < len(lines) and lines[i + 1] == "BILL":
            if i and lines[i - 1].isupper() and not COVER_STOP.match(lines[i - 1]):
                cover["title"] = lines[i - 1]
            lt = []
            for l2 in lines[i + 2:]:
                if COVER_STOP.match(l2) or (l2.isupper() and len(l2) > 3):
                    if l2.isupper() and "title" not in cover and not COVER_STOP.match(l2):
                        cover["title"] = l2
                    break
                lt.append(l2)
            if lt:
                cover["long_title"] = " ".join(lt)
            break
    m = re.search(r"Presented by\s+(.+?)\s+on\s+(\d{1,2}\w*\s+(?:of\s+)?\w+,?\s+\d{4})", t, re.S)
    if m:
        cover["presented_by"] = re.sub(r"\s+", " ", m.group(1)).strip()
        cover["presented_on"] = re.sub(r"\s+", " ", m.group(2)).strip()
    else:
        m = re.search(r"Ordered to be published by\s+(?:the\s+)?([^\n]+(?:,\s*\n[^\n]+)?)", t)
        if m:
            cover["presented_by"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = re.search(r"Published in the Gazette on\s+([^)\n]+)", t) or re.search(r"Part II of\s+([^\n]+)", t)
    if m:
        cover["gazette_date"] = m.group(1).strip()
    m = re.search(r"\[Bill No\.?\s*([\w/ -]+)\]", t)
    if m:
        cover["printed_no"] = m.group(1).strip()
    return cover


def match_section(line: str, expected: int):
    """Return the text after the section number if `line` starts section `expected`.
    Handles PDF-extraction artefacts where the number is doubled ('1.1. Text', '33. Text' for 3)."""
    s = str(expected)
    for prefix in (f"{s}.{s}.", f"{s}{s}.", f"{s}."):
        if line.startswith(prefix):
            rest = line[len(prefix):].strip()
            if rest and (rest[0].isupper() or rest[0] in "(\"\u201c\u2018'"):
                return rest
    return None


def clean_page(text: str, titles: set):
    lines = []
    for raw in text.splitlines():
        ln = re.sub(r"\s+", " ", raw).strip()
        if (not ln or any(r.match(ln) for r in NOISE) or norm(ln) in titles
                or norm(re.sub(r"^\d{1,3}\s+|\s+\d{1,3}$", "", ln)) in titles):
            continue
        lines.append(ln)
    # Marginal notes ("Short title", "Powers, duties / and functions / of ...") are extracted as short
    # unpunctuated lines after the page body; split them off. The first line of a note starts uppercase.
    notes = []
    while (lines and len(lines[-1]) <= 26 and not lines[-1].endswith(END_PUNCT)
           and not re.match(r"^\(?\d", lines[-1]) and not lines[-1].startswith("(")):
        notes.insert(0, lines.pop())
    while notes and not notes[0][0].isupper():  # return body fragments that are not note starts
        lines.append(notes.pop(0))
    return lines, notes


def parse_pages(pages: list, listing_title: str = "") -> ParsedBill:
    cover = parse_cover(pages[0]) if pages else {}
    titles = {norm(t) for t in (cover.get("title"), re.sub(r"\s*-\s*(bill|gs)\s*$", "", listing_title, flags=re.I))
              if t}
    titles.discard("")
    body = [(i + 1, *clean_page(p, titles)) for i, p in enumerate(pages)]

    # Start after "BE it enacted ... as follows:-" (the sentence wraps over 2-3 lines).
    start, found = (0, 0), False
    for pi, (pno, lines, _) in enumerate(body):
        for li, ln in enumerate(lines):
            if re.search(r"\bBE\s+it\s+enacted\b", ln, re.I):
                k = li
                while k < min(li + 3, len(lines)) and "follows" not in lines[k]:
                    k += 1
                start, found = (pi, (k + 1) if k < min(li + 3, len(lines)) else li + 1), True
                break
        if found:
            break
    if not found:
        start = (1 if len(body) > 1 else 0, 0)

    sections, cur, expected, part, in_sched = [], None, 1, None, False
    pending_part = False
    notes_by_page = {}
    for pi in range(start[0], len(body)):
        pno, lines, notes = body[pi]
        notes_by_page[pno] = notes
        for li, ln in enumerate(lines):
            if pi == start[0] and li < start[1]:
                continue
            if pending_part:
                if ln.isupper() and not SECTION.match(ln) and not SCHEDULE.match(ln):
                    part += " \u2014 " + ln if " \u2014 " not in part else " " + ln
                    part_lines += 1
                    pending_part = part_lines < 3
                    continue
                pending_part = False
            m = ROMAN_PART.match(ln)
            if m and not in_sched:
                part, pending_part, part_lines = f"PART {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else ""), True, 0
                continue
            if SCHEDULE.match(ln) and len(ln) < 80 and ln.split("(")[0].strip().isupper():
                in_sched = True
                cur = Section("schedule", None, part, ln, pno)
                sections.append(cur)
                continue
            rest = None if in_sched else match_section(ln, expected)
            if rest is not None:
                cur = Section("section", expected, part, None, pno, [rest])
                sections.append(cur)
                expected += 1
                continue
            if cur is None:
                cur = Section("section", None, part, None, pno)  # text before section 1
                sections.append(cur)
            cur.lines.append(ln)
    # Heading guess: if exactly one section starts on a page, that page's margin notes describe it.
    starts = {}
    for s in sections:
        starts.setdefault(s.page, []).append(s)
    for pno, ss in starts.items():
        if len(ss) == 1 and notes_by_page.get(pno):
            ss[0].heading = " ".join(notes_by_page[pno])
    sections = [s for s in sections if s.text]
    return ParsedBill(cover=cover, sections=sections, n_pages=len(pages))


def parse_pdf(path: str, listing_title: str = "") -> ParsedBill:
    import pypdf
    reader = pypdf.PdfReader(path)
    pages = []
    for p in reader.pages:
        try:
            pages.append(p.extract_text() or "")
        except Exception:
            pages.append("")
    return parse_pages(pages, listing_title)


def split_text(text: str, max_chars: int = 1400, overlap: int = 150) -> list:
    """Pack subsections into chunks of <= max_chars; window oversized paragraphs."""
    if len(text) <= max_chars:
        return [text]
    chunks, cur = [], ""
    for para in re.split(r"\n(?=\()", text):
        if len(para) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            i = 0
            while i < len(para):
                j = min(i + max_chars, len(para))
                if j < len(para):
                    k = para.rfind(" ", i + max_chars // 2, j)
                    j = k if k > 0 else j
                chunks.append(para[i:j].strip())
                if j >= len(para):
                    break
                i = max(j - overlap, i + 1)
        elif cur and len(cur) + len(para) + 1 > max_chars:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]
