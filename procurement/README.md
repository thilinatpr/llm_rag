# Procurement rules assistant (prototype)

Goal: a billrag-style cited Q&A engine for Sri Lankan government procurement officers. Separate from `billrag`: its own
parser, index (`data/procurement/index.sqlite`) and citation labels; it reuses only `billrag.llm` (model adapters, reranker).

```bash
python -m procurement build                       # ~5 s; needs pdftotext (poppler-utils) and the PDFs below
python -m procurement outline PM2024              # every section with its guideline reference and page
python -m procurement search "standstill period" [--all]      # BM25 (+ Workers AI rerank if CLOUDFLARE_API_TOKEN is set)
python -m procurement ask "How long is the standstill period?" [--json]   # BILLRAG_CHAT_* settings, same as billrag
python -m pytest tests/test_procurement.py -q     # offline, synthetic text
```

## Documents and their status
| id | document | file | status |
|---|---|---|---|
| PG2024 | NPC Procurement Guidelines 2024 (Goods, Works, Non-consulting), Gazette 2412/01 of 2024-11-25 | `raw/gazettes/2412-01_E.pdf` | current, from 2025-01-01 |
| PM2024 | NPC Procurement Manual 2024 (same scope), 251 pp | `data/procurement/PROCUREMENT MANUAL 2024.pdf` (owner-supplied, git-ignored) | current |
| CON | Constitution, **Chapter XIXB only** (Art. 156B-156H: NPC composition, functions, powers, meetings, staff, "Government institution"). Parliament Secretariat Revised Edition 2023, as amended up to 31 Oct 2022, English text | `data/procurement/Constitution 2023 (as amended to 2022-10-31).pdf` (parliament.lk/files/pdf/constitution.pdf) | current as of that edition; a later amendment would not be in it |
| (notes) | Gazette 2489/10 of 2026-05-18: amendments to PG2024 | hand-encoded in `sources.AMENDMENTS`, shown under the clauses they change | current |
| PG2006 | Government Procurement Guidelines 2006 (Goods & Works), NPA | `data/procurement/ProcurementGuidelines2006_amded12June.pdf` | transitional |
| PM2006 | Procurement Manual 2006 (with Supplement 8), NPA | `data/procurement/Procurement Manual 2006.pdf` | transitional |

PG2024 §1.4(d)(ii) repeals the 2006 Guidelines, the Manual, and all NPA/Treasury supplements and circulars. Under §1.4(e), a
procurement action begun before 2025-01-01 stays under the 2006 rules until the contract is awarded. So the 2006 documents
are searched only when a question is about the old regime (`ask.OLD`: "2006", "before 2025", "NPA", "TEC", ...). PG2024 §1.4 is
then always shown first. The Treasury "supplementary documents" (Supplements 1-40,
treasury.gov.lk/web/pro-supplementary-documents) amend the 2006 rules, so they are not indexed. The 2018/2019 NPC gazettes
(2070/15, 2144/68) were replaced and cancelled, so they are not indexed either.

## Parsing (`parse.py`); results of `build` on 2026-11-06
- **PG2024:** the contents pages list every clause. Body headings are matched in that order (number + first title words), so
  a "2.7" inside a sentence never starts a clause. **144 of 144 found.** The right-margin "Refer Manual 2.7.1" cage is cut off
  the text and kept as `xref`. DEFINITIONS form one block.
- **PM2024:** each section has a line "(Relevant Reference to the Procurement Guidelines - 2.7.1)" with its heading above it. That
  guideline clause becomes the section's `ref`, so `[PM2024 §2.7.1]` is the manual text that explains `[PG2024 §2.7.1]`. Many
  sections can share a ref (annexures). **144 sections**: 355 chunks, of which ~40 are annexures/forms.
- **PM2006:** "PROCUREMENT GUIDELINE REFERENCE: 2.7.1 (Cont)" with the heading below it; continuations are merged. 87 sections.
- **PG2006:** numbered clauses, sequence-checked, with left-margin notes left in the text. 78 of 78 level-2 clauses.
- Long clauses are split at blank lines (~1,800 chars). The answer step re-joins the whole clause (cap 3,500 chars).

- **CON:** only Chapter XIXB is parsed (`parse_constitution_npc`, 7 articles, cited `[CON Art. 156C]`). Side notes alternate
  margins (right on odd pages, left on even pages); they become the article heading. Searched with the current documents.
  The rest of the Constitution is out of scope here (for billrag it would be a separate reference index).

## Answering (`ask.py`)
1. Retrieval: BM25 with acronym expansion (MPC -> Ministry Procurement Committee), plus the optional Workers AI cross-encoder.
   BM25's top 2 always stay in the result, at second place: the reranker had dropped Art. 156B, BM25's #1 for "who appoints the
   NPC members".
2. Each hit becomes a whole clause, and each guideline clause is paired with its manual section (and vice versa). At most 8 units.
3. ONE model call, citing `[PG2024 §x.y]` / `[PM2024 §x.y]`.
4. A deterministic citation check: a sub-clause of a shown clause counts only if its number occurs in the clause text.
5. Replies with leaked channel tags or degenerate repetition are retried once.

**Table pages (`tables.py`, `tables/PM2024/p*.md`).** pdftotext flattens tables. On 2026-11-06 a live answer turned the BEC fee of
LKR 35,000 into "LKR 35,000 million", because the "(In LKR. Million)" heading belongs to the value bands. So 22 data-table pages
of PM2024 were found on contact sheets of all 252 pages and transcribed from page images (rendered at 170 dpi, cropped to the
text), whole page including prose:

| Pages | Content |
|---|---|
| 57-60 | authority limits |
| 72-76 | committee payments |
| 107-108 | TCE allocations |
| 119-122 | time frames |
| 129-130 | CIDA grades |
| 131 | bid validity |
| 156 | documents per method |
| 164-165 | document fees, bidding periods |
| 198-199 | domestic preference |

Blank forms are not transcribed. A transcription **replaces** the page's extracted text at build time. Units are written
into the column headers, merged cells are repeated on each row, and blank printed cells say "(blank in the printed table)". A
printing error is kept and marked [sic] (p198: "Less than 15% (fifty percent)").
`python -m procurement check-tables` requires every number in a transcription to appear in the PDF's own text layer for
that page, and the other way round: **22 of 22 pages match**. It caught two of my own additions on p58.

**Results on 18 officer-style questions** (gpt-oss-120b on Workers AI, BM25 only, effort low; 3.5-15.5 s each). Checked by
hand against the page images:
- **Core answer right: 18 of 18.** Before the table work the score was 11 of 12, and the price-contingency question also
  failed: the 4-page §4.2 section was cut at 3,500 chars, before its table. Long sections now keep the part that matched.
- **Smaller errors still in the answers:**
  - One BEC row pairs the wrong foreign-funded band. These are the rows whose GOSL cell is blank in print.
  - The shopping answer cites [PG2024 §3.1.5] for limits that are actually in [PM2024 §2.9]. Both were shown to the model,
    so the citation check passes.
- **Fixes that came out of these runs:**
  - Retry on degenerate replies.
  - PG2024 §1.4 is always shown for questions about the old regime.
  - Table-reading rule and abbreviation list in the prompt.
  - A page break ends a heading block (table rows had leaked into headings).
  - "(Procurement Manual Reference - 5.5)" now starts a section: 22 Chapter 05 annexures had been merged into §5.25.

## Open
- **Tables:**
  - The other documents' tables are not transcribed (PG2024 has none; PM2006 has some). Use the same method if the 2006
    documents matter.
  - A citation that names the wrong one of two shown passages is not caught (see above).
  - If NPC reissues the Manual, the page numbers move: rerun `check-tables`. A mismatch means the transcriptions need redoing.
- No eval set yet: turn the 12 questions into gold (doc, ref) items and measure retrieval, as `evals/retrieval_eval.jsonl` does.
- Not in the index: the Consultancy guidelines, Contract Management Guidelines (PG2024 §1.4(c)), Standard Procurement
  Documents, NPC circulars after 2025, and the Sinhala/Tamil texts.
- The amendment gazette is encoded by hand (8 rows). Recheck it if NPC publishes more amendments.
- Supreme Court FR petition (TISL, SCFR/22/2025): status unknown.

## Gazette retrieval (scripts/)
- documents.gov.lk generic server action (same id as `scripts/scrape_bills.py`) with `apiEndpoint=.../extra-gazette/get-all` and a
  text search; files via `/api/content-file-proxy?file=/extra-gazette-content/<name>.pdf`. `gz_search.py "query"`,
  `gz_collect.py`, `gz_download.py 2412/01,... E,S,T`. `raw/gazettes/index.json` = metadata of 9 procurement-related gazettes.
- treasury.gov.lk is reachable from the owner's machine but not from the cloud sandbox (proxy 403).
