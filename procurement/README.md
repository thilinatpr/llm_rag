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

Hosted at https://procurement.citizenlk.com behind the login (`procurement/serve.py`, `workers/procurement/`; see AGENTS.md "citizenlk.com layout").

## Documents and their status
| id | document | file | status |
|---|---|---|---|
| PG2024 | NPC Procurement Guidelines 2024 (Goods, Works, Non-consulting), Gazette 2412/01 of 2024-11-25 | `raw/gazettes/2412-01_E.pdf` | current, from 2025-01-01 |
| PM2024 | NPC Procurement Manual 2024 (same scope), 251 pp | `data/procurement/PROCUREMENT MANUAL 2024.pdf` (owner-supplied, git-ignored) | current |
| CON | Constitution, **Chapter XIXB only** (Art. 156B-156H: NPC composition, functions, powers, meetings, staff, "Government institution"). Parliament Secretariat Revised Edition 2023, as amended up to 31 Oct 2022, English text | `data/procurement/Constitution 2023 (as amended to 2022-10-31).pdf` (parliament.lk/files/pdf/constitution.pdf) | current as of that edition; a later amendment would not be in it |
| (notes) | Gazette 2489/10 of 2026-05-18: amendments to PG2024 | hand-encoded in `sources.AMENDMENTS`, shown under the clauses they change | current |
| PG2006 | Government Procurement Guidelines 2006 (Goods & Works), NPA | `data/procurement/ProcurementGuidelines2006_amded12June.pdf` | transitional |
| PM2006 | Procurement Manual 2006 (with Supplement 8), NPA | `data/procurement/Procurement Manual 2006.pdf` | transitional |

**Financial Regulations (FR):** treasury.gov.lk Resource Center -> FR Manual
(`/web/resource-center/section/fr manual`, file `/api/file/88bd6063-...`), saved as
`data/procurement/treasury/Financial Regulations (FR Manual).pdf`. This is the 1992 edition (amendments to 31.12.1991, issued
29.10.1993), 344 pages. `parse_fr` makes one chunk per regulation: 471 regulations, plus 16 out-of-sequence numbers dropped
by a longest-increasing-run check (misprints like F.R.487 between 77 and 84). Cited as [FR 135]. **Chapter XIII (supplies,
tenders, works) is not in this edition.** Later FR amendments by circulars are not included, and the prompt says so. Useful
here: FR 135 delegation (which Manual 2.9 B relies on), FR 137 approving officer, FR 101-112 losses, vouchers and payments,
and stores.

**FR amendments and the PFM Act** (web search + treasury.gov.lk Public Finance circular listings 2020-2026, 2026-11-06):
- **PFMA:** Public Financial Management Act No. 44 of 2024, from treasury.gov.lk/web/public-financial-management-act.
  Parsed with `billrag.parse` into 71 sections, cited [PFMA §67]. Under s. 67(6) the FR 1992 and existing circulars stay in
  force until regulations are made, where they are not inconsistent with the Act (PF Circular 03/2024, indexed as PFC0324).
- **FR amendments (unofficial translation):** PF Circular **01/2020** (28.08.2020) consolidates all PF and Treasury Operations
  circulars that amended FR 1992. Its amendments are 01/2020 (i) (12.01.2022), (ii) (20.12.2022, English, revoked by (III)), (III)
  (31.05.2024), (IV) (30.08.2024), 01/2024 and 02/2024 (29.05.2024) and 01/2024 (I) (24.01.2025). Except (ii) they are published in
  Sinhala and Tamil only, and we assume no official English version exists. All 43 scanned pages (`data/procurement/treasury/
  fr_amendments/`) were translated from the page images into `npc_texts/FRA.md` (Part I and the FR changes, by FR number:
  `[FRA 108]`) and `npc_texts/PFC0120.md` (Part II by paragraph: `[PFC0120 §4]`). **No Sinhala reader has checked them yet.**
  - Every chunk carries `sources.UNOFFICIAL`, and an answer citing these documents always gets a fixed note in code (`ask.TRANSLATION_NOTE`).
  - The 1992 text they replaced outright (FR 104(1)-(2), 108, 269, 371, 855; `sources.FR_REPLACED`) is cut from the index: a note alone
    did not stop the model quoting old amounts. Other amended FRs keep their text, marked SUPERSEDED IN PART when FRA is shown.
  - Open point in the circulars themselves: 01/2024 (I) does not say which limit applies to advance "B" loan write-offs after 31.03.2025.
- **Procurement circulars, English scans transcribed:**
  - PFC0326: PF 03/2026, relief measures valid until 30.09.2026; amended by PFC0326i.
  - PFC0126: PF 01/2026, procurement before budget allocation, no award without a confirmed budget, FR 136(3).
  - PFC0426: PF 04/2026, beneficial ownership disclosure in Ditwah procurements over Rs. 20 Mn.
- A passage that refers to an FR ("Financial Regulation (FR) 136 (3)") brings that regulation into the evidence.

**From nprocom.gov.lk (NPC site, fetched 2026-11-06; `scripts/npc_fetch.py`):**

| id | document | text from | status |
|---|---|---|---|
| SUP01-05, SUP07 | Supplements to the Manual 2024 (18.09.2025 - 24.09.2026): VRC/PAC payments (01), Non-collusion Declaration + intention to award within 7 days (02), no unsolicited proposals (03), Acceptance Committee payments (04), new CIDA grade tables (05), direct contracting with Government Institutions under LKR 2 Mn (07) | `npc_texts/SUP*.md`: text layer; SUP04 transcribed from the scan | current; shown next to the Manual/Guidelines section they change, and the amended Manual chunks carry a note |
| CG2007 + CGS01 | Guidelines for Selection and Employment of Consultants (Aug 2007) + Supplement 01 (12.12.2025, revised thresholds and payments) | PDF / `npc_texts/CGS01.md` | current for consulting services |
| NPCC2024, NPCC2023 | NPC Circulars 01/2024 (2024 Guidelines in force 01.01.2025) and 01/2023 (interim, historical) | `npc_texts/`, transcribed from the scans | current / historical |
| PFC0326i | Public Finance Circular 03/2026 (i), 13.08.2026: relief for conflict/cyclone price escalation | `npc_texts/PFC0326i.md`, transcribed from the scan | current (temporary) |
| LCC2026 | Detailed Guidance Note on Life Cycle Cost / Total Cost of Ownership (88 pp) | PDF | current |
| ESM2026 | Sector Specific Procurement Manual 2026, electricity investor selection (77 pp) | PDF | current from 22.10.2026 |

The site shows documents in a "3D FlipBook" plugin: titles and PDF URLs are base64 JSON in `FB3D_CLIENT_DATA.push(...)`. The
WordPress media API reports 65 uploads but returns only 3, and the pages have no plain PDF links. Not indexed: Supplement 06
(Sinhala/Tamil only), NPC Circular 01/2026 and the 2026 procurement-plans circular (Sinhala only), the FAQ (Sinhala only), the
draft 2026 consultancy guidelines, the Arbitration Act, PF Circular 03/2026 itself (only its amendment is on the site). Manual
2024 has no §8.2 section in print (8.1 jumps to 8.4); Supplement 02 supplies it and is found through PG2024 §8.2.

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

**NPC documents, 11 questions** (2026-11-06, same settings): all 11 right on the facts, e.g. intention to award within 7
calendar days [SUP02], CIDA "C2 and above" for 1500 Mn / 18 months [SUP05; the Manual table alone said "C2"], Acceptance
Committee 8,000 / 6,000 for LKR 300 Mn [SUP04], CPCM < Rs. 250 Mn for nationally advertised QCBS [CGS01]. One false
citation warning (paragraph numbers of a one-passage circular), fixed: a whole-document passage accepts any paragraph.

## Open
- **Tables:**
  - The other documents' tables are not transcribed (PG2024 has none; PM2006 has some). Use the same method if the 2006
    documents matter.
  - A citation that names the wrong one of two shown passages is not caught (see above).
  - If NPC reissues the Manual, the page numbers move: rerun `check-tables`. A mismatch means the transcriptions need redoing.
- Not in the index: the Consultancy guidelines, Contract Management Guidelines (PG2024 §1.4(c)), Standard Procurement
  Documents, NPC circulars after 2025, and the Sinhala/Tamil texts.
- The amendment gazette is encoded by hand (8 rows). Recheck it if NPC publishes more amendments.
- Supreme Court FR petition (TISL, SCFR/22/2025): status unknown.

## Eval (`python -m procurement.evaluate`, `evals/procurement_eval.jsonl`, 2026-11-07)
39 items: 35 officer-style questions recovered from an earlier session (the old "38 questions" were never committed, nor was any gold), the
live payment questions of 2026-11-06 and one follow-up. Gold is written from the source; `--check-gold` fails if a required fact is not in
its gold passage. No model judge: `strict` = required facts + cites gold; `clean` = strict + no unsupported citation. Regexes are coarse: a
pass does not prove the whole answer is right (below), so read the answers. n is small: treat gaps under ~0.1 as noise.

Final code, all 39 items, reranker on, 2026-11-07:
| model, setting | answers | evidence | cited | facts | strict | clean | warned |
|---|---|---|---|---|---|---|---|
| `deepseek-flash` (production), chain + pointers off | 78 | 1.00 | 1.00 | 1.00 | 1.00 | 0.95 | 0.08 |
| `deepseek-flash` (production), chain + pointers on | 78 | 1.00 | 1.00 | 1.00 | 1.00 | 0.99 | 0.03 |
| `gpt-oss-120b`, chain + pointers on | 117 | 0.99 | 0.97 | 0.88 | 0.86 | 0.83 | 0.10 |
So chain and pointers do no harm; the clean gap (3 fewer unsupported citations) is within noise. 156 DeepSeek answers cost about $0.08. **The perfect strict score overstates quality:** the
regexes are coarse and were written after reading a draft run of these questions on this model, and most questions also trained earlier fixes. It is a regression check, not an accuracy figure. A
new set of unseen questions, reviewed by an officer, is what would measure accuracy. gpt-oss overstated what the sources say about a "project accountant" (no document names the role); deepseek-flash's
answers on q36-q39 said so plainly in the run read, with n too small to say that always holds.

**Coverage, payment certification:** PM2024 §9.1 already covers certification (C) and joint inspection (F); the live question missed it because no document
says "IPC" (`index.QUERY_TERMS`). Added GPMCM (Ministry of Finance guide, Feb 2017, chapters 4-10 word for word, `scripts/gpmcm_extract.py`): §8.3.3 lists "unsupported
or undocumented costs" as a ground for withholding. It is non-binding and predates the 2024 rules: at most 2 passages per question, always with PM2024/PG2024 §9.1 (without
this it filled every slot and the model invented rules), and a fixed note in code. Not found: NPC's own Contract Management Guidelines (PG2024 §1.4(c), not in the NPC manifest), the works SBD.
**Follow-ups:** the page sends its last 2 turns (`history`); one extra model call rewrites the question; the server stores nothing; the page shows "Read as: ...".

## Gazette retrieval (scripts/)
- documents.gov.lk generic server action (same id as `scripts/scrape_bills.py`) with `apiEndpoint=.../extra-gazette/get-all` and a
  text search; files via `/api/content-file-proxy?file=/extra-gazette-content/<name>.pdf`. `gz_search.py "query"`,
  `gz_collect.py`, `gz_download.py 2412/01,... E,S,T`. `raw/gazettes/index.json` = metadata of 9 procurement-related gazettes.
- treasury.gov.lk is reachable from the owner's machine but not from the cloud sandbox (proxy 403).
