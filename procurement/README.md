# Procurement rules assistant (prototype)

Goal: a billrag-style cited Q&A engine for Sri Lankan government procurement officers. See the repo `AGENTS.md` for the engine.

## Retrieved so far (raw/gazettes/, English; `index.json` = metadata of 9 procurement-related gazettes)
| Gazette | Date | What |
|---|---|---|
| 2412/01 | 2024-11-25 | NPC Procurement Guidelines 2024 (Goods, Works, Non-Consulting), effective 2025-01-01. 60 pp |
| 2489/10 | 2026-05-18 | NPC amendments to the 2024 Guidelines (schedule table: chapter / section / amendment). 2 pp. Signed 9 April 2026 |
| 2144/68 | 2019-10-12 | NPC Procurement Guidelines 2019 (superseded) |
| 2070/15 | 2018-05-09 | NPC Procurement Guidelines 2018 (superseded) |

`*.txt` next to each PDF = `pdftotext -layout` output (untracked scratch, regenerate any time).

## How retrieval works (scripts/)
- Gazettes: documents.gov.lk generic server action (same id as `scripts/scrape_bills.py`) with `apiEndpoint=.../extra-gazette/get-all`
  and a text search; files via `/api/content-file-proxy?file=/extra-gazette-content/<name>.pdf`. `gz_search.py "query"`, `gz_collect.py`, `gz_download.py 2412/01,... E,S,T`.
- Not reachable from the sandbox (proxy 403): `treasury.gov.lk`, `www.documents.gov.lk`. Treasury circulars and the Procurement Manual need another route.

## Open
- Procurement Manual 2024 and Public Finance circulars (2018-2026): not retrieved.
- Amendment gazette is a table of edits, not consolidated text.
- Supreme Court FR petition (TISL, SCFR/22/2025) status unknown.
