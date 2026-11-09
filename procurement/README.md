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
| CIDA... | 20 CIDA rules and regulations under the Construction Industry Development Act No. 33 of 2014 (13 gazettes, 2018-2025; see "CIDA rules and regulations") | `raw/cida/*.pdf` | listed by CIDA |
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

## Dates (written 2026-11-07, NOT deployed)
Answers now get today's date (Sri Lanka time), and `sources.TIME_LIMITS` lists passages with their own end date: PF Circular 03/2026 and its amendment (30 September 2026) and the FR 113(6) delegated authority (31 March 2025). A search of all sources for "valid until"-style wording found these; **a new source with such wording must be added by hand.** Once the date has passed the passage is marked for the model and a fixed note goes under any answer that cites it, so the note does not depend on the model.
The wording is deliberately neutral. The circular has "retrospective effect", so whether it still covers a contract is for the Treasury. A first version told the model the circular "may no longer be available": answers then said flatly that it "can no longer be used", and "Can a price adjustment clause be included in a contract under three months?" flipped to No, although para 03 states what the Guidelines already mean and concerns future bidding documents. Read the answers after changing this wording.
Not handled: amounts or rules that end on a date not listed above, and dates inside a question ("was this allowed in August?").
Thinking counts against `max_tokens`: that same question once used all of 2,500 twice and got "please ask again". The limit is now 5,000 and the retry runs with thinking off.
Measured on the final wording (deepseek-flash, reranker on): 3 questions x 6: no cut-offs, no verdict words, end date stated 18/18, note 18/18; the short-contract question answered Yes 8/8 when read. Eval, 3 runs across the last two wordings: strict 1.00, 0.97, 1.00; clean 0.92-0.95. The miss (q36) and the warnings are citations of passages not shown, not about dates.
Known weakness: an answer can still open with "Yes" and give the date only lower down (q35, FR 113(6), date in its last bullet and the note).

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

**Answer length (2026-11-09, NOT deployed):** a live "how many quotations for shopping?" got 321 words (5 bullets, 2 off the question). The rule now fits the length to the question:
one-fact questions get 1-2 sentences + at most 2 bullets, how/whether/what-if questions keep up to 5; one sentence per bullet; no closing "the passages do not say" line unless asked.
Eval, deepseek-flash, reranker on, 2 runs each, same session: before strict 1.00/1.00, clean 0.92/0.97, median 210 words, 5 bullets, 5.7 s; after strict 0.97/1.00, clean 0.95/1.00,
median 141 words, 3 bullets, 5.0 s. The one strict miss (q39 run 1, a follow-up "It's not conclusive") left out joint inspection / withholding; run 2 had it. The live question: 57-80 words, 3.6-4.4 s, same answer.

**Coverage, payment certification:** PM2024 §9.1 already covers certification (C) and joint inspection (F); the live question missed it because no document
says "IPC" (`index.QUERY_TERMS`). Added GPMCM (Ministry of Finance guide, Feb 2017, chapters 4-10 word for word, `scripts/gpmcm_extract.py`): §8.3.3 lists "unsupported
or undocumented costs" as a ground for withholding. It is non-binding and predates the 2024 rules: at most 2 passages per question, always with PM2024/PG2024 §9.1 (without
this it filled every slot and the model invented rules), and a fixed note in code. Not found: NPC's own Contract Management Guidelines (PG2024 §1.4(c), not in the NPC manifest), the works SBD.
**Follow-ups:** the page sends its last 2 turns (`history`); one extra model call rewrites the question; the server stores nothing; the page shows "Read as: ...".

## CIDA rules and regulations (trial, 2026-11-09, NOT uploaded or deployed)
The Construction Industry Development Authority's "Rules & Regulations" page (cida.gov.lk `pages_e.php?id=82`) lists 13 Gazette Extraordinaries
(2018-2025) under the Construction Industry Development Act, No. 33 of 2014. The English PDFs are in `raw/cida/` (text layer, no OCR needed; the
Sinhala and Tamil PDFs use legacy fonts and are not used). `sources._CIDA` makes one document per **instrument**: 20 (a gazette can hold several),
ids `CIDAQP` ... `CIDAIMS`, 310 chunks. Not added: CIDA's notice about 2430/13 (it only says it takes effect on 1 April 2025, which is in the notes).
- **Parser** (`parse.parse_gazette`): instruments start at "REGULATIONS/RULES made by"; numbered regulations are sequence-checked (accepts "2 Every",
  "3.The", "8. where"); schedules ("FIRST SCHEDULE", "SCHEDULE II", "THE SCHEDULE" printed in parts) are split by their own item numbers. Refs:
  `[CIDACR §3]` = regulation 3, `[CIDACR §S1.6]` = First Schedule item 6 (`ask.CITE` accepts `S`). Removed: printer's job numbers ("07- 394"),
  "L.D.B." lines and the next instrument's "CONSTRUCTION INDUSTRY DEVELOPMENT ACT, No. 33 OF 2014" header (capitals only: the same words in lower
  case can be wrapped clause text). Check: every number printed in each instrument's body is in its chunks (0 missing for all 12 gazettes).
- **Substitution is code, not search order:** Gazette 2430/13 (01.04.2025) repealed and substituted the First and Second Schedules of the
  Registration of Contractors Regulation (2057/5: criteria, grades and financial limits, points, fees) and the fee Schedule of the Registration
  of Adjudicators Regulations (2113/63). `replaces` in `sources._CIDA` puts the 2025 text under the instrument it amends and the 2018 text is
  not indexed (as with `FR_REPLACED`). Each substituted chunk carries the note, including regulation 2 of 2430/13 (registrations made before then
  stay valid for their period). The amending instructions and that saving clause are cut out of the schedules (`stop`; the printed Second
  Schedule has no closing quote).
- **Status:** "current" means "listed by CIDA on 2026-11-09". A later amendment that the page does not list is not here, and the prompt says so.
- **Tables:** the grade, fee and appeal-fee tables (2-3 columns) extract cleanly. The points tables of First Schedule items 8-9 (Tables 8.2-8.7, 9.x;
  up to 14 columns, about 37k characters) are flattened and their columns can be misread: they are not transcribed (the `tables.py` method would fit).
- **Eval** (8 new items c01-c08 in `evals/procurement_eval.jsonl`, gold from the gazette text, `--check-gold` clean; deepseek-flash, reranker on,
  2 runs each, same session):

  | index | 39 earlier items: strict / clean | 8 CIDA items: strict / clean | warned |
  |---|---|---|---|
  | live (R2, 2026-11-09 12:42 upload) | 0.99 / 0.96 | 0.00 / 0.00 | 3 of 94 |
  | live + CIDA (trial) | 1.00 / 0.99 | 1.00 / 1.00 | 1 of 94 |

  So no harm to the earlier questions (gaps are within noise), and q14 (the grade a contract needs, from SUP05) is still answered from SUP05.
  Answers read: all 8 right on the facts. Weak spots: c07 also cited the qualified persons' disciplinary appeal rule (`CIDAQPD §17`) in an answer about
  contractors; c01 gave both "600 > X > 300" (grade table) and "600 ≥ X > 300" (fee table), which the gazette prints differently.
- **To make it live:** on the owner's machine `python -m procurement build` (builds everything, CIDA from `raw/cida/` in git), then
  `scripts/procurement_index.py upload`, then deploy Procurement Rules (`deploy.yml`, target procurement). The trial was built differently (live index
  from R2 + `index.build(docs=sources.CIDA_IDS)`), because the owner's PDFs are not in the sandbox; the result is the same for the other documents.

## Gazette retrieval (scripts/)
- documents.gov.lk generic server action (same id as `scripts/scrape_bills.py`) with `apiEndpoint=.../extra-gazette/get-all` and a
  text search; files via `/api/content-file-proxy?file=/extra-gazette-content/<name>.pdf`. `gz_search.py "query"`,
  `gz_collect.py`, `gz_download.py 2412/01,... E,S,T`. `raw/gazettes/index.json` = metadata of 9 procurement-related gazettes.
- treasury.gov.lk is reachable from the owner's machine but not from the cloud sandbox (proxy 403).

## Old-regime by date, search scope line, model bidding documents (written 2026-11-09, NOT deployed)
- **Why:** a live question ("a contract was awarded in 2021 ... contractor asks for SSCL payments") never searched PG2006/PM2006, because `OLD` only matched words like "2006" or "before 2025"; the answer then said "no 2006 text is among these passages", which read as if we did not hold it. `ask.is_old_regime` = `OLD` or `OLD_BY_DATE`: a year before 2025 within 3 plain words of award / tender / bid / advertis / invit / signed / "procurement started" (a comma or a digit ends the link, so "awarded in 2025, payments in 2024" and "payments in 2024 under a contract signed 2025" are NOT old). It misses "awarded on the 5th of March 2021" (a digit in between).
- **`ask.scope_line`:** the user message says what was searched (not the system prompt: the cache). Not old: 2006 documents NOT searched, say nothing unless the facts suggest an earlier procurement. Old: both searched, and whether a 2006 passage matched.
- **SBDW, SBDG** (`npc_texts/SBDW.md`, `SBDG.md`, kind `template`, note `sources.TEMPLATE`): OUR OWN SUMMARIES (not the publishers' wording) of the ICTAD Works SBD (2007) change in law (cl. 45) and price adjustment (cl. 47), and the NPA Goods NCB SBD general conditions 14, 16, 30. **Copyright:** the ICTAD document says no part may be reproduced without the publisher's permission, and this repo is public, so the first version (verbatim clauses, PR #115) was replaced by summaries; the verbatim text is still in git history (a force-push rewrite would be needed to remove it; owner's decision). Summaries were written from the page images and say "check clause N". The answer never quotes them, tells the reader to check the clause number in the document and in their own contract, and always carries a fixed note. Model documents, not rules: at most `MAX_TEMPLATE` (2) per answer. Searchable for every question (status current). `index.QUERY_TERMS`: sscl, vat, nbt, tax(es), levy/levies also search "change law legislation levy duties price adjustment", since the clauses never say SSCL. **Owner plan:** ask ICTAD for written permission; more SBDs to come, to be summarised the same way.
- **Measured** (scratch index, deepseek-flash, reranker on, no Bill Assistant): eval strict 1.00, clean 0.97 (1 warning), n=39, one run; the SSCL question searches both regimes, shows [SBDW §45] and says no 2006 clause matched. Not measured: the live answer with Bill Assistant's law passages, an eval gold for change-in-law questions (none written yet), SBDG on a goods question (the SSCL question ranked only SBDW).
- **To go live:** `python -m procurement build`, `scripts/procurement_index.py upload`, deploy Procurement Rules (the image bakes `procurement/`).
