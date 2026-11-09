# Reviewed transcriptions of scanned Acts

Acts that the index needs (base Acts: see AGENTS.md "Base Acts") but that exist only as scans. Each is transcribed BY HAND in a
session, never in the automated build (owner, 2026-11-09):

1. Two independent readings of the page images: `billrag/ocr.py` (Tesseract, pages straightened, margin notes split off) and a
   vision reading (a vision model or a person).
2. Every disagreement is settled on a zoomed crop of the scan; every number, date and paragraph label is checked.
3. Typesetting errors in the print are kept as printed; letters merely damaged in the scan are written as the word they clearly are.
4. The owner reads and accepts the text.

Files per Act `N-YYYY`:
- `N-YYYY.txt`: the text, pages separated by form feeds, margin notes after each page's body (as the parser expects).
- `N-YYYY.json`: `no`, `year`, `title` (without "Act, No. .."), `date` (certified), `sections` (count, checked by a test), `reviewed`,
  `source`, `accepted_by` (null until the owner accepts the text; the build adds only accepted ones).
- `N-YYYY.review.md`: what was corrected and why.

`scripts/refresh_bills.py` adds every Act here that the index does not hold (`python -m billrag add-base-act --text ...`), before it
fetches base Acts. Such an Act is recorded in `transcribed_docs`, and every answer that uses it carries the note "The text of ... was read
from a scanned copy by OCR and checked by hand ... check the wording against the official printed document" (billrag/transcribed.py).
