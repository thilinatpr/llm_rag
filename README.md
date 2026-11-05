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

## Cloudflare Worker with email OTP login

`src/auth.js` (entry: `src/worker.js`) puts an email one-time-code login in front of the site (static `public/` page, or the Python UI via `ORIGIN`).
Codes are sent through the Cloudflare Email Service REST API, so recipients need no verification in Cloudflare;
`ALLOWED_EMAILS` in `wrangler.toml` is the only allowlist.

1. Onboard your sender domain under Cloudflare Email -> Email Sending, and create an API token that can send email.
2. `npx wrangler kv namespace create AUTH` and paste the id into `wrangler.toml`; also set `CF_ACCOUNT_ID`, `ALLOWED_EMAILS`, `FROM_ADDRESS`.
3. `npx wrangler secret put OTP_PEPPER` (any long random string) and `npx wrangler secret put CF_EMAIL_TOKEN`.
4. `npx wrangler deploy`. Tests: `npm test`.

Codes are 6 digits, valid 5 minutes, single use, 5 attempts, one request per address per minute; sessions last 7 days.
