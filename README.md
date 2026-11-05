# llm_rag

## Bill chat engine (`billrag`)

A chat-style engine to scrutinise Sri Lankan parliamentary bills from
[documents.gov.lk](https://documents.gov.lk).

```
scripts/scrape_bills.py  ->  data/bills/{bills.json, pdfs/}     (replays the site's Next.js server action)
python -m billrag ingest ->  data/bills/index.sqlite             (structure-aware parse + FTS5/BM25 index)
python -m billrag chat   ->  agentic Q&A with clause citations
```

**What makes it more than embed-and-top-k**

- **Structure-aware chunking.** Bills are split into *sections* (with PART, section number, margin heading and page),
  schedules and a per-bill overview chunk, instead of fixed windows. Every chunk is indexed with a
  contextual header (`Bill 077/2026 - title | PART | §n | heading`), so a clause is findable even when its own text
  never names the bill.
- **Hybrid retrieval.** SQLite FTS5 BM25 + optional dense vectors, fused with reciprocal-rank fusion, optional LLM
  rerank, per-bill diversity cap, and collapsing of identical text across Gazette (GS) / Bill versions.
- **Agentic loop with tools:** `search_bills`, `list_bills`, `get_bill_overview`, `get_section` (read the full clause),
  `diff_bills` (section-level diff, e.g. GS vs presented Bill). The model plans, issues several searches, reads
  clauses, and must cite `[077/2026 §12(2)]`.
- **Provider-agnostic.** Anthropic or any OpenAI-compatible endpoint; embeddings optional and swappable.
  `python -m billrag search "..."` runs retrieval alone with no API key.

```bash
pip install -r requirements-billrag.txt
python scripts/scrape_bills.py                 # ~1.5k bills, ~4.5k PDFs (resumable)
python -m billrag ingest                       # parses the English PDFs
python -m billrag search "detention period of a suspect" -k 5     # no key needed

export ANTHROPIC_API_KEY=...                   # or see billrag/config.py for OpenAI-compatible servers
python -m billrag chat -v
python -m billrag serve                       # simple web chat UI at http://127.0.0.1:8000 (tick "Search only" to skip the LLM; --mode agent for the tool loop)
python -m billrag ask "How does the 2026 terrorism bill limit detention without trial?"

# optional upgrades
python -m billrag summarize --since-year 2024  # LLM bill summaries (improves "which bill is about X?")
BILLRAG_EMBED_PROVIDER=sentence-transformers BILLRAG_EMBED_MODEL=BAAI/bge-small-en-v1.5 python -m billrag embed
```

**Scope and limits**

- English text only. The Sinhala/Tamil PDFs use legacy font encodings and extract as garbage; they would need OCR.
- The corpus is the bill as *published/presented*; committee-stage amendments are not included.
- Answers explain the text; they are not legal advice.
- Tests: `pytest tests` (offline; uses a scripted fake LLM).

### Small-memory hosting (e.g. Cloudflare Containers `lite`, 256 MiB; the deployment now uses `basic`)

Measured on the real index (42,696 chunks x 768 dims; `index.sqlite` 201 MB, `vectors.npz` 114 MB as float32):

- float32 vectors need 131 MB of RAM (plus a temporary copy while loading), which does not fit in 256 MiB with Python.
- `python scripts/shrink_vectors.py` rewrites `vectors.npz` as float16: 61 MB file, 66 MB in RAM. Scoring runs in blocks
  (`retrieve.SCAN_BLOCK`), so there is no full float32 copy. On 400 test queries the top-20 overlap with float32 was 99.8%
  and the top-1 result was identical. New vectors can be written as float16 with `embed_all(..., dtype="float16")`.
- Peak RSS of `python -m billrag serve` after a dense search: ~124 MB. Each LLM SDK adds roughly 35 MB when first used.
- The web UI keeps at most 50 conversations in memory (`web.MAX_SESSIONS`).
- Not measured: speed on a 1/16 vCPU. On a full core, startup work is ~0.9 s (fingerprint 0.16 s, vector load 0.7 s) and a
  vector scan ~0.1 s.

### Chat model in the container

The container uses Cloudflare Workers AI for everything (chat, query embeddings, rerank), so no Anthropic key is needed.
`src/container.js` sets `BILLRAG_CHAT_PROVIDER=openai`, `BILLRAG_CHAT_MODEL=@cf/openai/gpt-oss-120b` (override with a
`CHAT_MODEL` var), the account's `.../ai/v1` base URL, and reuses the `CLOUDFLARE_API_TOKEN` secret as the API key.
Checked end to end locally against the real index: one pipeline question took ~15 s.

## Cloudflare Worker with email OTP login

`src/auth.js` (entry: `src/worker.js`) puts an email one-time-code login in front of the site (static `public/` page, or the Python UI via `ORIGIN`).
Codes are sent through the Cloudflare Email Service REST API, so recipients need no verification in Cloudflare;
`ALLOWED_EMAILS` in `wrangler.toml` is the only allowlist.

1. Onboard your sender domain under Cloudflare Email -> Email Sending, and create an API token that can send email.
2. `npx wrangler kv namespace create AUTH` and paste the id into `wrangler.toml`; also set `CF_ACCOUNT_ID`, `ALLOWED_EMAILS`, `FROM_ADDRESS`.
3. `npx wrangler secret put OTP_PEPPER` (any long random string) and `npx wrangler secret put CF_EMAIL_TOKEN`.
4. `npx wrangler deploy`. Tests: `npm test`.

Codes are 6 digits, valid 5 minutes, single use, 5 attempts, one request per address per minute; sessions last 7 days.
