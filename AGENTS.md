# AGENTS.md

Guidance for AI agents (and humans) working in this repo. See `TODO.md` for open work.

## What this repo is

`billrag`: an agentic, hybrid-retrieval chat engine over Sri Lankan parliamentary bills from
documents.gov.lk. The three `*.ipynb` notebooks are older LangChain/OpenAI RAG experiments (Colab); the live code is
`billrag/`, `scripts/` and `tests/`.

```
scripts/scrape_bills.py   documents.gov.lk -> data/bills/{bills.json,bills.csv,pdfs/<year>/<NNN_YYYY>_<LANG>.pdf}
scripts/upload_r2.py      data/bills/ -> Cloudflare R2 bucket `gov-rag`, prefix `bills/` (idempotent)
billrag/parse.py          PDF -> sections/parts/schedules (structure-aware) + cover metadata
billrag/ingest.py         parse every English PDF -> data/bills/index.sqlite (chunks + FTS5)
billrag/store.py          SQLite schema, BM25 search, section/outline/version queries
billrag/retrieve.py       BM25 (+ optional dense) -> RRF -> optional LLM rerank -> dedup of identical text -> diversity cap
billrag/agent.py          tool-calling loop: search_bills, list_bills, get_bill_overview, get_section, diff_bills
billrag/llm.py            Anthropic + OpenAI-compatible chat adapters, OpenAI/sentence-transformers embedders
billrag/enrich.py         optional: dense vectors (`embed`) and LLM bill summaries (`summarize`)
billrag/config.py         all settings come from BILLRAG_* env vars (documented at the top of the file)
evals/retrieval_eval.jsonl  47 reviewed Q&A items with gold (bill, section); results log in evals/results.jsonl
billrag/evaluate.py       recall@k / MRR metrics; scripts/make_eval.py generates + `--review` prints a draft
tests/test_billrag.py     offline tests (synthetic bills + scripted fake LLM); no network or keys
```

## Commands

```bash
pip install -r requirements-billrag.txt
python scripts/scrape_bills.py [--langs ENGLISH] [--since-year N]   # resumable; ~4.5k PDFs, ~700 MB
python -m billrag ingest [--since-year N]                           # ~1 min for everything
python -m billrag search "query" [-k 5 --year 2024 --bill 077/2026]  # retrieval only, no key needed
python -m billrag chat -v | ask "question"                          # needs a chat model (see config.py)
python -m billrag eval -k 10 --misses 10 --label NAME   # retrieval quality; run before/after any retrieval change
python -m pytest tests -q                                           # must pass before every commit
```

## Rules

- **Secrets:** never write credentials (R2, API keys) into the repo, docs, commits or logs. Read them from env vars
  only (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `ANTHROPIC_API_KEY`, ...).
  `data/` is git-ignored; keep it that way (PDFs are ~700 MB).
- **English only.** Sinhala/Tamil PDFs use legacy font encodings and extract as garbage; do not index them without OCR.
- **Never fabricate legal content.** The agent prompt (`agent.SYSTEM`) requires citations like `[077/2026 §12(2)]`
  and "say so if not found". Do not weaken it. Answers explain text; they are not legal advice.
- Bill keys are `NNN/YYYY` using the listing's number (`billNoText`), not the number printed on the PDF cover
  (`[Bill No. 82]`), which differs. `GS` = Gazette Supplement version, `Bill` = version presented to Parliament; the
  two usually share a title (`title_norm`) and are linked in `get_bill_overview`.
- Changing the parser requires re-running `python -m billrag ingest` and re-checking quality (see below).

## Parser facts learned the hard way (billrag/parse.py)

- Section numbers are sequence-checked (`expected`), which is what keeps false positives out. `match_section` accepts
  doubled numbers (`1.1.`, `33.` for 3), a space before the stop (`13 . (1)`), no stop (`2 (1) Text`), and lower-cased
  text (Appropriation bills).
- Long title: cover layouts vary a lot by era (printed Bill vs Gazette Supplement vs pre-2015), so the fallback is the
  `An Act to ...` text before `BE it enacted`. The enacting clause can read `NOW THEREFORE BE it enacted`.
- Margin notes (side headings) are extracted as short lines after the page body; they are split off and used as the
  section heading only when exactly one section starts on that page. Treat `heading` as a hint, not truth.
- Some Acts embed schedules mid-text; schedule mode exits when the next expected section number (>= 6) appears.
- Quality checks after any parser change (query `data/bills/index.sqlite`): bills with `long_title IS NULL` (now 9),
  bills with `n_sections <= 2` (now 12, mostly Appropriation / constitutional amendments, which are legitimately
  table- or quote-heavy), and `n_pages >= 30` with very few sections.

## Environment notes (cloud sandbox)

- documents.gov.lk data comes from a Next.js server action (`Next-Action` header; ID in `scrape_bills.py`, currently
  `40dd0374...`). If the site redeploys the ID changes: grab it from DevTools -> Network -> `POST /web/bills`, pass
  `--action-id`. The backend (`gvp-api:4500`) is internal; do not try to reach it directly.
- Hugging Face is blocked from the sandbox, so local embedding models cannot be downloaded there; PyPI works.
- Background jobs started with a `timeout` are killed at that limit; the scraper and uploader are resumable, so just
  re-run them.
- `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in the sandbox belong to the sandbox proxy, not R2: run R2 tools with
  `env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY`.

## Chat model on Cloudflare Workers AI (verified)

```bash
export BILLRAG_CHAT_PROVIDER=openai BILLRAG_CHAT_MODEL=@cf/openai/gpt-oss-120b \
  BILLRAG_CHAT_BASE_URL=https://api.cloudflare.com/client/v4/accounts/<account_id>/ai/v1 \
  BILLRAG_CHAT_API_KEY=$CLOUDFLARE_API_TOKEN      # token needs Workers AI permission; R2 keys do not work here
```
- Needs `api.cloudflare.com` in the environment's network allowlist and `CLOUDFLARE_API_TOKEN` as an env secret.
- Workers AI rejects `content: null` on assistant tool-call messages; `llm.to_openai_messages` sends `""` (tested).
- Model list: `GET /accounts/<id>/ai/models/search`. Reranker is listed under task "Text Classification".

## Eval set notes

- Questions were written by an LLM from a single source clause (told to paraphrase) and then reviewed by hand; the gold
  is that clause plus identical clauses in other versions of the same title. Strict single-clause gold means some
  "misses" are defensible alternatives (e.g. several clauses of an amending bill touch the same rule) - read the miss
  list, don't just trust the number.
- Known weakness of the set: it was LLM-generated from clauses, so it favours questions answerable by one clause, and
  many incorporation-of-foundation bills share boilerplate (we dropped most such items). Bills with near-identical
  names (e.g. "Al Hasan" 2013 vs "Alhasan" 2015) make ambiguous gold; drop those items rather than loosen gold.
- Always compare configs on the same file; n=47 means differences under ~0.15 are noise.
