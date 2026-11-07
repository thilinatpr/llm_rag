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

## Acts
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
- [ ] **Phase 1, basics (about a day). Homepage part DONE on branch `claude/seo` (title, description, canonical, OG, JSON-LD, favicon, robots.txt, sitemap.xml, tests; not deployed). Share image done (`site/og.png`, flat site colour + real-type wordmark and tagline, 24 KB). `noindex` on bills, procurement and admin hosts DONE (`src/seo.js`, X-Robots-Tag on every response, admin robots.txt disallows all; not deployed). Still open: Search Console/Bing:** homepage title + meta description + canonical + Open Graph/Twitter cards + favicon + 1200x630 share image;
      `robots.txt` + `sitemap.xml` per public Worker (disallow `/api/`, `/login`, `/consent`); `noindex` on login, consent, privacy, admin
      (admin host: disallow all); JSON-LD (`Organization`, `WebSite`, `WebApplication`); register citizenlk.com and bills.citizenlk.com in Google
      Search Console and Bing Webmaster Tools (DNS TXT).
- [ ] **Phase 2, public content pages:** build step that generates static `/bills/<NNN-YYYY>` and `/acts/<N-YYYY>` pages from the index (title, date,
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
