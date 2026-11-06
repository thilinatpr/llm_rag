"""PDF -> clause chunks for the procurement documents.

Three layouts:
- Guidelines 2024 (gazette 2412/01): the table of contents lists every clause ("2.7.1 High Level Procurement Committee");
  body headings are found IN TOC ORDER (sequence check, like billrag.parse), so a "2.7" inside a sentence never splits.
  The right-margin cage "Refer Manual / 2.7.1" is cut off the text and kept as `xref`.
- Manuals (2024 and 2006): each section is introduced by a line naming the guideline clause it explains:
  "(Relevant Reference to the Procurement Guidelines - 2.7.1)" (2024, heading ABOVE it) or
  "PROCUREMENT GUIDELINE REFERENCE: 2.7.1 (Cont)" (2006, heading BELOW it). That clause is the section's `ref`.
- Guidelines 2006: numbered clauses (1.3, 1.3.1, ...), sequence-checked; one chunk per level-2 clause.

Every function works on (page, line) pairs so chunks carry the PDF page.
"""
import re
import subprocess

# ---------------------------------------------------------------- text extraction + cleanup

HEADER = re.compile(
    r"GAZETTE EXTRAORDINARY OF THE DEMOCRATIC|ckrcfha|I fldgi|This Gazette Extraordinary can be downloaded"
    r"|^\s*\d*\s*GOVERNMENT PROCUREMENT GUIDELINES\s*\d*\s*$|^\s*\d+A\s*-\s*PG\s*\d+"
    r"|^\s*PRINTED AT THE DEPARTMENT OF GOVERNMENT PRINTING|^\s*EOG\s+\d+")
PAGE_NO = re.compile(r"^\s*(?:\d{1,3}A?|[ivxlc]{1,6})\s*$", re.I)
CLAUSE_NO = r"\d{1,2}\.\d{1,2}(?:\.\d{1,2})?"
# right-margin cage in the 2024 Guidelines: "Refer Manual" and/or a clause number, far to the right of the text
MARGIN = re.compile(rf"(?:^|\s{{3,}})((?:Refer\s+Manual|Refer|Manual|Re)(?:\s+{CLAUSE_NO})?|{CLAUSE_NO}(?:\s*[,&/]\s*{CLAUSE_NO})*)\s*$")


def pdf_pages(path, overrides=None):
    """pdftotext -layout, one string per PDF page (poppler-utils). `overrides` {page_no: text} replaces whole pages
    (hand transcriptions of table pages, see procurement/tables.py)."""
    out = subprocess.run(["pdftotext", "-layout", str(path), "-"], check=True, capture_output=True, text=True).stdout
    pages = out.split("\f")
    for p, txt in (overrides or {}).items():
        pages[p - 1] = txt
    return pages


def lines_of(pages):
    """[(page_no, line)] without running headers/footers and bare page numbers; blank lines kept (they separate blocks)."""
    out = []
    for p, txt in enumerate(pages, 1):
        out.append((p, ""))  # a page break ends a block: headings are never collected across pages
        for ln in txt.splitlines():
            ln = ln.rstrip()
            if HEADER.search(ln) or PAGE_NO.match(ln):
                continue
            out.append((p, ln))
    return out


def cut_margin(line, min_col=70):
    """Split off right-margin cage text. Returns (text, margin or None)."""
    m = MARGIN.search(line)
    if m and m.start(1) >= min_col:
        return line[: m.start(1)].rstrip(), m.group(1)
    return line, None


def tidy(lines):
    """Join lines into paragraphs-ish text: dedent, collapse runs of blank lines, strip trailing space."""
    txt = "\n".join(ln.strip() for ln in lines)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return txt.strip()


def split_long(text, limit=1800):
    """Split a long clause at blank lines (then single lines) into pieces of at most ~limit chars."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in re.split(r"(\n\n)", text):
        if len(cur) + len(block) > limit and cur.strip():
            parts.append(cur.strip()); cur = ""
        cur += block
        while len(cur) > limit * 1.5:  # one huge block (a table): cut at a line break
            cut = cur.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(cur[:cut].strip()); cur = cur[cut:]
    if cur.strip():
        parts.append(cur.strip())
    return parts


def numkey(ref):
    return tuple(int(x) for x in ref.split("."))


def norm_words(s):
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).split()


# ---------------------------------------------------------------- Guidelines 2024 (TOC-guided)

TOC_LINE = re.compile(rf"^\s*({CLAUSE_NO})\s+(\S.*?)\s*(?:\.{{3,}}.*)?$")
CHAPTER = re.compile(r"^\s*CHAPTER\s+(\d+|[IVX]+)\s*[–-]?\s*(.*?)\s*$")


def toc_entries(lines):
    """Clause numbers + titles from the contents pages (lines before the DEFINITIONS heading)."""
    out = []
    for i, (_, ln) in enumerate(lines):
        if re.match(r"^\s*DEFINITIONS\s*$", ln):
            return out, i
        m = TOC_LINE.match(ln)
        if m:
            out.append([m.group(1), m.group(2).rstrip(". ")])
        elif out and ln.strip() and not CHAPTER.match(ln) and "...." in ln and not re.match(r"^\s*\d", ln) \
                and not re.sub(r"\.{3,}.*$", "", ln).strip().isupper():  # not a wrapped CHAPTER title
            out[-1][1] = (out[-1][1] + " " + re.sub(r"\.{3,}.*$", "", ln).strip()).strip()  # wrapped title
    return out, len(lines)


def heading_match(line, ref, title):
    s = line.strip()
    m = re.match(rf"^{re.escape(ref)}\.?\s+(\S.*)$", s)
    if not m:
        return False
    want, got = norm_words(title)[:2], norm_words(m.group(1))[:2]
    return bool(want) and got[: len(want)] == want


def parse_guidelines_2024(pages, doc="PG2024"):
    lines = lines_of(pages)
    toc, defs_at = toc_entries(lines)
    body_at = next(i for i in range(defs_at, len(lines)) if CHAPTER.match(lines[i][1]) and "...." not in lines[i][1])
    chunks = []
    # definitions: one block, split into pieces
    defs = [ln for _, ln in lines[defs_at + 1: body_at]]
    for j, piece in enumerate(split_long(tidy(defs), 1500)):
        chunks.append(dict(doc=doc, ref="Definitions", heading="Definitions", chapter="", kind="definitions",
                           page=lines[defs_at][0], part=j, text=piece))
    # clauses, in TOC order
    starts, nxt, chapter, chapters = [], 0, "", {}
    for i in range(body_at, len(lines)):
        p, ln = lines[i]
        mc = CHAPTER.match(ln)
        if mc and "...." not in ln:
            chapter = f"Chapter {int(mc.group(1)):02d} - {mc.group(2).title()}" if mc.group(1).isdigit() else ln.strip()
            continue
        text, _ = cut_margin(ln)
        for j in range(nxt, min(nxt + 3, len(toc))):  # tolerate up to 2 missing headings
            if heading_match(text, *toc[j]):
                starts.append((i, j, chapter)); nxt = j + 1
                break
    missing = [toc[j][0] for j in range(len(toc)) if j not in {s[1] for s in starts}]
    for n, (i, j, chap) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        body, xref = [], []
        for k in range(i, end):
            if CHAPTER.match(lines[k][1]):
                break  # the next chapter's title block, not this clause
            t, mg = cut_margin(lines[k][1])
            if mg:
                xref += re.findall(CLAUSE_NO, mg)
            body.append(t)
        ref, title = toc[j]
        body[0] = re.sub(rf"^\s*{re.escape(ref)}\.?\s+", "", body[0])  # heading line: drop the number
        for part, piece in enumerate(split_long(tidy(body))):
            chunks.append(dict(doc=doc, ref=ref, heading=title, chapter=chap, kind="clause", page=lines[i][0],
                               part=part, text=piece, xref=",".join(dict.fromkeys(xref)) or None))
    return chunks, dict(toc=len(toc), found=len(starts), missing=missing)


# ---------------------------------------------------------------- Manuals (sections keyed by guideline reference)

REF_2024 = re.compile(r"^\s*\(?\s*(?:Relevant\s+Reference\s+to\s+the\s+Procurement\s+Guidelines?|Procurement\s+(?:Guidelines?|Manual)"
                      r"\s+Reference)\s*[-–:]\s*(.+?)\s*\)?\s*$", re.I)
REF_2006 = re.compile(r"^\s*PROCUREM\s*ENT\s+GUIDELINES?\s+REFERENCE\s*:?\s*(.*?)\s*(\(\s*Cont\.?\s*\))?\s*$", re.I)


def refs_in(s):
    return re.findall(r"\d{1,2}(?:\.\d{1,2}){1,3}", s)


def _manual_body_start(lines):
    """The first "CHAPTER 01" line without dot leaders (the contents lines carry them; forms later in the book do too)."""
    for i, (_, ln) in enumerate(lines):
        m = CHAPTER.match(ln)
        if m and m.group(1) in ("01", "1", "I") and "...." not in ln:
            return i
    return 0


def parse_manual_2024(pages, doc="PM2024"):
    lines = lines_of(pages)
    start = _manual_body_start(lines)
    marks = [i for i in range(start, len(lines)) if REF_2024.match(lines[i][1])]
    chunks, chapter, owned = [], "", set()
    heads = {}
    for i in marks:  # heading = the block of short lines directly above the reference line
        h, k = [], i - 1
        while k > start and lines[k][1].strip() and len(h) < 4 and not re.search(r"[.;:]\s*$|…|\.{4}|\*\*", lines[k][1]) \
                and not REF_2024.match(lines[k][1]) and not CHAPTER.match(lines[k][1]):
            h.insert(0, lines[k][1].strip()); k -= 1
        heads[i] = (k + 1, " ".join(h))
        owned.update(range(k + 1, i + 1))
    chap_at = {}
    for i in range(start, len(lines)):
        mc = CHAPTER.match(lines[i][1])
        if mc:
            title = mc.group(2)
            if i + 1 < len(lines) and lines[i + 1][1].strip() and lines[i + 1][1].strip().isupper():
                title += " " + lines[i + 1][1].strip()  # wrapped chapter title
            chap_at[i] = f"Chapter {int(mc.group(1)):02d} - {title.title()}"
    for n, i in enumerate(marks):
        end = heads[marks[n + 1]][0] if n + 1 < len(marks) else len(lines)
        for c in sorted(chap_at):
            if c <= i:
                chapter = chap_at[c]
        body = []
        for k in range(i + 1, end):
            if k in chap_at:
                break
            body.append(lines[k][1])
        rs = refs_in(REF_2024.match(lines[i][1]).group(1))
        heading = heads[i][1]
        kind = "annexure" if re.search(r"\bAnnex(?:ure)?\b", heading, re.I) else "section"
        for part, piece in enumerate(split_long(tidy(body))):
            chunks.append(dict(doc=doc, ref=rs[0] if rs else "?", heading=heading, chapter=chapter, kind=kind,
                               page=lines[i][0], part=part, text=piece, xref=",".join(rs) or None))
    return chunks, dict(sections=len(marks))


def parse_manual_2006(pages, doc="PM2006"):
    lines = lines_of(pages)
    marks = [i for i, (_, ln) in enumerate(lines) if REF_2006.match(ln) and refs_in(REF_2006.match(ln).group(1))]
    secs = []  # merge "(Cont)" pages into the section they continue
    for n, i in enumerate(marks):
        m = REF_2006.match(lines[i][1])
        rs = refs_in(m.group(1))
        end = marks[n + 1] if n + 1 < len(marks) else len(lines)
        body = [ln for _, ln in lines[i + 1: end]]
        if m.group(2) and secs and secs[-1]["rs"] == rs:
            secs[-1]["body"] += body
            continue
        heading = []
        while body and not body[0].strip():
            body.pop(0)
        while body and body[0].strip() and len(heading) < 2 and body[0].strip().upper() == body[0].strip():
            heading.append(body.pop(0).strip())
        secs.append(dict(rs=rs, page=lines[i][0], heading=re.sub(r"\s+", " ", " ".join(heading)), body=body))
    chunks = []
    for s in secs:
        heading = s["heading"].title() if s["heading"] else ""
        kind = "annexure" if re.search(r"\b(FORMAT|ANNEX)", s["heading"]) else "section"
        for part, piece in enumerate(split_long(tidy(s["body"]))):
            chunks.append(dict(doc=doc, ref=s["rs"][0], heading=heading, chapter="", kind=kind, page=s["page"],
                               part=part, text=piece, xref=",".join(s["rs"])))
    return chunks, dict(sections=len(secs))


# ---------------------------------------------------------------- Guidelines 2006 (numbered, sequence-checked)

NUM_LINE = re.compile(rf"^\s{{0,26}}({CLAUSE_NO})\.?(?:\s+(\S.*))?$")


def plausible_next(last, new):
    if last is None:
        return new == (1, 1)
    if new <= last:
        return False
    if new[0] == last[0]:
        return new[1] <= last[1] + 2
    return new[0] == last[0] + 1 and new[1] <= 2


def parse_guidelines_2006(pages, doc="PG2006"):
    lines = lines_of(pages)
    # body starts at the LAST "1.1 <word>" before the first "1.1.1" (the contents list 1.1 ... 9.8 first)
    first_111 = next(i for i, (_, ln) in enumerate(lines) if re.match(r"^\s*1\.1\.1\s", ln))
    start = max(i for i in range(first_111) if re.match(r"^\s*1\.1\s+\S", lines[i][1]))
    heads, last, chapter, chap_of = [], None, "", {}
    for i in range(start, len(lines)):
        ln = lines[i][1]
        mc = CHAPTER.match(ln)
        if mc:
            nxt = next((lines[k][1].strip() for k in range(i + 1, min(i + 4, len(lines))) if lines[k][1].strip()), "")
            chapter = f"Chapter {mc.group(1)} - {nxt.title()}"
        m = NUM_LINE.match(ln)
        if not m:
            continue
        key = numkey(m.group(1))
        if len(key) == 2 and plausible_next(last, key):
            heads.append((i, m.group(1), (m.group(2) or "").strip())); last = key
            chap_of[i] = chapter
    chunks = []
    for n, (i, ref, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = [ln for _, ln in lines[i + 1: end] if not CHAPTER.match(ln)]
        # drop the next chapter's title block (CHAPTER II / GENERAL) that sits at the end of the previous clause
        for part, piece in enumerate(split_long(tidy(body))):
            chunks.append(dict(doc=doc, ref=ref, heading=title, chapter=chap_of[i], kind="clause", page=lines[i][0],
                               part=part, text=piece, xref=None))
    return chunks, dict(clauses=len(heads))


# ---------------------------------------------------------------- Constitution, Chapter XIXB only (NPC, Articles 156B-156H)

CON_HEADER = re.compile(r"The Constitution of the Democratic Socialist Republic of Sri Lanka")
ARTICLE = re.compile(r"^\s*(156[B-Z])\.\s")


def split_margin(lines):
    """Margin notes (side headings) alternate sides: on a page whose body is indented (>= 14 cols) they are on the LEFT,
    otherwise on the RIGHT (>= 55 cols, after a gap). Returns [(body, note or "")]."""
    starts = sorted(len(ln) - len(ln.lstrip()) for ln in lines if len(ln.strip()) > 40)
    left = bool(starts) and starts[len(starts) // 2] >= 14
    out = []
    for ln in lines:
        note = ""
        if left:
            m = re.match(r"^(\s{0,9}\S.{0,24}?)(?:\s{3,}(\S.*))?$", ln)
            if m and (len(ln) - len(ln.lstrip()) < 10) and (m.group(2) or len(ln.strip()) < 26):
                note, ln = m.group(1).strip(), m.group(2) or ""
        else:
            m = re.search(r"\s{3,}(\S.{0,24})$", ln)
            if m and m.start(1) >= 55:
                note, ln = m.group(1).strip(), ln[: m.start()]
        out.append((ln.rstrip(), note))
    return out


def parse_constitution_npc(pages, doc="CON"):
    """Chapter XIXB (National Procurement Commission) of the consolidated English Constitution: one chunk per Article."""
    pairs, inside = [], False
    for p, txt in enumerate(pages, 1):
        lines = [ln for ln in txt.splitlines() if not CON_HEADER.search(ln)]
        for ln, note in split_margin(lines):
            if re.match(r"^\s*\[?CHAPTER XIX\s?B\s*$", ln):
                inside = True; continue
            if inside and re.match(r"^\s*CHAPTER XX\s*$", ln):
                inside = False
            if inside:
                pairs.append((p, ln, note))
        if pairs and not inside:
            break
    arts = []  # lines before 156B (the chapter title) are not kept
    for p, ln, note in pairs:
        m = ARTICLE.match(ln)
        if m:
            arts.append(dict(ref=m.group(1), page=p, body=[], notes=[]))
        if not arts:
            continue
        if note:
            arts[-1]["notes"].append(note)
        if ln.strip() or not note:  # a line that held only a margin note is not a paragraph break
            arts[-1]["body"].append(ln)
    chunks = []
    for a in arts:
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", tidy(a["body"]))  # rec-\nommendation
        text = re.sub(r"(?<=\S)\n(?=[a-z(])", " ", text)  # re-flow wrapped lines; keep breaks before (1), (a) ...
        chunks.append(dict(doc=doc, ref=a["ref"], heading=" ".join(a["notes"]), chapter="Chapter XIXB - National Procurement Commission",
                           kind="article", page=a["page"], part=0, text=text, xref=None))
    return chunks, dict(articles=len(chunks))


PARSERS = {"PG2024": parse_guidelines_2024, "PM2024": parse_manual_2024,
           "PG2006": parse_guidelines_2006, "PM2006": parse_manual_2006,
           "CON": parse_constitution_npc}
