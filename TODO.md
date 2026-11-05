# TODO

Legend: [x] done, [ ] open. See `AGENTS.md` for context.

## Done
- [x] Scrape bill metadata + PDFs from documents.gov.lk (1,521 listing rows, 4,509 PDFs, 0 failures)
- [x] Upload everything to R2 (`gov-rag/bills/`, 4,511 objects; re-run is a no-op)
- [x] Structure-aware parser (sections, parts/chapters, schedules, margin headings, old + new layouts)
- [x] SQLite FTS5 index: 1,513 bills / 42,696 chunks; keyless `search` command
- [x] Hybrid retrieval plumbing (RRF, optional dense, optional LLM rerank, GS/Bill text dedup, diversity cap)
- [x] Agent loop + 5 tools + Anthropic/OpenAI-compatible adapters; 15 offline tests
- [x] Verified live on Cloudflare Workers AI: `@cf/openai/gpt-oss-120b` via the OpenAI-compatible endpoint does multi-step
      tool calling and a correctly cited, source-checked answer (~18 s, 4 tool calls). `bge-reranker-base` and
      `bge-base-en-v1.5` (768-d) respond too; neither is wired into billrag yet.

## Next (in priority order)
- [ ] **Wire a `cloudflare` provider** for embeddings (`/ai/run/@cf/baai/bge-base-en-v1.5`, `{"text":[...]}` ->
      `result.data`) and reranking (`/ai/run/@cf/baai/bge-reranker-base`, `{"query","contexts":[{"text"}]}` ->
      `result.response[{id,score}]`). Note: in a first trial the reranker scores saturated (top 3 all ~0.997), so judge it
      on the eval set, don't assume it helps. Still unverified live: the Anthropic adapter.
- [ ] **Build an eval set** (~30-50 questions with expected bill/section) and a script reporting recall@k for
      BM25 vs hybrid vs +rerank, plus answer-citation checks. Without it, tuning is guesswork.
- [ ] Upload `data/bills/index.sqlite` to R2 (`scripts/upload_r2.py --include-index`) so a fresh session skips ingest.
- [ ] Run `python -m billrag summarize` (LLM bill summaries) and measure whether "which bill is about X?" improves.
- [ ] Rotate the R2 API token: it was pasted into a chat transcript. Revoke it and issue a new one.
- [ ] Agent latency (~18 s for a 4-tool question on gpt-oss-120b): try parallel tool calls, a smaller model for
      routing, or streaming.

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
- [ ] Multilingual question handling (answer in Sinhala/Tamil from English sources) once a model is chosen.
- [ ] Conversation memory limits (the agent keeps full history; trim or summarise for long chats).
