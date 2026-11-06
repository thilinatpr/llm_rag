"""Treasury guide PDF text (pdftotext -layout) -> procurement/npc_texts/GPMCM.md: chapters 4-10 of Part I only, word for word.

    pdftotext -layout "data/procurement/treasury/Guide to Project and Contract Management (Works) 2017.pdf" guide.txt
    python procurement/scripts/gpmcm_extract.py guide.txt

Source: treasury.gov.lk/api/file/773cd7b6-3dd0-4938-9e68-5723faebb134. The rest of the 301 pages is FIDIC clause procedures and forms,
which depend on the form of contract in use, so it is left out. The generic `numbered` parser cannot read this PDF: its numbering breaks after 2.10.
"""
import re
import sys
from pathlib import Path

HEAD = re.compile(r"^\s*((?:4|5|6|7|8|9|10)(?:\.\d{1,2}){0,2})\s+([A-Z].*\S)\s*$")
TITLE = "Guide to Project Management and Contract Management (Works), Ministry of Finance, February 2017, chapters 4-10"


def extract(text):
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if re.match(r"^\s*4\s+CONTRACT MANAGEMENT\s*$", l))
    out, last = [f"<!-- GPMCM | {TITLE}, word for word. Non-binding guidance. -->"], (3,)
    for ln in lines[start:]:
        if re.match(r"^\s*ANNEX 1\s*$", ln):  # chapter 10 ends here
            break
        m = HEAD.match(ln)
        if m:
            key = tuple(int(x) for x in m[1].split("."))
            if key > last and key[0] - last[0] <= 1 and key != (10, 2):  # "10.2 To initiate ..." is a numbered paragraph, not a heading
                out += ["", f"## {m[1]} | {re.sub(r'\s+', ' ', m[2])}"]
                last = key
                continue
        if re.fullmatch(r"\s*\d{1,3}\s*", ln) or "\f" in ln and not ln.strip("\f ").strip():
            continue  # printed page number
        if ln.strip():
            out.append(re.sub(r"\s+", " ", ln.replace("\f", "")).strip())
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    Path("procurement/npc_texts/GPMCM.md").write_text(extract(Path(sys.argv[1]).read_text(encoding="utf-8")), encoding="utf-8")
