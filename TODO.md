# TODO

Legend: [x] done, [ ] open. See `AGENTS.md` for context.

## Done
- [x] Scrape bill metadata + PDFs from documents.gov.lk (1,521 listing rows, 4,509 PDFs, 0 failures)
- [x] Upload everything to R2 (`gov-rag/bills/`, 4,511 objects; re-run is a no-op)
- [x] Structure-aware parser (sections, parts/chapters, schedules, margin headings, old + new layouts)
- [x] SQLite FTS5 index: 1,513 bills / 42,696 chunks; keyless `search` command
- [x] Hybrid retrieval plumbing (RRF, optional dense, optional LLM rerank, GS/Bill text dedup, diversity cap)
- [x] Agent loop + 5 tools + Anthropic/OpenAI-compatible adapters; 15 offline tests
- [x] Retrieval eval set (`evals/retrieval_eval.jsonl`, 47 reviewed questions) + `python -m billrag eval`.
      Config comparison (n=47, differences < ~0.15 are noise):
        bm25 only            R@1 0.38  R@5 0.66  R@10 0.66  MRR 0.48
        bm25 + rerank        R@1 0.57  R@5 0.72  R@10 0.79  MRR 0.64
        bm25 + dense         R@1 0.47  R@5 0.83  R@10 0.91  MRR 0.62
        bm25 + dense + rerank R@1 0.60 R@5 0.81  R@10 0.91  MRR 0.69   <- best
- [x] Dense + rerank are now the default when Cloudflare credentials exist; missing/stale vectors raise `VectorsError`
      (fingerprinted). Index + vectors uploaded to R2 and restore verified (checksums + eval MRR 0.69 from the copy).
- [x] Bill summaries (365 bills: all 2024-2026 + 64 of 2023) + `find_bills` tool + kind routing; eval grown to 134 items
      (72 recent) with repaired bill-level gold. Recent (2025+): MRR 0.85 -> 0.87, bill-level MRR 0.91 -> 0.96.
- [x] Two-step pipeline (retrieve -> AI grading -> AI summary) is the default engine; `eval-answers` added. On 24 recent
      questions: strict 0.83 vs agent 0.71, 2.2 LLM calls/question; not faster (see AGENTS.md).
- [x] Workers AI providers wired in: `BILLRAG_EMBED_PROVIDER=cloudflare` (bge-base-en-v1.5, 42,696 chunks embedded in ~3 min)
      and `BILLRAG_RERANK=cloudflare` (bge-reranker-base). Agent verified end to end with the best config (~25 s/question).
- [x] Verified live on Cloudflare Workers AI: `@cf/openai/gpt-oss-120b` via the OpenAI-compatible endpoint does multi-step
      tool calling and a correctly cited, source-checked answer (~18 s, 4 tool calls). `bge-reranker-base` and
      `bge-base-en-v1.5` (768-d) respond too; neither is wired into billrag yet.

## Next (in priority order)
- [ ] **Latency**: pipeline median ~12-15 s, p90 ~25-30 s. Try a faster/smaller model for the grading call (e.g. gpt-oss-20b),
      grade fewer candidates (14 -> 8) or truncate passages harder, cap evidence to what scored 3, and stream the answer.
- [ ] Answer eval is only 24 questions and self-judged: widen to all 72 recent items, run 2-3 times for variance, and judge
      with a different model than the one answering.
- [ ] Grader drops (`kept_gold` < `cand_gold`) and boilerplate retrieval misses (identical "qualified auditor" / rules clauses in
      many incorporation bills): when several bills match a boilerplate question, show the candidate bills instead of guessing.
- [x] **One-command refresh for newly published bills** (done: `scripts/refresh_bills.py`) (the users' main use case): scrape -> `ingest` -> `summarize
      --since-year <current-1>` -> incremental `embed` -> `upload_r2.py --include-index`, ideally on a schedule. Each
      step is already resumable/incremental; they just aren't chained. Check how the scraper detects new/changed bills
      (listing has `updatedAt`).
- [ ] Recency prior in ranking (small boost for newer bills when the question gives no year) - measure on `--since-year 2025`;
      today recency is only an instruction in the agent prompt.
- [ ] Grow the *recent* eval slice with real questions (current 72 are LLM-generated) and add answer-level grading.
- [ ] Remaining recent misses are mostly boilerplate-clause ambiguity (identical "qualified auditor" / retirement-age
      clauses in many bills); consider showing the user the candidate bills instead of guessing.
- [ ] **Wire a `cloudflare` provider** for embeddings (`/ai/run/@cf/baai/bge-base-en-v1.5`, `{"text":[...]}` ->
      `result.data`) and reranking (`/ai/run/@cf/baai/bge-reranker-base`, `{"query","contexts":[{"text"}]}` ->
      `result.response[{id,score}]`). Note: in a first trial the reranker scores saturated (top 3 all ~0.997), so judge it
      on the eval set, don't assume it helps. Still unverified live: the Anthropic adapter.
- [ ] Grow/harden the eval set: 47 questions gives roughly +/-0.14 on any rate, so only large differences are
      meaningful. Add real user questions, multi-clause questions, and answer-level grading of `agent.ask` output
      (the stored `answer` field is the reference).
- [ ] Rotate the R2 API token: it was pasted into a chat transcript. Revoke it and issue a new one.
- [ ] Agent latency (~18-25 s for a 4-tool question on gpt-oss-120b; rerank adds ~1 s per search): try parallel tool calls, a smaller model for
      routing, or streaming.

- [ ] Automate refresh: after a re-scrape/ingest, run `embed` + `upload_r2.py --include-index` in one command.
- [ ] Only the *changed* chunks need re-embedding after a small re-ingest; today `embed` redoes all 42k.

## SSCL question: root cause and fixes (from the logs of 2026-11-09)
Live question, asked on both services at 06:54 UTC: "A contract was awarded in 2021. Payments made in 2024. Now contractor asks for
sscl payments. Can we pay". Both answers said the evidence does not settle it: honest, but not useful. No errors and no citation
warnings, and the grader kept the right SSCL clauses (scores 2-3). The causes are what the indexes hold and one indexing bug:
- [ ] **Add the SSCL Act 25/2022 (main cause).** (2026-11-09: checked from the owner's PC: documents.gov.lk lists it with an English PDF and it parses: 61 sections, 96 chunks, levy charged "on or after October, 1, 2022" (the bill said 1 July); no clause on passing the levy to a buyer or on existing contracts. Goes in with the next `refresh-index.yml` build with `force` on GitHub, NOT built locally; that build stops on the ~48 base Acts that cannot be found until they are found or waived.) Acts are indexed from 2024 only (`acts.ACT_SINCE`), so the Social Security Contribution
      Levy Act, No. 25 of 2022 is missing. Only its bills (213/2022 Gazette text, 219/2022 Bill) and amending Acts 15/2024, 24/2025 and
      10/2026 are held, so the answer quoted the 2022 bill. The bill's start date (1 July 2022) may differ from the Act's: from memory
      1 October 2022, not checked. `python -m billrag base-acts` on the live index already reports it: "[NOT FOUND] Social Security
      Contribution Levy Act (25/2022), amended by 6 documents" (one of 78; none fetched, because no refresh has run since the check
      was added). Fix: `scrape_bills.py --source acts --keys 25/2022` + `ingest-acts --keys 25/2022` (or `add-base-act --pdf ...`), then
      `embed`, publish. documents.gov.lk reset connections from the cloud sandbox, so whether it lists 25/2022 with an English PDF is
      not known; try from the local PC. Running the full base-Act step (`refresh_bills.py --force`) would bring in the other ~30
      fetchable base Acts at the same time (see AGENTS.md "Base Acts").
- [x] **`ingest.classify` misses title variants (143 documents, 13 from 2020+).** Fixed in code 2026-11-09: one rule `parse.split_kind` / `plain_title` used by classify, version_tag, shown_text, strip_noise, titles, resolve, store.coverage, build_site; `python -m billrag reclassify` fixes `bills.kind`/`title_norm` in place (chunk text unchanged, so no embed) and every `refresh_bills.py` run does it first. The index changes at the next refresh build + Publish; the display fix needs a container deploy. Was: It recognises only titles ending " - GS" / " - Bill", so
      "Social Security Contribution Levy - (GS)", "... (GS)", "... _ GS", "... GS", "... (Bill)", "... Bill", "... - SUPPLEMENT (Bill)",
      "... BIll" are stored with kind `Other` and a `title_norm` that keeps "gs"/"bill". Effects: the GS text and its Bill land in different
      `bill_meta.family` groups (213/2022 family "213/2022", 219/2022 family "219/2022"), so identical sections are treated as two documents
      (the SSCL evidence carried §3 twice, using up evidence slots), sibling-version linking, `prefer_latest` and eval gold `expand_gold`
      miss them, and Sources lines show the code "(GS)" (`pipeline.version_tag` strips only " - GS"/" - Bill"). Recent ones: 067/2026,
      068/2026, 032/2026, 537/2024, 296/2023, 213/2022, 197/2022, 180/2022, 123/2021, 055/2021, 054/2021, 053/2021, 052/2021.
      Fix: widen the regex in `classify` and in `version_tag` / `strip_noise` / `_HEAD`, add a test, update `bills.kind` + `title_norm` in
      place (no full re-ingest, so chunk ids stay), run `meta` and `titles`; overview chunks whose header contains the title change
      text, so run `embed` (incremental, a few hundred chunks at most) and publish.
- [ ] **Procurement Rules has nothing on taxes / change in legislation in contracts.** (2026-11-09: confirmed in the index: only "taxes/levies shall be included in the cost" of a bid, PM2024 4.2/7.7, and the energy-sector manual's change-in-law; nprocom.gov.lk's SBD pages carry only the 13-page national shopping procedure, so the works/goods SBDs must come from the owner or CIDA.) No source covers who bears a new tax or levy under
      an existing contract (nearest: PF Circular 03/2026, fuel/material price escalation). That rule lives in the contract conditions:
      Standard Procurement Documents (Works, Goods: "changes in laws and regulations") and the Contract Management Guidelines, both
      listed as "Not in the index" in `procurement/README.md`. Fix: add them if the PDFs are available, then `python -m procurement
      build` and an eval question for this case.
- [ ] **Cross-service questions.** (Held until Procurement Rules can answer the contract side: a pointer now would send people to a service that says it does not know.) The question needs both halves: tax law (who owes SSCL: Bill Assistant) and contract terms (whether the
      employer pays it: Procurement Rules). Each service answered only its half. Option: when an answer rests on a tax/levy Act, add a
      line pointing to Procurement Rules for the contract side (and the reverse from Procurement for statutory levies).
- What the indexed text does support (for checking an answer after the fixes): under 219/2022 §3 the levy is charged on the taxable
  person (the contractor) on "the sum receivable whether received or not" in each quarter, so a payment due in 2024 is in the
  contractor's 2024 turnover. The levy text does not put it on the buyer on top of the price: that depends on the contract's
  change-in-law clause.
- Not a cause: the grader's made-up query "SSCL Act No. 25 of 2022 ..." did not restrict retrieval to a missing Act (only the user's
  question restricts, and missing keys are dropped, `pipeline.py` `_ask`).

## Acts
- [ ] **Closing words after a list are joined to its last paragraph** (`parse.Section.text`, every Act and bill). In s.5(1) of 46/1992
      "..., unless such person is registered ..." applies to (a)-(c) but is stored at the end of (c); the same for "shall be guilty of an
      offence" after a list of offences. A model usually reads it right, but the structure is wrong. A fix (keep a line break before
      flush-left text that follows the last list item) changes chunk text for most documents, so it needs a full re-ingest + embed
      (on GitHub, see refresh-index.yml) and an eval run before and after.
- [x] Certified Acts 2024+ in the index, linked to their bills, compared section by section (`billrag/acts.py`; see AGENTS.md). Not live.
- [ ] Refresh: new Acts need scrape -> `ingest-acts` -> `summarize` -> `embed` -> upload, like new bills.
- [ ] Older Acts (2015-2023, ~245): principal Acts of today's amendments, and "did this bill pass?" for older bills.
- [ ] Tamil text of Acts; Sinhala of 2024 Acts (FM Abhaya) and 17 visual-order 2025+ Acts.
- [ ] Compare schedules; label a bill-to-Act link as uncertain when its text score is low.

## Data quality
- [ ] OCR the 2 scanned bills with no text layer (`048/2013`, `011/2012`); OCR also unlocks Sinhala/Tamil.
- [ ] Find out why 9 bills still have no `long_title` (`select key from bills where long_title is null`).
- [ ] Appropriation bills and the 22nd Amendment: add table-aware chunking (per head/programme row) and nested
      Article-level splitting for long quoted amendments, instead of large coarse chunks.
- [ ] Capture `WHEREAS` preambles (purpose statements) and "Statement of Legal Effect" pages as their own chunks.
- [ ] Link GS and Bill versions of the same title explicitly (a `versions` table) and improve `diff_bills` for
      sections renumbered between versions.
- [ ] Re-scrape on a schedule to pick up new bills (the listing has `updatedAt`); make ingest incremental.

## Product
- [ ] Web UI / API wrapper (streaming answers, clickable citations that open the PDF page from R2).
- [x] Translation layer (`billrag/translate.py`, switch `BILLRAG_TRANSLATE=off|script|ai`): any language via one AI detect+translate call, Sinhala/Tamil free by script; answers come back in the question language, citations preserved.
- [x] Official Sinhala text for 2025+ bills (`ingest-si`): 143/180 bills, 3,456 sections, used to word Sinhala answers.
- [ ] Sinhala <= 2024 (FM Abhaya -> Unicode table) and the 35 visual-order 2025 PDFs; Tamil (doubled consonants + viramas).
- [ ] Compare translation models on real Sinhala/Tamil questions (Gemma 3 12B vs M2M100 vs gpt-oss) using the warning reasons in the chat logs.
- [ ] Evaluate the translation layer: a small Sinhala/Tamil question set, back-translation check, which model translates best.
- [ ] Conversation memory limits (the agent keeps full history; trim or summarise for long chats).

## Procurement Rules (procurement.citizenlk.com)
- [x] Eval set + harness (`python -m procurement.evaluate`); heading chain and pointers measured: no harm (deepseek-flash strict 1.00 both, clean 0.95 off / 0.99 on, coarse regexes). See `procurement/README.md`.
- [x] Coverage: "IPC" search term, GPMCM guide added as non-binding; follow-ups (`history`, one rewrite call); `type: "ask"` in the log. Written 2026-11-07, NOT deployed.
- [ ] Write 20+ unseen questions with an officer's review: the current set is a regression check, not an accuracy figure.
- [ ] Rebuild and deploy the container from `main` (`python -m procurement build`, `npx wrangler deploy -c workers/procurement/wrangler.toml`). Needs the owner's go-ahead.
- [ ] Watch payment answers in the logs: gpt-oss overstated what the sources say about a "project accountant"; deepseek-flash did not in the run read.
- [ ] Find NPC's Contract Management Guidelines (PG2024 §1.4(c)) and the works SBD; not on the NPC site as of 2026-11-06. They would replace most of the 2017 guide.
- [ ] Authority/scope tags per clause (who it applies to: PPC, MPC, provincial council, project; value band) so retrieval can filter.
      Largest piece, least certain, and the measurement gave no sign that retrieval is the limit (evidence 0.99): probably not worth doing yet.

## SEO (planned 2026-11-08, nothing built)
Why: the chat and Procurement Rules are behind login, so search engines see only the homepage, which has just a `<title>` (no meta
description, canonical, Open Graph, robots.txt, sitemap or structured data). Real ranking needs public, static pages generated from the index.
- [ ] **Phase 1, basics (about a day). Homepage part DONE on branch `claude/seo` (title, description, canonical, OG, JSON-LD, favicon, robots.txt, sitemap.xml, tests; not deployed). Share image done (`site/og.png`, flat site colour + real-type wordmark and tagline, 24 KB). `noindex` on bills, procurement and admin hosts DONE (`src/seo.js`, X-Robots-Tag on every response, admin robots.txt disallows all; not deployed). Search Console/Bing registration deferred by the owner (2026-11-08), do it later:** homepage title + meta description + canonical + Open Graph/Twitter cards + favicon + 1200x630 share image;
      `robots.txt` + `sitemap.xml` per public Worker (disallow `/api/`, `/login`, `/consent`); `noindex` on login, consent, privacy, admin
      (admin host: disallow all); JSON-LD (`Organization`, `WebSite`, `WebApplication`); register citizenlk.com and bills.citizenlk.com in Google
      Search Console and Bing Webmaster Tools (DNS TXT).
- [ ] **Phase 2, public content pages. PILOT BUILT on branch `claude/seo` (2026-11-08), NOT deployed, not yet in Google:** `scripts/build_site.py` writes 99 Acts (2024+), 49 bill families (2026), 2 list pages and `sitemap-docs.xml` into `site/` (git-ignored); checked through the real home Worker with `wrangler dev --local` and in Chromium at 1100 and 400 px. Open: deploy (run the build first), check indexing after 2-3 weeks, then widen `--bills-since`; Act PDF links need `acts.csv` in R2 (they link the documents.gov.lk Acts list for now); a deep link from a page into the chat with the question filled in. Original plan: build step that generates static `/bills/<NNN-YYYY>` and `/acts/<N-YYYY>` pages from the index (title, date,
      stage/status, long title, "auto-generated" summary, section list, bill -> Act link, versions, "Ask about this bill" button). Pilot: 2026 bills
      + all Acts (~200 pages); check indexing after 2-3 weeks before doing older bills. Reuse the hedged status wording; never state "failed" or
      "became law" more strongly than `acts.status_lines` does. Internal links from `bill_meta`, `section_refs`, `act_links`.
- [ ] Procurement pages (one per guideline/section group, link to source PDF): first check the licence and provenance of the PDFs.
- [ ] Regenerate pages + sitemap (with `lastmod`) after each published index build; ping IndexNow (Bing/Yandex) on new documents.
- [ ] **Phase 3, query targeting:** titles like "<Bill title> - Sri Lanka Bill 2026 | citizenlk", "Act No. N of YYYY", abbreviations (PTSA, OSA);
      per-page FAQ blocks from the Act comparison data. Decide (owner): use official Sinhala titles as `hreflang`/alternate names on 2025+ pages?
      Conflicts with DESIGN.md principle 6 (languages not advertised).
- [ ] **Phase 4, technical:** serve from Worker assets with long cache headers; light server-rendered HTML; load Turnstile and chat code only on
      interaction; one `<h1>` per page; canonical between GS and presented-bill versions; 301s on renamed URLs.
- [ ] **Phase 5, authority and measurement:** admin-panel tiles for pages indexed and queries with impressions (Search Console); link to and get cited
      by documents.gov.lk, civil-society and journalist sources; a few explainer pages on high-interest recent bills; use aggregated chat-log
      topics as keyword research (logs stay private). Optional: `llms.txt`.
- [ ] Before publishing pages: lawyer glance at the disclaimer wording (consent text is not reviewed either); check documents.gov.lk terms on
      republishing text (link to PDFs rather than copy full text); keep "can be incomplete or wrong" and "not government, not legal advice" on every page.
- [ ] Parser: margin notes and the printer's line ("Department of Government Printing, Sri Lanka.") leak into clause text: 211 of 14,788
      clause chunks of 2024+ instruments, 1,053 of 27,923 older ones; on a page where several sections start, the margin notes land on the
      last one. No content is lost; since `pipeline.strip_noise` the model no longer reads most of it, but search still does. Fix in `parse.py`, then `ingest` of the affected bills only (a full ingest renumbers chunk ids, so `embed` would redo nearly all), `embed`,
      `link-acts`, `ingest-si`, the parser quality checks, the 72-item and gist evals, and `upload_r2.py --include-index`.
