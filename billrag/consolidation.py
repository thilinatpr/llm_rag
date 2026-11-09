"""Inland Revenue Department consolidated texts (ird.gov.lk) -> our transcript format, for MANUAL review sessions only.

IRD publishes "consolidated versions" of tax Acts: Word-made PDFs with a text layer that fold every amendment into the Act and note
each change in the right margin ("Sec 2(1) of 15 of 2024 / Repealed and substituted"). They say of themselves: "not a statutory
consolidation, but rather a convenient reference". In the index they sit NEXT TO the certified Act (key "Cons N/YYYY"), never
instead of it, and every answer that uses one gets a fixed note (billrag/transcribed.py, method "IRD consolidation").

    pages, report = to_pages("sscl_cons_2026.pdf", first_body_page=8, title_prefix="Social Security Contribution Levy Act")

Output: one string per page in the transcript format (`ingest.read_transcript`): body lines, then the page's margin titles (as the
parser expects margin notes), with each change note kept inline where it stands: "[IRD note: Sec 2(1) of 15 of 2024: Repealed and
substituted]". Review it like a transcription (sources/base_acts/README.md): compare the unchanged sections with the certified Act.
"""
import re

MARGIN_X = 497          # x where the margin column starts on IRD's Letter-size pages (measured on SSCL 2026)
CHANGE = re.compile(r"^(?:Sec(?:tion)?s?\.?|s\.?)\s*\d|\b(?:of\s+\d{1,3}\s+of\s+\d{4}|Repealed|Substituted|Inserted|Amended|Added|"
                    r"Replaced|Deleted|w\.?e\.?f|with effect)\b", re.I)


def _clean(t):
    t = re.sub(r"[ \t]+", " ", t.replace(" ", " "))
    return "\n".join(ln.strip() for ln in t.splitlines() if ln.strip())


def _join_labels(t):
    """'4 \\n(1) \\nEvery ...' -> '4 (1) Every ...', '(a) \\nimports' -> '(a) imports': a line that is only a label joins the next line."""
    out = []
    for ln in t.split("\n"):
        # only a real label: "(a)", "(1)", "(iv)", a bare section number "4" or "4 (1)" ("18." alone ends a sentence: "... section\\n18.")
        if out and re.fullmatch(r"\([a-z0-9]{1,4}\)|\d{1,3}[A-Z]?|\d{1,3}\s+\(\d{1,2}\)", out[-1]) and not re.fullmatch(r"\d{1,3}", ln):
            out[-1] = out[-1] + " " + ln
        else:
            out.append(ln)
    return out


def _number_sections(lines, expected):
    """'15 (1) ...' / '16 Every ...' -> '15. (1) ...': only the next expected section number, at a line start, so schedule items and
    figures cannot pass for sections."""
    out = []
    for ln in lines:
        if re.match(r"^\s*FIRST\s+SCHEDULE\b", ln):
            expected[0] = 10 ** 6  # the sections are over: numbered schedule items are not sections
        m = re.match(r"^(\d{1,3})\s+(?=[A-Z(\u201c\"])", ln)
        if m and int(m.group(1)) == expected[0]:
            ln = f"{m.group(1)}. " + ln[m.end():]
            expected[0] += 1
        out.append(ln)
    return out


def _rows(body, inline):
    """Body lines -> printed rows: lines that overlap vertically are one row (a bold section number sits a few points lower than its
    text), ordered left to right; change notes go after the row they stand beside."""
    rows = []
    for y0, x0, t, y1 in sorted(body):
        overlap = min(y1, rows[-1]["y1"]) - max(y0, rows[-1]["y0"]) if rows else 0
        if rows and overlap >= 0.3 * min(y1 - y0, rows[-1]["y1"] - rows[-1]["y0"]):  # a label set a little lower still overlaps
            rows[-1]["parts"].append((x0, t))
            rows[-1]["y1"] = max(rows[-1]["y1"], y1)
        else:
            rows.append({"y0": y0, "y1": y1, "parts": [(x0, t)]})
    out = [(r["y0"], " ".join(t for _x, t in sorted(r["parts"]))) for r in rows] + list(inline)
    return [t for _y, t in sorted(out, key=lambda r: r[0])]


PARA_START = re.compile(r"^(?:\(([a-z]{1,4}|\d{1,2}|[ivx]{1,5})\)|\d{1,3}\.\s|Provided\b|PART\b|CHAPTER\b|[A-Z ]{6,}$)")


def _notes_to_paragraph_end(lines):
    """A change note stands beside the line it annotates, which is often mid-sentence: move it to the end of that paragraph (before the
    next line that starts a paragraph, a section, a proviso or a heading)."""
    out, pending = [], []
    for ln in lines:
        if ln.startswith("[IRD note:"):
            if pending:  # two notes beside one paragraph: one bracket
                pending[-1] = pending[-1][:-1] + "; " + ln[len("[IRD note: "):]
            else:
                pending.append(ln)
            continue
        if pending and PARA_START.match(ln):
            out.extend(pending)
            pending = []
        out.append(ln)
    return out + pending


def to_pages(path, first_body_page, title_prefix, margin_x=MARGIN_X):
    """Lines (not blocks) are sorted into body and margin: on some pages PyMuPDF puts a margin note and body text in one block."""
    import pymupdf
    doc = pymupdf.open(path)
    pages, notes_all, expected = [], [], [1]
    for pno in range(first_body_page - 1, doc.page_count):
        body, margin = [], []  # (y, text)
        for b in doc[pno].get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                t = _clean("".join(sp["text"] for sp in ln["spans"]))
                x0, y0 = ln["bbox"][0], ln["bbox"][1]
                # header (y < 50) and footer (y > 775) by position; the last body line of a page sits at y 750-755
                if not t or y0 < 50 or y0 > 775 or (t == "T" and x0 < 60) or t.startswith(title_prefix) \
                        or t.startswith(("Tax Policy and Legislation Unit", "Prepared By")):
                    continue
                y1 = ln["bbox"][3]
                (margin if x0 >= margin_x else body).append((y0, x0, t, y1))
        # margin lines -> runs (a gap of > 14pt starts a new run); a run is a title, or a change note, or a title then a note
        runs, cur, last = [], [], None
        for y0, x0, t, _y1 in sorted(margin):
            if cur and y0 - last > 14:
                runs.append(cur)
                cur = []
            cur.append((y0, t))
            last = y0
        if cur:
            runs.append(cur)
        titles, inline = [], []
        for run in runs:
            lines = [t for _y, t in run]
            k = next((i for i, l in enumerate(lines) if CHANGE.search(l)), len(lines))
            if lines[:k]:
                titles.append(" ".join(lines[:k]))
            if lines[k:]:
                note = re.sub(r"\s+", " ", " ".join(lines[k:])).strip()
                notes_all.append((pno + 1, note))
                inline.append((run[k][0] + 0.5, f"[IRD note: {note}]"))
        text = "\n".join(_notes_to_paragraph_end(_number_sections(_join_labels("\n".join(_rows(body, inline))), expected)))
        pages.append(text + ("\n" + "\n".join(titles) if titles else ""))
    return pages, {"change_notes": notes_all}
