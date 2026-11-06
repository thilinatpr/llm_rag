"""Hand transcriptions of table pages (procurement/tables/<DOC>/p<NNN>.md), read from the page images.

pdftotext flattens tables: columns drift, merged cells lose their rows, and a unit in a header ("In LKR. Million") ends up
next to the wrong numbers (a live answer turned LKR 35,000 into "LKR 35,000 million"). For those pages, the transcription
REPLACES the extracted text at build time. Each file is a whole page (prose and tables) with section headings and
"(Relevant Reference ...)" lines kept as their own lines, so the manual parser splits it like any other page.

`check(doc, pdf)` is the safeguard: every number in a transcription must occur in the PDF's own text layer for that page,
and every number in the text layer must occur in the transcription (minus the printed page number). Lines that are
ours, not the document's (the <!-- --> header, "Table:" captions, "(continued ...)" notes), are ignored.
"""
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent / "tables"
OURS = re.compile(r"^\s*(?:<!--.*-->|Table:|\((?:table continues|continued from)|The printed table ticks)", re.I)
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def load(doc):
    """{pdf page number: transcribed text} for one document (empty if there are none)."""
    out = {}
    for f in sorted((ROOT / doc).glob("p*.md")):
        txt = f.read_text(encoding="utf-8")
        out[int(f.stem[1:])] = "\n".join(ln for ln in txt.splitlines() if not ln.lstrip().startswith("<!--"))
    return out


def numbers(text, skip_ours=True):
    c = Counter()
    for ln in text.splitlines():
        if skip_ours and OURS.match(ln):
            continue
        ln = re.sub(r"\[sic[^\]]*\]", "", ln)
        for n in NUM.findall(ln):
            n = n.replace(",", "").rstrip(".")
            if n:
                c[n.lstrip("0") or "0"] += 1
    return c


def check(doc, pdf):
    """[(page, only_in_transcription, only_in_pdf_text)] for pages whose numbers differ (as sets)."""
    from .parse import pdf_pages
    pages = pdf_pages(pdf)
    bad = []
    for p, txt in load(doc).items():
        mine, theirs = set(numbers(txt)), set(numbers(pages[p - 1], skip_ours=False))
        printed = re.findall(r"^\s*(\d{1,3})\s*$", pages[p - 1], re.M)  # the printed page number
        extra, missing = sorted(mine - theirs), sorted(theirs - mine - set(printed))
        if extra or missing:
            bad.append((p, extra, missing))
    return bad
