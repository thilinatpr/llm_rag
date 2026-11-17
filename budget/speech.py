"""Budget Speech parser: English pages of the Treasury's trilingual PDF -> chunks by chapter.

The PDF holds Sinhala, Tamil and English one after another (the first two have broken text
layers), so the English run is found by script. Chunks keep the chapter number (`17.2`), its
heading and the PRINTED page number, which is what a citation shows: [Speech §17.2, p.41].
"""
from __future__ import annotations

import re

LIMIT = 1800  # characters per chunk before splitting at a paragraph/bullet boundary

CHAPTER = re.compile(r"^(\d{1,2})\.\s+([A-Z][^\n]{2,90}?)\s*$")
SUB = re.compile(r"^(\d{1,2}\.\d{1,2})\.?\s+([A-Z][^\n]{2,110}?)\s*$")


def english_pages(texts: list[str]) -> tuple[int, int]:
    """First and last 0-based index of the English run (the longest run of Latin-script pages)."""
    flags = []
    for t in texts:
        n = max(len(t), 1)
        flags.append(len(re.findall(r"[A-Za-z]", t)) / n > 0.4)
    best, start = (0, -1), None
    for i, f in enumerate(flags + [False]):
        if f and start is None:
            start = i
        if not f and start is not None:
            if i - start > best[0]:
                best = (i - start, start)
            start = None
    # English is the last language in the file; pages inside it can be near-blank (tables, images)
    last = max(i for i, f in enumerate(flags) if f)
    return best[1], last


def clean_page(text: str) -> tuple[str | None, str]:
    """(printed page number or None, body without the page number and running title)."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    printed = None
    body = []
    for k, ln in enumerate(lines):
        s = ln.strip()
        if printed is None and k < 3 and re.fullmatch(r"\d{1,3}", s):
            printed = s
            continue
        if s == "Budget Speech – 2027" or re.fullmatch(r"Budget Speech\s*[–-]\s*\d{4}", s):
            continue
        body.append(ln)
    return printed, "\n".join(body)


def parse_speech(texts: list[str]) -> list[dict]:
    a, b = english_pages(texts)
    chunks: list[dict] = []
    chapter, chap_title, sub, sub_title = None, None, None, None
    buf: list[str] = []
    buf_page = None

    def flush():
        nonlocal buf, buf_page
        text = re.sub(r"[ \t]+", " ", "\n".join(buf)).strip()
        buf = []
        if not text:
            return
        ref = sub or chapter or "intro"
        heading = " / ".join(x for x in (chap_title, sub_title) if x) or "Opening"
        for part in split_text(text):
            chunks.append({"ref": ref, "heading": heading, "page": buf_page, "text": part})

    for idx in range(a, b + 1):
        printed, body = clean_page(texts[idx])
        if idx == a and printed is None:  # cover page
            continue
        lines = [x.strip() for x in body.splitlines()]
        for k, s in enumerate(lines):
            if not s:
                continue
            if re.match(r"^Annexure\b", s):
                flush()
                return chunks
            if re.fullmatch(r"\d{1,2}\.", s):  # "5." then the title on the next line
                nxt = next((x for x in lines[k + 1:] if x), "")
                if nxt[:1].isupper():
                    s = f"{s} {nxt}"
                    lines[k + 1 + lines[k + 1:].index(nxt)] = ""
            m = SUB.match(s)
            c = CHAPTER.match(s) if not m else None
            # a chapter heading is the next number (or the one after, if a heading was missed):
            # bullets, tax lists and company lists number from 1 again inside a chapter
            nxt_ok = c and (chapter is None and int(c.group(1)) == 1 or chapter is not None and int(chapter) < int(c.group(1)) <= int(chapter) + 2)
            if nxt_ok:
                flush()
                chapter, chap_title, sub, sub_title = c.group(1), c.group(2).strip(), None, None
                buf_page = printed
                continue
            if m and chapter and m.group(1).split(".")[0] == chapter:
                flush()
                sub, sub_title = m.group(1), m.group(2).strip()
                buf_page = printed
                continue
            if not buf:
                buf_page = printed
            buf.append(s)
    flush()
    return chunks


def split_text(text: str) -> list[str]:
    if len(text) <= LIMIT:
        return [text]
    out, cur = [], ""
    for para in re.split(r"\n(?=[•o] |\S)", text):
        if cur and len(cur) + len(para) > LIMIT:
            out.append(cur.strip())
            cur = ""
        cur += para + "\n"
    if cur.strip():
        out.append(cur.strip())
    return out


# ----------------------------------------------------------------- annexures

def _lines(words, tol=4):
    """Group (x0, y0, x1, text) words into lines by y."""
    out = []
    for w in sorted(words, key=lambda w: (round(w[1]), w[0])):
        for ln in out:
            if abs(ln["y"] - w[1]) <= tol:
                ln["w"].append(w)
                break
        else:
            out.append({"y": w[1], "w": [w]})
    for ln in out:
        ln["w"].sort(key=lambda w: w[0])
    return sorted(out, key=lambda l: l["y"])


def annex_ranges(texts: list[str], a: int, b: int) -> list[tuple[str, int, int]]:
    """[(name, first, last)] 0-based page indexes for each 'Annexure N' in the English run."""
    marks = []
    for i in range(a, b + 1):
        m = re.search(r"(?m)^\s*Annexure\s+([IVX]+)\b", texts[i])
        if m:
            marks.append((m.group(1), i))
    out = []
    for k, (name, i) in enumerate(marks):
        end = (marks[k + 1][1] - 1) if k + 1 < len(marks) else b - 1  # b = sign-off page
        out.append((name, i, end))
    return out


def parse_proposals(doc, first: int, last: int) -> list[dict]:
    """Annexure II rows: serial, sector, proposal, amount (Rs million), column recurrent|capital.

    A proposal's words sit above and below its serial number (the serial is centred in the cell),
    so every word goes to the NEAREST serial by height; the amount's column is whichever header
    ('Recurrent' / 'Capital') is nearer to the amount's centre. Headings (sectors) are lines that
    start left of the serial column and carry no number."""
    rows: list[dict] = []
    sector = None
    skip = {"Serial", "No.", "Expenditure", "Proposal", "Proposed", "Annual", "Allocation", "(Rs.", "Million)", "Recurrent", "Capital"}
    for pi in range(first, last + 1):
        words = [(w[0], w[1], w[2], w[3], w[4]) for w in doc[pi].get_text("words")]  # x0 y0 x1 y1 text
        sx = next((x0 for x0, y0, x1, y1, t in words if t == "Serial"), None)
        hdr = {}
        for x0, y0, x1, y1, t in sorted(words, key=lambda w: w[1]):  # topmost occurrence = the column header
            if t in ("Recurrent", "Capital") and t not in hdr:
                hdr[t] = (x0, y1)
        if len(hdr) < 2 or sx is None:
            continue
        head = {t: (hdr[t][0] + [x1 for x0, y0, x1, y1, tt in words if tt == t and x0 == hdr[t][0]][0]) / 2 for t in hdr}
        top = max(hdr["Recurrent"][1], hdr["Capital"][1])
        printed = next((t for x0, y0, x1, y1, t in words if y0 > 740 and re.fullmatch(r"\d{1,3}", t)), None)
        body = [w for w in words if w[1] > top - 1 and w[1] < 740]
        mid = lambda w: (w[1] + w[3]) / 2
        serials = sorted(
            [w for w in body if re.fullmatch(r"\d{1,3}", w[4]) and abs(w[0] - (sx + 8)) <= 25], key=mid)
        sers = [{"serial": int(w[4]), "y": mid(w), "proposal": [], "amt": None, "page": printed, "sector": None} for w in serials]
        # sector headings: lines starting left of the serial column, no digits
        heads = []
        for ln in _lines([(w[0], w[1], w[2], w[4]) for w in body if w not in serials]):
            ws = ln["w"]
            txt = " ".join(w[3] for w in ws)
            if ws[0][0] < sx + 3 and not re.search(r"\d", txt) and not (set(txt.split()) & skip):
                heads.append((ln["y"], txt))
        amt_left = head["Recurrent"] - 60
        for w in body:
            if w in serials or not sers:
                continue
            near = min(sers, key=lambda s: abs(s["y"] - mid(w)))
            if w[0] >= amt_left and re.fullmatch(r"[\d,]*\d|-", w[4]):
                if near["amt"] is None:
                    c = (w[0] + w[2]) / 2
                    near["amt"] = (None if w[4] == "-" else int(w[4].replace(",", "")),
                                   "recurrent" if abs(c - head["Recurrent"]) < abs(c - head["Capital"]) else "capital")
            elif w[0] > sx + 20 and not any(abs(w[1] - y) < 3 and w[0] < sx + 3 for y, _ in heads):
                near["proposal"].append(w)
        for s in sers:
            above = [t for y, t in heads if y < s["y"]]
            if above:
                sector = above[-1]
            s["sector"] = sector
        # a heading can sit between rows: the row below it must take it, the one above must not
        for s in sers:
            ws = sorted(s["proposal"], key=lambda w: (round(w[1]), w[0]))
            text = re.sub(r"\s+", " ", " ".join(w[4] for w in ws)).strip()
            amount_, column = s["amt"] or (None, None)
            for k in (3, 2, 1):  # a wrapped sector heading ("Better Quality / of Life") leaks into the first row
                tail = " ".join((s["sector"] or "").split()[-k:])
                if tail and text.startswith(tail + " ") and (s["sector"] or "").endswith(tail) and len(s["sector"].split()) > k:
                    text = text[len(tail):].strip()
                    break
            rows.append({"serial": s["serial"], "sector": s["sector"], "proposal": text,
                         "amount": amount_, "column": column, "page": s["page"]})
    return rows
