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
- [x] Workers AI providers wired in: `BILLRAG_EMBED_PROVIDER=cloudflare` (bge-base-en-v1.5, 42,696 chunks embedded in ~3 min)
      and `BILLRAG_RERANK=cloudflare` (bge-reranker-base). Agent verified end to end with the best config (~25 s/question).
- [x] Verified live on Cloudflare Workers AI: `@cf/openai/gpt-oss-120b` via the OpenAI-compatible endpoint does multi-step
      tool calling and a correctly cited, source-checked answer (~18 s, 4 tool calls). `bge-reranker-base` and
      `bge-base-en-v1.5` (768-d) respond too; neither is wired into billrag yet.

## Next (in priority order)
- [ ] **Wire a `cloudflare` provider** for embeddings (`/ai/run/@cf/baai/bge-base-en-v1.5`, `{"text":[...]}` ->
      `result.data`) and reranking (`/ai/run/@cf/baai/bge-reranker-base`, `{"query","contexts":[{"text"}]}` ->
      `result.response[{id,score}]`). Note: in a first trial the reranker scores saturated (top 3 all ~0.997), so judge it
      on the eval set, don't assume it helps. Still unverified live: the Anthropic adapter.
- [ ] Bill-level "which law is this?" is still the weakest (R@1 0.50, R@10 0.80): try embedding the long title +
      LLM summary as a separate bill-level index (`python -m billrag summarize`), and a `search_bills` mode that
      returns bills instead of clauses.
- [ ] Grow/harden the eval set: 47 questions gives roughly +/-0.14 on any rate, so only large differences are
      meaningful. Add real user questions, multi-clause questions, and answer-level grading of `agent.ask` output
      (the stored `answer` field is the reference).
- [ ] Run `python -m billrag summarize` (LLM bill summaries) and measure whether "which bill is about X?" improves.
- [ ] Rotate the R2 API token: it was pasted into a chat transcript. Revoke it and issue a new one.
- [ ] Agent latency (~18-25 s for a 4-tool question on gpt-oss-120b; rerank adds ~1 s per search): try parallel tool calls, a smaller model for
      routing, or streaming.

- [ ] Automate refresh: after a re-scrape/ingest, run `embed` + `upload_r2.py --include-index` in one command.
- [ ] Only the *changed* chunks need re-embedding after a small re-ingest; today `embed` redoes all 42k.

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
