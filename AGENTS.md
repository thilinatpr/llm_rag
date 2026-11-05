# AGENTS.md

Guidance for AI agents (and humans) working in this repo. See `TODO.md` for open work.

## What this repo is

`billrag`: an agentic, hybrid-retrieval chat engine over Sri Lankan parliamentary bills from
documents.gov.lk. The three `*.ipynb` notebooks are older LangChain/OpenAI RAG experiments (Colab); the live code is
`billrag/`, `scripts/` and `tests/`.

```
scripts/scrape_bills.py   documents.gov.lk -> data/bills/{bills.json,bills.csv,pdfs/<year>/<NNN_YYYY>_<LANG>.pdf}
scripts/upload_r2.py      data/bills/ -> Cloudflare R2 bucket `gov-rag`, prefix `bills/` (idempotent; --include-index adds index+vectors)
scripts/fetch_index.py    R2 -> data/bills/{index.sqlite,vectors.npz}: restore the built index in a fresh session (no ingest/embed)
billrag/parse.py          PDF -> sections/parts/schedules (structure-aware) + cover metadata
billrag/ingest.py         parse every English PDF -> data/bills/index.sqlite (chunks + FTS5)
billrag/store.py          SQLite schema, BM25 search, section/outline/version queries
billrag/retrieve.py       BM25 (+ optional dense) -> RRF -> optional LLM rerank -> dedup of identical text -> diversity cap
billrag/pipeline.py       DEFAULT chat engine, two steps: retrieve -> AI evidence grading -> AI summary (+ citation check)
billrag/answer_eval.py    answer-level eval: cites-the-gold-clause, LLM judge, latency (`billrag eval-answers`)
billrag/translate.py       translation layer: Sinhala/Tamil question -> English -> pipeline -> answer back (script detection, no model call for English)
billrag/agent.py          (`--mode agent`) tool-calling loop: search_bills, list_bills, get_bill_overview, get_section, diff_bills
billrag/llm.py            Anthropic + OpenAI-compatible chat adapters, OpenAI/sentence-transformers embedders
billrag/enrich.py         optional: dense vectors (`embed`) and LLM bill summaries (`summarize`)
billrag/config.py         all settings come from BILLRAG_* env vars (documented at the top of the file)
evals/retrieval_eval.jsonl  134 reviewed Q&A items with gold (bill, section); 72 are from 2025+ bills; log in evals/results.jsonl
billrag/evaluate.py       recall@k / MRR metrics; scripts/make_eval.py generates + `--review` prints a draft
tests/test_billrag.py     offline tests (synthetic bills + scripted fake LLM); no network or keys
```

## Commands

```bash
pip install -r requirements-billrag.txt
python scripts/scrape_bills.py [--langs ENGLISH] [--since-year N]   # resumable; ~4.5k PDFs, ~700 MB
python -m billrag ingest [--since-year N]                           # ~1 min for everything
python -m billrag search "query" [-k 5 --year 2024 --bill 077/2026]  # retrieval only, no key needed
python -m billrag chat -v | ask "question"                          # needs a chat model (see config.py); default = two-step pipeline
python -m billrag ask --mode agent "question"                      # the older free-form tool loop
python -m billrag eval-answers --mode pipeline --since-year 2025 --limit 24   # answer quality + latency (~3 min, ~70 LLM calls)
python -m billrag eval -k 10 --since-year 2025 --misses 10 --label NAME   # retrieval quality on RECENT bills (the ones users ask about)
python -m billrag summarize --since-year 2024 --workers 8   # LLM bill summaries + search keywords (resumable, newest first)
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

## Two-step pipeline (default engine) - how it works and what we learned

`Pipeline.ask`: (1) retrieve clauses (k=14) + bill-level overviews (k=4) with the hybrid retriever, restricted to the bill
if the question contains an explicit `NNN/YYYY`; (2) ONE LLM call grades every candidate 0-3 and says whether the kept
evidence is enough (if not: at most one extra retrieval round with the grader's suggested queries; only NEW candidates
are graded); (3) ONE LLM call writes the answer from the *full section text* of the kept clauses; (4) a deterministic
check flags any citation that is not in the evidence shown to the model (`Result.warnings`). In a chat, follow-ups are
first condensed into a standalone question (+1 call). Typical: 2.2 LLM calls, median ~12-15 s.

Measured on 24 recent (2025+) eval questions, same gold, same judge (`eval-answers`):

| engine | strict* | judged correct | avg s |
|---|---|---|---|
| agent (tool loop) | 0.71 | 18/24 | 13.2 |
| pipeline v4 | 0.83 | 22/24 | 16.0 |
| pipeline v5 (answer effort low) | 0.83 | 23/24 | 15.7 |

*strict = judge says correct AND the answer cites the gold clause/bill (or a sibling GS/Bill version of it). n=24 and the
judge is the same model that answers, so treat gaps under ~0.1 as noise. The pipeline is not faster than the agent.

Lessons (all cost us a debugging round):
- **Reasoning models sometimes answer JSON requests in prose** ("analysis: we need to..."). Grader and judge retry once with a
  firmer prompt before falling back; `parse_json` scans for the first valid object. Never rely on a single parse.
- **Don't make the grader strict about bill names.** A "different bill scores <= 1" rule dropped the correct clause when the
  question paraphrased the title (strict 0.88 -> 0.67). The grader only uses the title as a tie-break; the answer prompt makes
  the model name the bill it actually relied on.
- **The judge cannot see wrong-bill answers** (lookalike boilerplate clauses give the right text from the wrong bill), so
  always read `strict` / `cited_gold`, not just "correct". `expand_gold` counts the same section of sibling GS/Bill versions.
- Remaining failures are retrieval misses on boilerplate-heavy incorporation bills and a few grader drops (`kept_gold` 0.88-0.96
  vs `cand_gold` 0.96): check `cand_gold` vs `kept_gold` to see whether the retriever or the grader lost the clause.
- `BILLRAG_GRADE_EFFORT=low` (default) makes grading ~3x faster; low effort on the answer call did not help (latency is dominated
  by hosted-model variance). Models without `reasoning_effort` support: set `BILLRAG_GRADE_EFFORT=` (empty).
- Models may emit citations in full-width brackets 【..】; the verifier accepts both.

## Translation layer (any language -> English -> any language)

`BILLRAG_TRANSLATE` switch: `off` | `script` (default) | `ai`. `script`: `translate.detect` recognises Sinhala/Tamil from the
Unicode script (no model call; English costs nothing extra). `ai`: everything else first goes through ONE call
(`translate.detect_ai`, JSON `{language, english}`) that names the language and translates, so any language and romanised
Sinhala/Tamil work, at +1 call on every non-Sinhala/Tamil question (English included). A non-English question then costs one
more call to translate the answer back. The English answer is citation-verified first and kept in `Result.answer_en`; the
translation must reproduce every `[NNN/YYYY §s]` citation exactly, else we return the English answer with a warning. If `ai`
detection returns unusable output, the question is treated as English (warning). Bill titles stay English (English-only index).
Translation quality is unmeasured: no non-English eval set yet.

## Product priority: recent bills

Users mostly ask about **newly published bills** and rarely about old ones. So: report eval numbers on the recent slice
(`--since-year 2025`) first; spend LLM/embedding budget newest-first (`summarize` processes bills newest-first, so it is
safe to stop early - summaries currently exist for all 2024-2026 bills and 64 of 2023, none older); the agent prompt
tells the model to prefer the newest matching bill and cite dates. Don't optimise for 2003-2020 bills at the cost of
recent ones.

## Retrieval routing, summaries, versions (learned the hard way)

- **Route by chunk kind.** `search_bills` searches clauses/schedules only (`kinds=("section","schedule")`);
  `find_bills` searches `overview` chunks only. When summaries were first added to overview chunks and everything was
  searched together, recent R@1 dropped 0.78 -> 0.67 because rich overview chunks crowded out the real clause.
  `billrag eval` mirrors this (`--route split`, default; `--route none` searches all kinds).
- **Summaries** are LLM-written (`@cf/openai/gpt-oss-120b`, ~110 words + 8-12 keywords), stored in `bills.summary`, folded
  into the overview chunk, labelled "auto-generated" everywhere they are shown, and survive a re-ingest. The prompt
  forbids outside knowledge - a first pilot added a UN resolution number that is not in the bill. Never present a
  summary as authoritative; the agent must verify details in the clauses.
- **Effect (n=24 recent bill-level questions):** MRR 0.91 -> 0.96, R@1 0.88 -> 0.92. Real but small; baseline was
  already strong. Old bills (no summaries) are unchanged.
- **`Store.versions(title_norm, near_date)`**: titles like "Finance (Amendment)" recur every year (26 Code of Criminal
  Procedure amendments!). Always pass the bill's date so only its own GS/Bill versions (within 300 days) count. The first
  eval gold labels forgot this and were wildly generous; they were repaired and bill-level numbers dropped accordingly.
- `embed` is incremental: it reuses vectors whose (id, text-hash) is unchanged, so re-embedding after `summarize` costs
  a few hundred chunks, not 42k. The same `model` must be used or everything is re-embedded.

## Eval set notes

- Questions were written by an LLM from a single source clause (told to paraphrase) and then reviewed by hand; the gold
  is that clause plus identical clauses in other versions of the same title. Strict single-clause gold means some
  "misses" are defensible alternatives (e.g. several clauses of an amending bill touch the same rule) - read the miss
  list, don't just trust the number.
- Known weakness of the set: it was LLM-generated from clauses, so it favours questions answerable by one clause, and
  many incorporation-of-foundation bills share boilerplate (we dropped most such items). Bills with near-identical
  names (e.g. "Al Hasan" 2013 vs "Alhasan" 2015) make ambiguous gold; drop those items rather than loosen gold.
- Always compare configs on the same file. With 72 recent / 134 total items, differences under ~0.05-0.10 are noise.
- Bill-level gold = the bill's own versions within 300 days (+ hand-added spelling-variant siblings, see `review_note`).
  Drop questions that are truly ambiguous (e.g. two bills amending the same Act in one year) instead of loosening gold.

## Retrieval config and the index in R2

**Defaults:** if `CLOUDFLARE_API_TOKEN` and an account id (`BILLRAG_CF_ACCOUNT_ID` / `CLOUDFLARE_ACCOUNT_ID` /
`R2_ACCOUNT_ID`) are set, dense embeddings (`@cf/baai/bge-base-en-v1.5`) and the cross-encoder
(`@cf/baai/bge-reranker-base`) turn on automatically; otherwise keyword search only. Override with
`BILLRAG_EMBED_PROVIDER=none` / `BILLRAG_RERANK=none`. `python -m billrag eval` prints `rerank=... dense=on|off`.

Measured on `evals/retrieval_eval.jsonl`: BM25 only MRR 0.48 -> +rerank 0.64 -> +dense 0.62 -> +dense+rerank 0.69
(R@10 0.91). Pure cross-encoder order beat the RRF-fused variant (`cloudflare-fused`).

**Fresh session (fastest path):**
```bash
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/fetch_index.py --bucket gov-rag   # R2_* env vars
python -m billrag eval -k 10                                                                      # sanity: MRR ~0.69
```
**Rebuilding:** `ingest` then `embed` (~10M tokens, ~3 min, ~1 min for ingest), then
`python scripts/upload_r2.py --bucket gov-rag --include-index` so the next session can fetch it.

- **Fail-loud rule:** `vectors.npz` carries a fingerprint of the exact index it was built from
  (`Store.fingerprint()`: hash of every chunk id + text). If dense is enabled and the file is missing, stale after a
  re-ingest, or unstamped, the retriever raises `VectorsError` (CLI: exit 1 with the fix). Never bypass it; re-run `embed`.
  `ingest` prints a NOTE when it leaves existing vectors stale.
- Reranking is best-effort: on any API error the first-stage order is kept. Missing Cloudflare credentials with
  `BILLRAG_RERANK=cloudflare` set explicitly is an error at startup.
- `index.sqlite` (201 MB) and `vectors.npz` (114 MB) are git-ignored; R2 `gov-rag/bills/{index.sqlite,vectors.npz}` is
  the shared copy. Upload always overwrites them (size is not a reliable change check).
