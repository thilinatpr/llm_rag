# IRD consolidated texts

Consolidated versions of tax Acts published by the Inland Revenue Department (ird.gov.lk, Publications > Acts). Held next to the certified
Act under key "Cons N/YYYY" (billrag/consolidated.py); answers that use one carry a fixed note ("a convenient reference, not a statutory
text ... check the certified Act and its amending Acts"). Made by hand in a session, never by the automated build:

1. `billrag/consolidation.py` `to_pages(pdf, first_body_page, title_prefix)` -> transcript format (pages separated by form feeds).
2. Parse it and compare every section with the certified Act; sections that differ must carry an IRD change note, or be explained.
3. Correct only errors that the certified Act proves (and list them in the review file).
4. The owner accepts it (`accepted_by`); `scripts/refresh_bills.py` then adds it (`python -m billrag add-consolidation`). A newer
   consolidation (later `up_to`) replaces the older one.

Files per Act `N-YYYY`: `.txt` (the text), `.json` (`no`, `year`, `title`, `up_to`, `sections`, `amendments`, `source`, `prepared`,
`reviewed`, `accepted_by`), `.review.md` (what was checked and corrected).
