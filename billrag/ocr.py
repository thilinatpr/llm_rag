"""OCR helper for scanned PDFs with no text layer, for MANUAL review sessions only (owner, 2026-11-09: hard cases are handled by hand,
never inside the automated refresh). Its output is a first reading: compare it with a second, independent reading (a vision model or
a person), settle every disagreement on the page image, and add the reviewed text with `python -m billrag add-base-act --text`.

  python -c "from billrag import ocr; p, i = ocr.ocr_pdf('scan.pdf'); open('out.txt','w').write('\\n\\f\\n'.join(p)); print(i)"

Each page is straightened, rendered at 300 dpi and read by Tesseract (`tesseract` binary,
English data) with word boxes; margin notes are taken out of the body lines by position and put after the page body, which is
where the parser expects them (that is how pypdf returns a printed Act's margin notes).

What was learned on 14/1990 (7 pages, 2026-11-09), each from a wrong result:
- `get_pixmap(dpi=...)` ignores `matrix`, so a rotation must go into the matrix with the zoom.
- Scans are skewed up to 2 degrees; unstraightened, Tesseract cuts slanted lines into pieces and a sort by y interleaves them.
  Keep Tesseract's own line order.
- Margin notes share a text line with the clause ("Legal effect  3. (1) Where ..."): lines are cut at wide gaps between words, and a
  short piece outside the body column is a note. A bold section number followed by a wide space ("3.   (1)") stays with its text.
- The stop after a section number is often read as a comma ("1, This Act").
- OCR misreads words and numbers ("No. 14 of 1980" for 1990 at 84% confidence, "Law, No. 18" for 13), so confidence cannot certify
  it; some misspellings are in the print itself. Never index OCR output without the review above. Indexed transcriptions are marked
  in table `transcribed_docs` (billrag/transcribed.py) and answers that use them carry a fixed note.
"""
import os
import re
import shutil
import statistics
import subprocess
import tempfile

MIN_TEXT = 200   # fewer extracted characters than this in the whole PDF = a scan (needs_ocr)
DPI = 300
GAP = 0.025      # words further apart than this share of the page width belong to different pieces of a line


def available():
    return shutil.which("tesseract") is not None


def needs_ocr(pages):
    return sum(len((p or "").strip()) for p in pages) < MIN_TEXT


def skew(page):
    """Angle in degrees (-3..3) that levels the text rows: the one whose row-ink profile varies most."""
    import numpy as np
    import pymupdf
    best = (0.0, -1.0)
    for a10 in range(-30, 31, 2):
        a = a10 / 10
        pix = page.get_pixmap(matrix=pymupdf.Matrix(100 / 72, 100 / 72).prerotate(a), colorspace=pymupdf.csGRAY)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
        score = float((img < 128).sum(axis=1).astype(float).var())
        if score > best[1]:
            best = (a, score)
    return best[0]


def read_tsv(tsv):
    """Tesseract TSV -> ([line = [(x, y, w, h, word)]] in Tesseract's order, [word confidences])."""
    lines, confs = {}, []
    for row in tsv.splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5" or not f[11].strip():
            continue
        lines.setdefault((int(f[2]), int(f[3]), int(f[4])), []).append((int(f[6]), int(f[7]), int(f[8]), int(f[9]), f[11]))
        confs.append(float(f[10]))
    return list(lines.values()), confs


def split_margin(lines, width):
    """(body lines, margin pieces). A line is cut where two words are more than GAP * width apart; a piece of at most 4 words lying
    outside the body column (left of the usual line start or right of the usual line end) is a margin note."""
    gap = GAP * width
    pieces = []  # (line index, words)
    for i, ln in enumerate(lines):
        cur = [ln[0]]
        for w in ln[1:]:
            lone_number = len(cur) == 1 and re.fullmatch(r"\d{1,3}[A-Z]?[.,]", cur[0][4])
            if w[0] - (cur[-1][0] + cur[-1][2]) > gap and not lone_number:
                pieces.append((i, cur))
                cur = [w]
            else:
                cur.append(w)
        pieces.append((i, cur))
    long = [p for _, p in pieces if len(p) >= 5]
    if not long:
        return lines, []
    left = statistics.median(p[0][0] for p in long)
    right = statistics.median(p[-1][0] + p[-1][2] for p in long)
    slack = 0.01 * width
    body, margin = {}, []
    for i, p in pieces:
        outside = p[-1][0] + p[-1][2] < left - slack or p[0][0] > right + slack
        if outside and len(p) <= 4:
            margin.append(p)
        else:
            body.setdefault(i, []).extend(p)
    return [body[i] for i in sorted(body)], margin


def as_text(lines):
    out = "\n".join(" ".join(w[4] for w in ln) for ln in lines)
    return re.sub(r"(?m)^(\d{1,3}[A-Z]?)\s*,(?=\s+[A-Z(])", r"\1.", out)  # "1, This Act" -> "1. This Act"


def ocr_pdf(path):
    """-> (page texts, info {pages, mean_conf, low_share}). Needs the `tesseract` binary with English data."""
    import pymupdf
    doc = pymupdf.open(path)
    pages, confs = [], []
    with tempfile.TemporaryDirectory() as td:
        for i, page in enumerate(doc):
            a = skew(page)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(DPI / 72, DPI / 72).prerotate(a))
            png = os.path.join(td, f"{i}.png")
            pix.save(png)
            tsv = subprocess.run(["tesseract", png, "stdout", "-l", "eng", "--psm", "4", "tsv"],
                                 capture_output=True, text=True, check=True).stdout
            lines, c = read_tsv(tsv)
            confs += c
            body, margin = split_margin(lines, pix.width) if lines else ([], [])
            pages.append(as_text(body) + ("\n" + as_text(margin) if margin else ""))
    info = {"pages": len(pages), "mean_conf": round(statistics.mean(confs), 1) if confs else 0.0,
            "low_share": round(sum(c < 60 for c in confs) / len(confs), 3) if confs else 1.0}
    return pages, info

