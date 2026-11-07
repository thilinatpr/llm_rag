# AGENTS.md

Guidance for AI agents (and humans) working in this repo. See `TODO.md` for open work.

## What this repo is

Visual design for citizenlk.com pages is in `DESIGN.md`; the homepage is `site/index.html` (static, Worker `citizenlk-home`); the chat page
(`public/index.html`) and sign-in page (`LOGIN_PAGE` in `src/auth.js`) follow it.

`billrag`: an agentic, hybrid-retrieval chat engine over Sri Lankan parliamentary bills from
documents.gov.lk. The three `*.ipynb` notebooks are older LangChain/OpenAI RAG experiments (Colab); the live code is
`billrag/`, `scripts/` and `tests/`.

```
scripts/scrape_bills.py   documents.gov.lk -> data/bills/{bills.json,bills.csv,pdfs/<year>/<NNN_YYYY>_<LANG>.pdf}
scripts/scrape_bills.py --source acts   documents.gov.lk/web/acts -> data/acts/{acts.json,acts.csv,pdfs/<year>/<N_YYYY>_<LANG>.pdf}
scripts/upload_r2.py      data/bills/ -> Cloudflare R2 bucket `gov-rag`, prefix `bills/` (idempotent; --include-index publishes index+vectors as a checked pair)
scripts/fetch_index.py    R2 -> data/bills/{index.sqlite,vectors.npz}: restore the built index in a fresh session (no ingest/embed)
billrag/parse.py          PDF -> sections/parts/schedules (structure-aware) + cover metadata
billrag/ingest.py         parse every English PDF -> data/bills/index.sqlite (chunks + FTS5); `ingest-acts` adds certified Acts
billrag/acts.py           Acts next to bills: bill -> Act links, section-by-section comparison, the status line answers get
billrag/store.py          SQLite schema, BM25 search, section/outline/version queries
billrag/retrieve.py       BM25 (+ optional dense) -> RRF -> optional LLM rerank -> dedup of identical text -> diversity cap
billrag/pipeline.py       DEFAULT chat engine, two steps: retrieve -> AI evidence grading -> AI summary (+ citation check)
billrag/answer_eval.py    answer-level eval: cites-the-gold-clause, LLM judge, latency (`billrag eval-answers`)
billrag/translate.py       translation layer: question -> English -> pipeline -> answer back; garble check, quote alignment to official text
billrag/parse_si.py       official Sinhala text per section (Unicode PDFs, 2025+) -> table local_sections; `ingest-si`
billrag/glossary.py       English <-> official Sinhala titles + defined terms mined from local_sections -> table glossary
billrag/agent.py          (`--mode agent`) tool-calling loop: search_bills, list_bills, get_bill_overview, get_section, diff_bills
billrag/llm.py            Anthropic + OpenAI-compatible chat adapters, FallbackLLM (backup model when the provider fails), OpenAI/sentence-transformers embedders
billrag/enrich.py         optional: dense vectors (`embed`) and LLM bill summaries (`summarize`)
billrag/config.py         all settings come from BILLRAG_* env vars (documented at the top of the file)
src/chatconfig.js         which chat model/provider the container gets (CHAT_PROVIDER var: workers-ai | deepseek); pure function, tested in test/worker.test.mjs
procurement/evaluate.py   Procurement Rules answer eval: gold in evals/procurement_eval.jsonl, no model judge
billrag/meta.py           bill metadata outside `chunks`: families, newest instance, bill type, tags, section cross-references (`meta`, `tag`)
billrag/titles.py         title index (table `title_index`) + custom title ranker: which bill/Act does a gist question name? one / ask which / none
src/refresh.js, scripts/refresh_r2.py, .github/workflows/refresh-index.yml   daily new-bills check + Build/Publish buttons (see "Scheduled refresh")
billrag/resolve.py        question grounding before retrieval: greetings, abbreviation (PTSA) -> bill title matching, "closest bills"
scripts/build_site.py     SEO: public pages (/acts/, /bills/ + sitemap-docs.xml) from the published index into site/ (git-ignored); run before every deploy of `citizenlk-home`
src/seo.js                noindex + robots.txt wrapper for the three login-gated Workers (bills, procurement, admin)
billrag/chatlog.py        one JSON record per chat turn -> R2 `chatlogs/` + local JSONL; `build_id()` = hash of the running code
billrag/web.py            `python -m billrag serve`: stdlib HTTP API (/api/ask, /api/ask/stream, /api/search, /api/stats, /healthz incl. `coverage`); secret-gated
billrag/resources.py      CPU/memory: `snapshot()` for /api/stats, `Usage` per request -> log field `usage` (stdlib, /proc + cgroup)
billrag/gate.py           admission control for `serve`: N questions at once + bounded FIFO queue (503 when full); separate `public` lane
src/public.js, quota.js, quota-do.js, util.js   public (anonymous) lane: Turnstile + signed cookie + daily quotas (Durable Object `QuotaCounter`)
billrag/calltrace.py      one record per model / Workers AI call (step, model, seconds, tokens, cache hits, USD, retries) -> log fields `calls`, `cost`
public/index.html         the chat page (static asset, served by the Worker behind the login; also used by local `serve`)
src/worker.js             Worker entrypoint: login gate (auth.js) + forwards /api/* and /healthz to the container
src/privacy.js            the /privacy page of each service, open without sign-in; keep it true when logging or providers change
src/auth.js               email one-time-code login (KV codes/sessions, Cloudflare Email Service REST sender), routing
src/container.js          `BillragContainer` (Durable Object): starts the Python container, passes env/secrets to it
Dockerfile, docker/entrypoint.sh, requirements-serve.txt   the container image (restores the index from R2, then serves)
wrangler.toml             Worker + assets + KV + container + Durable Object config; test/worker.test.mjs = Node tests (`npm test`)
scripts/refresh_bills.py  one command for newly published bills: copy live index -> scrape -> only the new documents -> checks -> `--publish`
scripts/load_test.py      how many people at once (closed loop), also a stub chat model (`--stub-model`, `--fail 402`); rows in evals/load_test_*.jsonl
workers/admin/worker.js, src/admin.js, admin-stats.js   the admin panel, its own Worker: To do list, tiles, one page per source, question reader (see "Admin panel")
src/balance.js            hourly DeepSeek credit check (cron on `gov-rag-llm`) -> email to ALERT_EMAIL below BALANCE_ALERT_USD
scripts/read_chatlogs.py  download + summarise the chat logs from R2;  scripts/shrink_vectors.py = float16 vectors (half the RAM)
evals/answer_model_comparison/   per-model answer-eval rows and reports (see "Answer model comparison")
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
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/fetch_index.py --bucket gov-rag --pdfs SINHALA --since-year 2025 --no-index
python -m billrag ingest-si [--since-year 2025]                     # link official Sinhala sections (~3 min; needs pymupdf)
python -m billrag glossary                                          # rebuild the glossary (~5 s; ingest-si also rebuilds it)
python scripts/scrape_bills.py --source acts --since-year 2024 --langs ENGLISH SINHALA   # then:
python -m billrag ingest-acts && python -m billrag summarize --since-year 2024 && python -m billrag embed && python scripts/shrink_vectors.py
python -m billrag link-acts                                         # recompute links + comparison only (~1 min, no model)
set -a; . ./.local.env; set +a; env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY .venv/bin/python scripts/refresh_bills.py [--publish]   # add new bills (see "Refreshing the index")
python scripts/eval_acts.py                                         # bill-vs-Act answer check, 12 questions; read the answers
python -m pytest tests -q                                           # must pass before every commit
```

## Production: architecture, Cloudflare, deploying, logs

**Live site:** https://bills.citizenlk.com (login required; only addresses in `ALLOWED_EMAILS` can sign in). The old workers.dev
address and preview URLs are off (`workers_dev = false`, `preview_urls = false`).

### citizenlk.com layout
One subdomain per service, each its own Worker (custom domains in that Worker's Wrangler config), all in this repo:
| address | Worker | config | notes |
|---|---|---|---|
| `citizenlk.com`, `www.` (redirect) | `citizenlk-home` | `workers/home/wrangler.toml` | static `site/`, no login, no container |
| `bills.citizenlk.com` | `gov-rag-llm` | `wrangler.toml` | Bill Assistant |
| `admin.citizenlk.com` | `citizenlk-admin` | `workers/admin/wrangler.toml` | admin panel, no container |
| `procurement.citizenlk.com` | `citizenlk-procurement` | `workers/procurement/wrangler.toml` | Procurement Rules: `procurement/serve.py` in its own `lite` container |
Deploy the homepage with `npx wrangler deploy -c workers/home/wrangler.toml`. Sessions are host-only cookies: each service has its own sign-in.
Procurement Rules: `python -m procurement build`, then `npx wrangler deploy -c workers/procurement/wrangler.toml` from the main checkout
(the index is baked into the image from `data/procurement/index.sqlite`). The container has no Cloudflare or R2 credentials: chat is DeepSeek,
reranking calls the Worker's `/internal/rerank` (AI binding, `X-Proxy-Secret`). Without the reranker, 3 of 38 test questions missed the passage
that answers them. Its Worker writes one JSON record per question to R2 `procurement-logs/YYYY-MM-DD/` (`LOGS` binding, no container credentials; read with `scripts/read_chatlogs.py --prefix procurement-logs`; its per-step tables will be empty, the records are just question, answer, sources, warnings, ms) and the container also prints a line to its logs. Live since 2026-11-06 (Worker-only deploy, see "Deployed vs main"). Written 2026-11-07, NOT deployed: `calls`, `cost`, `seconds`, `fallback` in those records (the container sends them in the `X-Ask-Trace` header; the Worker logs and removes it), so the per-step tables fill up. Its allowlist and KV namespace are separate from Bill Assistant's. **Heading chain and clause pointers (written 2026-11-06, NOT deployed, not measured):** `Index.chain` gives a clause's parents from its dotted ref (2.3.1 under 2.3 under 2; the Manual borrows Guideline titles) and `render` prints them as `Under: 2.3 ...`; `ask.xrefs` follows "clause/Guideline/Section/paragraph N.N" pointers (keyword required; one hop, max 3 extra clauses of 2,000 chars, noted "Referred to by [..]") for PG/PM 2006 and 2024 only. Both work at query time on the existing index (no rebuild), but the image bakes `procurement/`, so they go live only with a container rebuild, which needs `data/procurement/index.sqlite` (owner's PDFs, not in the sandbox). Measured 2026-11-07 (`python -m procurement.evaluate`, `evals/procurement_eval.jsonl`, deepseek-flash): no harm. See `procurement/README.md` "Eval".

```
browser -> Worker `gov-rag-llm` (src/worker.js + auth.js)
             /login, /login/request, /login/verify, /logout   handled in the Worker (KV: one-time codes + sessions)
             /  and other static paths                        public/ via the ASSETS binding (needs a session)
             /api/*, /healthz                                 -> Durable Object BILLRAG -> Container (Python `billrag serve`, :8000)
                                                               header X-Proxy-Secret added by the Worker, session cookie stripped
Container: restores index.sqlite + vectors.npz from R2 (gov-rag/bills/) on start, then serves.
           Chat, query embeddings and rerank all go to Cloudflare Workers AI (no Anthropic key).
```

### Cloudflare resources (IDs are not secrets)

| What | Value |
|---|---|
| Account id | `e81ea992523c4e14d3d1a89f10732576` (also `CF_ACCOUNT_ID` in `wrangler.toml`) |
| Worker | `gov-rag-llm`; workers.dev subdomain `patprathnayaka`; preview URLs are on (they sit behind the same login) |
| KV namespace | `gov-rag-llm-auth`, id `a44e7e8cc8f0444ba865ea162820193d`, binding `AUTH` |
| Container app | `gov-rag-llm-billragcontainer`, id `a03f4f3c-2efb-4e69-a1b6-c408e6f9b8dc`; class `BillragContainer`, binding `BILLRAG` |
| Container size | `instance_type = "basic"` (1/4 vCPU, 1 GiB, 4 GB disk), `max_instances = 1`, sleeps after 15 min idle (`sleepAfter`) |
| R2 bucket | `gov-rag`: `bills/` (PDFs, `index.sqlite`, `vectors.npz`), `chatlogs/` (conversation logs), `bills/vectors.float32.npz` (old float32 backup, delete when no longer needed) |
| Email | zone `citizenlk.com` (Email Sending enabled 2026-11-07; bounces and DKIM on `cf-bounce.citizenlk.com`, DMARC `p=reject`, no MX on the bare domain, so mail to it is not received); sender `login@citizenlk.com` (was `login@tripletrackr.com`, still enabled for rollback) via the Email Service REST API (`/accounts/<id>/email/sending/send`) - no per-recipient verification in Cloudflare; `ALLOWED_EMAILS` is the only allowlist |

**Worker vars** (`wrangler.toml`): `ALLOWED_EMAILS`, `FROM_ADDRESS`, `CF_ACCOUNT_ID`, `R2_BUCKET`; optional `CHAT_MODEL`
(default `@cf/openai/gpt-oss-120b`), `ORIGIN` (ignored when the container is bound), `TRANSLATE` (`off`|`script`|`ai`, passed to the container as
`BILLRAG_TRANSLATE`; currently `ai`), `TRANSLATE_MODEL` (-> `BILLRAG_TRANSLATE_MODEL`; currently `@cf/meta/llama-3.3-70b-instruct-fp8-fast`).
**Worker secrets** (`npx wrangler secret put NAME`): `OTP_PEPPER`, `CF_EMAIL_TOKEN` (Email Sending), `PROXY_SECRET` (shared with the
container; >= 16 chars), `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (the container needs only read access: use a read-only R2
token if you can), `CLOUDFLARE_API_TOKEN` (Workers AI). `container.js` copies these into the container's env as
`R2_*`, `CLOUDFLARE_API_TOKEN`, `BILLRAG_CF_ACCOUNT_ID`, `BILLRAG_PROXY_SECRET` and the `BILLRAG_CHAT_*` settings for Workers AI.
**API token scopes** used by whoever deploys: Workers Scripts: Edit, Workers KV Storage: Edit, Containers (Cloudchamber): Edit,
Zone: Read. Separate tokens for sending email (Email Sending) and for Workers AI keep a leak small.

### Login behaviour (src/auth.js)
6-digit code, valid 5 min, single use, 5 wrong tries kill it, one request per address per minute, 7-day session cookie
(`HttpOnly; Secure; SameSite=Lax`). The reply is identical for allowed and unknown addresses (no probing). If `PROXY_SECRET`
is missing the Worker answers 500 for `/api/*` instead of forwarding (fails closed). The Python server also refuses to listen
on a non-loopback host without `BILLRAG_PROXY_SECRET`. To add a user: add the address to `ALLOWED_EMAILS` and redeploy.

### One account across services (`src/auth.js`, `src/consent.js`, `test/account.test.mjs`; written 2026-11-08, NOT deployed)
Bill Assistant and Procurement Rules share sign-in: cookie `clk_session` with `Domain=citizenlk.com` (var `COOKIE_DOMAIN`, in both Wrangler configs), and the SAME KV namespace (`gov-rag-llm-auth`; Procurement's old own namespace `66d74e46...` is unused, so its sessions end at deploy). A session, a code and the consent record made on either are valid on both; logout on either ends it everywhere. Each Worker still checks its own `ALLOWED_EMAILS`, so an account that is not on Procurement's list is treated as signed out there; opening sign-up (Phase 3) removes that. **The admin panel is not part of it:** own KV, host-only cookie `session`, no consent step (`createHandler` defaults). The Worker removes the Cookie header before the container, as before.
**Consent:** after the code, a signed-in account with no record at the current `consent.VERSION` gets `/consent` (pages 302, API 403 `consent_required`). Statements: accuracy, not legal advice / not government, data sent to third-party providers outside Sri Lanka (no provider named; the privacy page names them); Procurement Rules adds three (not NPC or the Treasury and circulars expire, no confidential procurement information, not the sole basis for an award, rejection, payment or appeal). Record `consent:<email>` = `{general:{v,at}, procurement:{v,at}}`, no expiry; every box must be ticked (server checks the ids). Change the wording in substance -> bump `VERSION` -> everyone is asked again. Not reviewed by a lawyer. Not done: the chat pages do not yet send a user to `/consent` when an API call returns `consent_required` (a revoked or new version mid-session shows a generic error until the next page load).

### Open sign-up and free accounts (`src/members.js`, `test/signup.test.mjs`; written 2026-11-08, OFF and NOT deployed)
`OPEN_SIGNUP = "on"` (+ `TURNSTILE_SECRET`/`TURNSTILE_SITEKEY`, `OTP_PEPPER`, the `QUOTA` binding: any missing = off, only `ALLOWED_EMAILS` sign in) lets any address sign in: the sign-in page shows the Turnstile widget (`openLoginPage`), a code request needs a verified token (also for staff, so replies never tell the lists apart) and counts 20 per network and 5 per address a day (`SIGNUP_CODES_PER_IP`, `_PER_ADDRESS`; key `si|`, `sa|` + hash of the address, in the QUOTA Durable Object). A **free account** = signed in and not in `ALLOWED_EMAILS` (staff are never counted). Its `/api/ask` and `/api/ask/stream` are counted per service, per day and month (`MEMBER_DAILY` 20, `MEMBER_MONTHLY` 150, `"none"` = no monthly cap; Procurement Rules: 5 and none, own counter keys, same Durable Object by `script_name`), refused with 429 + `reason` day|month + its own sentence, and the body gets our chat id `m:<hash of address>:<page chat id>`. `GET /public/me` gives a free account `{user:"member", free:true, left}`; the page shows "N free questions left today". Given back (`public.settle`, the same code as the public lane): a 5xx, an apology (`unanswered`), and a turn the container marks **free** (`web.free_turn`: greeting, "which bill?", a translation of the previous answer; header `X-Free` on `/api/ask`, `free` event on the stream; the Worker removes the marker). Procurement Rules does not send the marker yet. If sign-up is turned off, free sessions stop working (the session check needs `mayJoin`), staff keep theirs. The public lane's "or sign in" message becomes "create a free account for more" only while sign-up is on. Offline tests only; not checked on Cloudflare: the widget on the real sign-in page, the cross-Worker Durable Object binding from Procurement, a real free account's limits.
### Chat history (`src/history.js`, `test/history.test.mjs`; written 2026-11-09, NOT deployed)
Signed-in accounts (staff and free; not anonymous visitors) keep their chats 30 days. KV (the shared `AUTH` namespace) `hist:<service>:<hash of address>:<chat id>`, value `{turns:[{q,a}]}`, metadata `{title,updated,n}` (so a list is one KV call), `expirationTtl` 30 days renewed on every save (the store enforces the 30 days, no job). Routes in `auth.js` after the consent gate: `GET /history`, `DELETE /history` (all, follows list pages), `GET|PUT|DELETE /history/<id>`; per service, per account, keys hold a hash never the address; write/delete refused when `Origin` is another site. The PAGE saves after each answer (so the Worker does not parse streams); caps: 50 chats, 60 turns, question 1,000 / answer 20,000 characters, 200 KB per chat (a 51st chat gets 409 `full`; adding to an existing one still works). Page (`public/index.html`): History button for any signed-in user, list with Delete, "Delete all history", open = read-only transcript (the container's conversation memory is gone, so follow-ups are not offered: "start a new chat"). Deleting history does not touch the R2 question logs, which carry no account (privacy page says so). Checked in headless Chromium against a stub server (button, save, list, open, new chat, delete all) and offline Node tests; not on Cloudflare (real KV list/metadata/expiry). Procurement Rules page has the same History button (`workers/procurement/public/index.html`): there a saved chat can be opened AND continued, because that page sends its own last two turns with each question (the saved entry is what the person typed, so a continued chat loses the rewritten "Read as" question); checked in headless Chromium against a stub server (save, open, continue sends the earlier turn, same chat updated to 2 questions, delete all).
**Procurement Rules for registered accounts (`OPEN_TO = "registered"` in `workers/procurement/wrangler.toml`, 2026-11-09, NOT deployed):** `auth.mayJoin` also accepts an address that already has a `consent:<email>` record in the shared KV (an account made while Bill Assistant sign-up was open), so no new account can start on this Worker and it needs no Turnstile; the sign-in page says "Sign in with your citizenlk account ... Create a free one" with a link to bills. A registered account is a free account here: 5 a day (`MEMBER_DAILY`), own counter, its extra consent statements first. Removing `OPEN_TO` closes it (staff keep access). Consequences to know: Bill Assistant's sign-up must be on for anyone new to reach it; an account stays registered here even if Bill Assistant's sign-up is later turned off; Procurement Rules does not mark free turns, so every question counts. The Python container does not know about accounts. Offline tests only.
**Not built yet (Phase 3, held by the owner 2026-11-09):** the weekly new-bill email with unsubscribe, the weekly report, soft launch.

### Parallel work on the owner's machine: git worktrees (`scripts/worktree.sh`)
One checkout per branch, so several sessions can work at once. The main checkout (`~/projects/llm_rag`, on `main`) holds the
shared, git-ignored parts; worktrees symlink to them: `.venv` (Python 3.12 like the image), `data/` (index from R2),
`.local.env` (credentials). `node_modules` is per worktree (`npm ci`).
```bash
scripts/worktree.sh setup                          # once (done 2026-11-06): .venv, npm ci, index.sqlite + vectors.npz from R2
scripts/worktree.sh new claude/my-change           # ../llm_rag.worktrees/claude-my-change, new branch from origin/main
scripts/worktree.sh new claude/parser --own-data   # private copy of the index: use for ingest / embed / ingest-si / glossary
scripts/worktree.sh list | rm claude/my-change     # rm deletes the branch only if it is merged into origin/main
cd ../llm_rag.worktrees/claude-my-change && .venv/bin/python -m pytest tests -q && npm test
```
`data/` is shared: rebuilding the index in a worktree without `--own-data` changes it for every worktree. `claude -w <name>` /
EnterWorktree make worktrees under `.claude/worktrees/` instead; `.claude/settings.json` (`worktree.symlinkDirectories`) links
`data` and `.venv` there and `.worktreeinclude` copies `.local.env`; for `node_modules` run `npm ci` in it. Deploy only from a
commit on `main` (see "Deploying"), not from a feature worktree.

### Run and test locally
```bash
python -m pytest tests -q                 # Python, offline (59+ tests); must pass before every commit
npm ci && npm test                        # Worker/login tests (Node 22, no Cloudflare needed)
python -m billrag serve                   # http://127.0.0.1:8000, serves public/index.html; add --mode agent for the tool loop
npx wrangler dev --local                  # Worker with simulated KV/assets; login mail needs CF_EMAIL_TOKEN in .dev.vars (else a code request returns 502)
```
`wrangler.toml` contains a container, so `wrangler dev` and `wrangler deploy` both want a running Docker daemon.

### Deploying (the normal way, from a machine with Docker)
```bash
export CLOUDFLARE_API_TOKEN=...  CLOUDFLARE_ACCOUNT_ID=e81ea992523c4e14d3d1a89f10732576   # or `npx wrangler login`
npm ci
python -m pytest tests -q && npm test      # never deploy red
npx wrangler deploy                        # builds the image, pushes it to registry.cloudflare.com, uploads Worker + assets, rolls the container
```
What `deploy` does: uploads `public/` (the page) and the Worker immediately; the container is a **rolling update** - a new instance
must start (and download ~260 MB of index from R2) before the old one is replaced, usually 2-5 min. During that time the old
code can still answer. Check it:
```bash
npx wrangler containers info a03f4f3c-2efb-4e69-a1b6-c408e6f9b8dc     # "version", image digest, health.instances
curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/containers/applications/<app-id>/rollouts"   # status: progressing | completed
npx wrangler deployments list                                           # Worker versions
```
An instance that was already running keeps serving until the rollout replaces it. **To confirm which code answered, compare the
`build` field in the chat log record with `python -c "from billrag.chatlog import build_id; print(build_id())"`** on the commit you
deployed. Roll back by checking out the previous commit and running `wrangler deploy` again. Vectors: copy
`bills/vectors.float32.npz` back over `bills/vectors.npz` in R2 (and use code that loads float32; both load).

Secrets: `npx wrangler secret put NAME` (stdin) / `npx wrangler secret list`. They apply to the live Worker immediately; the
container reads them from the Worker env when it (re)starts, so restart it (redeploy or let it sleep) after changing one.
Rotating a token: create the new one, `secret put` it, then revoke the old one.

### Deploying from the cloud sandbox (verified 2026-11-05, build `4cfb4b63`)
Only when the owner asked for a deploy in that turn. Deploy a commit that is on `main` (or merge it right after, with a merge
commit, not a squash, so the deployed SHA stays in `main`'s history), and always deploy green.

**Why not just `npx wrangler deploy`:** the sandbox proxy re-signs HTTPS, so `pip` inside `docker build` fails with
`CERTIFICATE_VERIFY_FAILED`. Docker Hub also rate-limits anonymous pulls (HTTP 429) and the Docker daemon is not running by
default. Never disable TLS verification and never bake the proxy CA into the image: bind-mount it for the `pip` step only.

**0. Pre-flight: token and network.** The deploy token needs Workers Scripts: Edit, Workers KV Storage: Edit,
Containers: Edit, Zone: Read. It does NOT need Workers AI (the container uses its own `CLOUDFLARE_API_TOKEN` Worker
secret). A Workers-AI-only token fails late, after the image build, with `Failed to automatically retrieve account IDs` or
`403 No access to the specified service`, so check first:
```bash
cd /home/user/llm_rag && export CLOUDFLARE_ACCOUNT_ID=$CF_ACCOUNT_ID      # wrangler cannot look the account up itself
H="Authorization: Bearer $CLOUDFLARE_API_TOKEN"; B=https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID
for u in containers/applications storage/kv/namespaces workers/subdomain; do
  printf "%-26s" $u; curl -sS -H "$H" "$B/$u" | python3 -c 'import sys,json; d=json.load(sys.stdin); print("ok" if d["success"] else d["errors"])'
done                                                                     # all three must say ok
curl -s -o /dev/null -w "%{http_code}\n" https://registry.cloudflare.com/v2/   # 401 = reachable; anything else = add it to the allowlist
```
**1. Green tests, Docker up, base image cached.**
```bash
pip install -q -r requirements-billrag.txt pytest && python -m pytest tests -q
npm ci && npm test
pgrep dockerd || (nohup dockerd > /tmp/dockerd.log 2>&1 &); sleep 8; docker info | grep "Server Version"
docker pull python:3.12-slim && docker tag python:3.12-slim local-python:3.12
```
**2. Note what is live now (your rollback target) and the build id you are about to ship.**
```bash
npx wrangler containers info a03f4f3c-2efb-4e69-a1b6-c408e6f9b8dc | grep -E '"version"|"image"'   # e.g. version 8, sha256:...
python -c "from billrag.chatlog import build_id; print(build_id())"                               # appears as `build` in chat logs
```
**3. Scratch build context outside the repo** (the repo `Dockerfile` stays untouched):
```bash
S=<scratchpad>/build; rm -rf $S; mkdir -p $S/scripts $S/docker
cp -r billrag $S/ && cp scripts/fetch_index.py scripts/upload_r2.py $S/scripts/ && cp docker/entrypoint.sh $S/docker/
cp requirements-serve.txt /root/.ccr/ca-bundle.crt $S/ && find $S -name __pycache__ -prune -exec rm -rf {} +
sed -e 's|^FROM python:3.12-slim|FROM local-python:3.12|' \
    -e 's|^RUN pip install -r requirements-serve.txt|RUN --mount=type=bind,source=ca-bundle.crt,target=/ca.crt PIP_CERT=/ca.crt pip install -r requirements-serve.txt|' \
    Dockerfile > $S/Dockerfile
diff Dockerfile $S/Dockerfile          # must show exactly those two lines
sed "s|^image = \"./Dockerfile\"|image = \"$S/Dockerfile\"\nimage_build_context = \"$S\"|" wrangler.toml > wrangler.deploy.toml
```
If `Dockerfile` gains new `COPY` lines, copy those files into `$S` too.
**4. Deploy, then clean up** (the build + push takes ~1 min):
```bash
npx wrangler deploy -c wrangler.deploy.toml    # ends with "Current Version ID: <worker version>" and "Modified application ..."
rm -f wrangler.deploy.toml; pkill dockerd; git status --short   # must be clean
```
**5. Watch the container rollout** (took ~2 min; the old instance answers until it completes):
```bash
A=a03f4f3c-2efb-4e69-a1b6-c408e6f9b8dc
for i in $(seq 1 12); do
  s=$(curl -sS -H "$H" "$B/containers/applications/$A/rollouts" | python3 -c 'import sys,json; r=sorted(json.load(sys.stdin)["result"],key=lambda x:x["created_at"])[-1]; print(r["status"], "v"+str(r["target_version"]), r["progress"].get("updated_instances"), "/", r["progress"].get("total_instances"))')
  echo "$(date +%T) $s"; case "$s" in completed*) break;; esac; sleep 30
done
```
**6. Verify and record.** `workers.dev` and `wrangler tail` are NOT reachable from the sandbox, so verify from the chat
logs once someone has asked a question (`pip install boto3` first; it is not in `requirements-billrag.txt`):
```bash
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/read_chatlogs.py --since $(date +%F) --out /tmp/logs.jsonl --show 5
python3 -c "import json,collections; print(collections.Counter(json.loads(l).get('build') for l in open('/tmp/logs.jsonl')))"
```
Then update **Deployed vs main** below (build id, commit, Worker version, container version, previous version) in the same PR
or a follow-up, and tell the owner exactly what is live and what is only merged.
A project hook does the reminding: `.claude/settings.json` runs `.claude/hooks/record_deploy.py` after every Bash call; after a
successful `wrangler deploy` (not `--dry-run`) it hands Claude the commit, build id, Worker version and image and the steps:
wait for the rollout, update that paragraph on a branch fresh from `main`, PR, merge with a merge commit. The hook itself never
pushes or merges. It is silent for every other command.
A second hook, `.claude/hooks/session_start.py` (SessionStart), fetches `origin/main` and tells Claude at the start of every
session: whether `main`'s code differs from the live build recorded below (and which merged commits are therefore not live),
whether the current branch was already merged (restart it from `origin/main`), and which test tools are missing.
**Rollback:** check out the previous deployed commit and repeat steps 1-5; the old image digest stays in
`registry.cloudflare.com`, and `wrangler deployments list` shows earlier Worker versions.

### Reading the chat logs
Every question the live app answers is logged (best-effort, off the request path) to `r2://gov-rag/chatlogs/YYYY-MM-DD/<HHMMSSfff>-<session>-<turn>.json`
(`BILLRAG_LOG_PREFIX`, `BILLRAG_LOG_DIR`; `BILLRAG_CHATLOG=0` turns it off). A record has: `ts`, `build`, `session`, `turn`, `type`
(`ask`/`search`), `question`, `answer`, `error`, `seconds`, `mode`, `model`, `queries` (what retrieval actually searched),
`enough` (did the grader think the evidence sufficed), `evidence` (bill, section, grade 0-3), `candidates_graded`, `warnings`
(unsupported citations, unusable model output), `llm_calls`, `step_seconds`, `events` (resolve/retrieve/evaluate with the grader's
"missing" text) and `usage` (`cpu_s` process CPU seconds during the turn, `cpu_pct` = cpu_s / wall seconds, `rss_mb`,
`rss_delta_mb`, `peak_rss_mb`; also on `search` records) and `calls` (one entry per model / Workers AI call and local search step:
`kind` chat|ai_run|bm25|dense_scan, `step`, `model`, `s` seconds incl. retries, `at` offset in the turn, `in`/`out` tokens,
`reasoning` tokens if the provider reports them, `stop`, `retries` (silent OpenAI-SDK retries), `attempts` (Workers AI `/ai/run`),
`max_tokens`, `effort`, `error`). `read_chatlogs.py` prints a per-step table: median/max seconds, median output tokens, tokens/s.
```bash
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/read_chatlogs.py [--since 2026-11-05] [--out logs.jsonl] [--show 20]
```
It prints totals, median/p90 seconds and lists: errors, "found nothing", "evidence judged insufficient", slowest. Read the raw
`--out` file for detail. Things to look for: answers starting `analysis:` or a run of `!` (model glitch; should now be retried),
a `queries` list that contains guessed bill names (grounding failure), `evidence` from the wrong bill, `enough: false` with
long answers, `build` not matching the commit you think is live, and anything with `error`. Logs contain users' questions: handle
them as private. Records before the first logging deploy do not exist.
### Monitoring CPU and memory
- **Per turn:** the `usage` field above; `read_chatlogs.py` prints median/p90/max of `cpu_s`, `cpu_pct`, `rss_mb`, `peak_rss_mb`.
  Live logs reach `cpu_pct` 117%, so `basic` is not a hard 1/4 vCPU cap at quiet times; high means the turn was CPU-bound, low means it was waiting on the model. CPU is
  process-wide: a `search` running during an `ask` adds to it.
- **Live snapshot:** sign in, then open `/api/stats` (forwarded like all `/api/*`; proxy secret required). It returns process
  `rss_mb`, `peak_rss_mb`, `cpu_s`, `cpu_avg_pct` (since start, after 10 s), `threads`, `loadavg`, `machine` (/proc/meminfo
  total/available), `cgroup` (memory usage/limit/peak, CPU usage/throttling/limit, whichever files exist), `sessions` /
  `max_sessions`, `turns`, `busy` (a question is running), `retriever_loaded`, `build`, `uptime_s`. Opening it wakes a sleeping
  container and resets the 15-minute idle timer, so don't poll it.
- **Where a question's time goes** (`calls`, measured 2026-11-06 from the sandbox with production settings): the answer call
  dominates, and most of it is gpt-oss's **hidden reasoning**, not writing. Streamed answer calls log `first` (seconds to the
  first visible word), `think_chars` (reasoning characters, never shown) and `text_chars`. Workers AI counts reasoning inside
  `out` (no `completion_tokens_details`), so `out` alone over-states the answer: a 1,428-char Sinhala answer was 2,611 `out`
  tokens, but the visible text streamed in ~2.5 s after 14 s of reasoning. Answer step, same questions, `ANSWER_EFFORT` unset
  (model default) vs `low`:

  | answer step | first visible word | answer call | reasoning chars |
  |---|---|---|---|
  | Sinhala direct, default (n=4) | 13.6-26.8 s | 16.7-33.0 s | 2,200-5,700 |
  | Sinhala direct, `low` (n=4) | 0.6-7.0 s | 7.7-12.7 s | 35-190 |
  | English, default (n=3) | 2.8-7.0 s | 5.0-10.2 s | 1,100-1,700 |
  | English, `low` (n=2) | 0.6-0.9 s | 3.0-5.2 s | 75-120 |

  One of 3 Sinhala runs was rejected in each setting (Korean characters, Devanagari danda ।, no citations) and fell back to the
  slow translated path (89-116 s). Answer quality with `low` was measured on the English eval earlier ("pipeline v5": strict
  0.83, 23/24), not on Sinhala. Grading: 2.9-8.2 s (one run's grader reply was unusable: +15 s retry, or "using retrieval order").
  Retrieval: two searches per round, each an embedding (0.2-0.55 s) + reranker (0.5-1.2 s) call, 2-3.5 s in total; BM25 and the
  dense scan < 0.1 s each. Llama translate-in 0.7-1.3 s. No SDK or `/ai/run` retries seen.
- **Streaming** (`POST /api/ask/stream`, server-sent events): `status` {step} at each pipeline step, `reset` + `token` {text}
  while the final answer call writes (English answers and direct Sinhala answers; a translated answer shows steps only, since
  its draft would be English), then `done` {answer} = the checked answer (citations verified, quotes fixed, warnings, sources),
  which replaces the draft, or `error`. Replies that start like leaked reasoning ("analysis:") are not streamed. The page falls
  back to `/api/ask` on a 404 (an older container during a rollout). The Worker passes the stream through unchanged.
- **Answer speed choice on the page** ("Answer: Thorough | Fast", remembered per browser): requests carry `answer`, and only
  `web.ANSWER_MODES` names are accepted. `fast` = reasoning effort `low` for the answer call only (grading, translation unchanged),
  and only when the server sends effort at all (`BILLRAG_GRADE_EFFORT` non-empty); `thorough` / missing = the server default
  (`BILLRAG_ANSWER_EFFORT`, unset = model default). Logged as `answer_mode` and `answer_effort`. First live check (Sinhala
  cricket question, fast): answer call 5.1 s, first word 0.8 s, 35 reasoning chars, direct path, no warnings; 20.6 s in total.
  Sinhala answer quality with `fast` is not measured beyond a handful of runs: compare `direct_fallback` / `warnings` by
  `answer_mode` in the logs.
- **Over time:** Cloudflare dashboard -> Workers & Pages -> Containers -> `gov-rag-llm-billragcontainer` has CPU/memory/disk graphs
  per instance (not reachable from the sandbox).

Worker-side errors (`upstream_failed`, with path, ms, error) go to Workers Logs (dashboard: Workers & Pages -> gov-rag-llm -> Logs;
`[observability] enabled = true`), and container logs are enabled too.

### Admin panel (`src/admin.js`, `src/admin-stats.js`, `test/admin.test.mjs`; live since 2026-11-07, see "Deployed vs main")
`https://admin.citizenlk.com`, Worker `citizenlk-admin` (`workers/admin/`; deploy `npx wrangler deploy -c workers/admin/wrangler.toml`, which does not touch Bill Assistant or its container), modelled on LankaWatch's control panel (`sri-lanka-ds-boundaries/docs/control-panel.md`): home is a **To do** list, most urgent first (People affected, Waiting for you, Watching), each row with its own button and gone when the cause is; then tiles; then `/admin/source/<id>` per source (`bills`, `procurement`, `money`, `public`, `containers`, `index`). `/admin/questions` and `/admin/q/<log key>` read the logs; `/admin/audit` lists buttons pressed. Server-rendered, no build step; pages update when loaded (no auto-refresh).
- **Who:** its own sign-in and sessions (own KV `AUTH`, own `OTP_PEPPER`); `ALLOWED_EMAILS` in `workers/admin/wrangler.toml` is the list. Actions are POST with an `Origin` check.
- **What it shares with Bill Assistant:** `BILLS_KV` (that Worker's namespace, which also holds user sessions; the panel uses only `admin:*`), R2 `gov-rag`, and read-only calls to the `QUOTA` and container Durable Objects by `script_name`. Bill Assistant's Worker has no R2 binding and no panel code; it reports `admin:settings` (public switch, limits, whether secrets exist, never their values) from its hourly cron, because the panel cannot see its vars. `ALERT_EMAIL`, `BALANCE_ALERT_USD`, `FROM_ADDRESS`, `CF_ACCOUNT_ID` are in both configs for "Check now"; a test keeps them equal.
- **Never wakes a container** and spends no model call: `getState()` reads stored state, the credit is the last result of `checkBalance` (KV `admin:balance`), the public count is `QuotaCounter.peek`.
- **Numbers:** rollups at R2 `admin-rollups/<service>/<day>.json` (`LOGS` binding) remember the last log key counted and read only newer ones, within a budget of R2 calls per view; the panel Worker's hourly cron (`23 * * * *`) moves them too. A day over for 2 days is closed. The newest minute is left for later, because a log is written just after its key's time and `startAfter` would skip it for good. Rollups hold counts and keys, never words. Deleting a rollup recounts it. Days are UTC (the log folders), times are Sri Lanka time.
- **Log records are untrusted** (anyone can type a question): everything is escaped, and the CSP allows only that response's nonce. Keep both; a test posts a `<script>` question.
- **Pause public questions:** KV `admin:public-paused`, read in `auth.js` for signed-out requests (cached 60 s, so it can take a minute to reach every location). Treats visitors as `PUBLIC_CHAT = "off"`; members are unaffected.
- **Checked** (offline only): Node tests; a real Chromium run at 1280 and 400 px against copies of the real logs (no CSP violations, no sideways scroll, pause and resume pressed); its counts matched an independent Python count of the same records. **Not checked on Cloudflare:** the cross-Worker Durable Object calls, real R2. Not built from LankaWatch's panel: Web Push, phone install (PWA), a People page. The daily new-bills check and the Build/Publish buttons are written but not deployed (see "Scheduled refresh").

### Runbook (symptom -> likely cause -> fix)
| Symptom | Cause / fix |
|---|---|
| "Failed to fetch" on a question | The container was asleep (cold start ~1-3 min incl. the 260 MB index download). The page now shows a "waking up" status and retries; the Worker returns a JSON 502 if the container does not answer. Check `upstream_failed` in Workers Logs and the rollout status. |
| Page loads but answers look like the old behaviour | A rollout is still replacing the old instance; compare `build` in the logs (above). |
| Login code never arrives | `CF_EMAIL_TOKEN` secret missing/expired; sender domain not onboarded under Email -> Email Sending; address not in `ALLOWED_EMAILS`; 60 s resend cooldown; check the send response in Workers Logs. |
| `/api/*` returns 403 | The request did not carry `X-Proxy-Secret`: `PROXY_SECRET` differs between Worker and container (re-`put` it, restart the container). |
| `/api/*` returns 500 "Server is not configured" | `PROXY_SECRET` is not set on the Worker. |
| Answer is only "analysis: ..." or "!!!!" | Model glitch. `Pipeline._llm` strips a "final" section, retries once, else returns a fallback + warning. If it recurs, look at the log record. |
| "I could not find anything" for a normal question | Check `queries`/`evidence` in the log: wrong expansion of an abbreviation, a bill not in the index, or a retrieval miss. Greetings and unknown abbreviations are handled before retrieval (`resolve.py`). |
| `VectorsError` at startup | `vectors.npz` does not match `index.sqlite` (re-ingested without re-embedding). Re-run `embed`, `upload_r2.py --include-index`. |
| workerd: "provided value is not of type function or ExportedHandler" | `src/worker.js` may only export the default handler and the Durable Object class; keep helpers in `auth.js`/`container.js`. |
| `docker build` 429 or pip SSL errors in the sandbox | See "Deploying from the cloud sandbox". |
| `wrangler deploy`: "Failed to automatically retrieve account IDs" / "403 No access to the specified service" | Token lacks deploy scopes (e.g. the Workers-AI-only token) or `CLOUDFLARE_ACCOUNT_ID` is unset: run the pre-flight check in "Deploying from the cloud sandbox". |
| `wrangler deploy`: "The Docker CLI is needed ... could not be launched" | The Docker daemon is not running: `nohup dockerd > /tmp/dockerd.log 2>&1 &`. |
| Out of memory in the container | `lite` (256 MiB) fit in memory but was too slow; `basic` is current. Vectors are float16 (66 MB); sessions are capped at 50 (`web.MAX_SESSIONS`). |

### Cost
Container memory dominates: about $0.009/h while awake on `basic` (sleeps after 15 idle minutes), plus Workers AI usage and the
Workers Paid plan. `lite` would be ~$0.002/h but was too slow in use (1/16 vCPU: ~6-8 s per search).

## Question handling added on top of the pipeline

- **Grounding (`resolve.py`):** greetings/thanks get a help message with no search or model call. An ALL-CAPS 3-8 letter token that
  occurs nowhere in the corpus is treated as an abbreviation: it is accepted only if it matches the initials of a real bill title
  (with/without bracketed words and a trailing "A", transpositions allowed, year-aware: "PTSA 2026" -> *Protection of the State from
  Terrorism*, 070/2026 + 078/2026) and the answer says how it was read; otherwise the user is asked ("did you mean ...") and no
  model is called. Known weakness: without a year the "did you mean" list can contain unrelated titles. When nothing relevant is
  found the answer ends with "Closest bills by title".
- **Model-output guards (`Pipeline._llm`):** reasoning-only replies (`analysis: ...`) and punctuation runs are detected, retried
  once (grader JSON calls have their own retry), then replaced by a short fallback with a warning.
- **Brevity:** the answer prompt asks for 1-2 sentences + at most 3 one-line cited bullets, ~120 words (~180 for "summarise").
  Measured on the 24-question eval: average 117 -> 90 words, strict 0.79 -> 0.83, judged correct 0.92 (unchanged), time not
  faster (18.8 -> 20.7 s, noise). The model still overshoots on broad questions (e.g. 177 words for an "explain" question).
- **Separate answer model:** `BILLRAG_ANSWER_MODEL` (same provider/endpoint) swaps only the final answer step; grading, condense
  and judging keep `BILLRAG_CHAT_MODEL`. Unset by default.

### Answer model comparison (24 questions, judge/grader `gpt-oss-120b`, rows in `evals/answer_model_comparison/`)

| answer model | strict | judged correct | avg s | notes |
|---|---|---|---|---|
| `gpt-oss-120b` (current) | 0.79 | 0.92 | 18.8 | clean |
| `llama-4-scout-17b-16e` | 0.79 | 0.92 | 19.3 | 3 answers cited clauses not in their evidence |
| `llama-3.3-70b-instruct-fp8-fast` | 0.75 | 0.88 | 17.0 | |
| `mistral-small-3.1-24b` | 0.75 | 0.83 | 15.5 | 1 unsupported citation |

No non-reasoning model beat `gpt-oss-120b` on quality, and none was more than ~3 s faster, so we stayed. Latency is dominated by the
hosted model, not the answer length.

### Concurrency (`billrag/gate.py`, `web.App.ask`)
Questions overlap on one instance: `BILLRAG_MAX_CONCURRENT` (default 8) run at once, `BILLRAG_MAX_QUEUE` (20) wait in arrival order,
each at most `BILLRAG_QUEUE_WAIT` s (90); beyond that HTTP 503 + `Retry-After` (stream: `error` event with `busy`). Streamed replies get
`queue` {position} events while waiting (the page shows "Waiting in line"). One conversation runs one question at a time (per-session
lock). `calltrace` is a ContextVar, so each turn logs only its own calls; threads started inside a turn must use
`contextvars.copy_context()`. Log fields `queue_wait_s`, `concurrent`; `usage.cpu_s` is process-wide, so it includes other running
turns when `concurrent` > 1. `/api/stats` has `running`, `queued`, `max_concurrent`, `max_queue`, `rejected`. Each thread has its own SQLite connection (`Store.db`, `Index.db`).
`procurement/serve.py` uses the same gate: `PROCUREMENT_MAX_CONCURRENT` (2), `PROCUREMENT_MAX_QUEUE` (6), `PROCUREMENT_QUEUE_WAIT` (60 s).

### Load test (2026-11-07, `scripts/load_test.py`, rows in `evals/load_test_2026-11-07.jsonl`)
Run on the real images at the plan's limits under Docker (`--cpus 0.25 --memory 1g`, Procurement `0.0625` / `256m`), production index, real embeddings and reranker. **NOT on the Cloudflare container:** that
needs a signed-in cookie (`--url https://bills.citizenlk.com --cookie ...`; each question is real money and appears in the logs). "Stub" = a stand-in chat model with realistic delays (grading ~2.5 s, answer ~3 s), which
measures the server and saves credit, but has no provider rate limits. A person asks, waits for the whole answer and asks again (2 rounds; 1 with real DeepSeek).

**Found:** one SQLite connection shared by threads, live since the concurrency deploy, failed questions as soon as they overlapped: at 4 people 2 of 8, at 8 people 9 of 16 (`InterfaceError`, `IndexError`). Now one connection per thread, with a regression test.
Also, with the host's many cores, OpenBLAS threads used up the CPU share: a vector scan took 3.2 s at 1/4 vCPU; one BLAS thread and float32 in memory (+72 MB, same file) make it 0.01 s (median, under load).

| Bill Assistant, 1/4 vCPU | people at once | answered | refused | median s | p90 s | peak RSS MB |
|---|---|---|---|---|---|---|
| old code, stub | 8 | 7/16 | 0 (9 failed) | 60.8 | 71.7 | 309 |
| fixed, 4 slots, stub | 8 | 16/16 | 0 | 36.7 | 54.5 | 314 |
| fixed, 4 slots, stub | 14 | 28/28 | 0 | 63.7 | 77.8 | 369 |
| fixed, 4 slots, stub | 20 | 28/40 | 12 | 51.4 | 61.5 | 431 |
| fixed, 8 slots, stub | 8 | 16/16 | 0 | 16.7 | 22.1 | 294 |
| fixed, 8 slots, stub | 14 | 28/28 | 0 | 21.6 | 31.9 | 376 |
| fixed, 8 slots, stub | 20 | 40/40 | 0 | 33.9 | 54.6 | 479 |
| **final, real DeepSeek** | 1 / 4 / 8 | 13/13 | 0 | 17.2 / 25.2 / 18.4 | - / 27.0 / 29.9 | 285 |

More slots did not slow questions down, because most of a question is waiting on the model, so the defaults went from 4/10 to 8/20 (public lane 2/6; the old 1/3 served 4 of 8). **Reading:** on the worst-case CPU it handles about 8-14 people asking at the same instant at about 20-30 s a question, and beyond 28 it refuses cleanly.
People read between questions, so that is more people than it sounds. Not measured: real DeepSeek above 8 people, provider rate limits, sustained load (RSS rose with people to ~520 MB of 1 GiB; sessions are capped at 50, but a slow leak would not show), public 2/6 as such, cold starts.

| Procurement Rules, 1/16 vCPU, 2 slots + queue 6 | people | answered | refused | median s | p90 s | peak MB |
|---|---|---|---|---|---|---|
| stub | 4 / 8 / 12 | 8/8, 16/16, 16/24 | 0, 0, 8 | 5.6 / 10.6 / 9.9 | 8.0 / 14.7 / 12.6 | 77 / 93 / 109 |
| real DeepSeek | 1 / 2 / 4 | 7/7 | 0 | 9.8 / 5.7 / 5.3 | - / 5.8 / 8.2 | 62 |

No reranker in these runs (it needs the Worker), which adds a call per question.

### Backup model when DeepSeek fails (`llm.FallbackLLM`, written 2026-11-07)
A failed DeepSeek call (no credit, outage, 60 s of silence, after 1 retry) is repeated on Workers AI `@cf/openai/gpt-oss-120b` in the same question. After a failure DeepSeek is left alone for 60 s, or 5 minutes for 401/402/403. `src/chatconfig.js` passes `BILLRAG_FALLBACK_*`; Procurement has no Cloudflare token, so its container calls the Worker's `/internal/chat` (`PROCUREMENT_FALLBACK_URL`, AI binding).
Log field `fallback` {calls, model, reason} and a `chat_fallback` line in the container log; `read_chatlogs.py` counts them; `cost.usd` leaves the backup's calls out. The backup scored lower (strict 0.89 vs 0.93, "Priorities, DeepSeek"), which is why the balance alert exists.
**Simulated failure, real everything else (2026-11-07):** a stub answering 402 as DeepSeek. Bill Assistant: an English and a direct-Sinhala question answered on real Workers AI in 28 s and 23 s, no warnings. Procurement: 2 questions in ~5 s through the real `handler.js` in Node calling Workers AI over REST (the binding itself is not exercised). Both models down: the visitor sees a plain message, a signed-in user the error. Not done on the live site.

### Failed answers and the visitor's allowance (2026-11-07)
They were charged: a streamed reply is HTTP 200 before the answer exists, so `status >= 500` never fired (live case: 2026-11-06 20:38 UTC, DeepSeek 402, `lane: public`; the new test fails on the old code). `public.watchStream` now reads the stream as it passes and gives the question back on an `error` event (provider failure or busy), an `unanswered` event (the reply is only an apology; `X-Unanswered` on `/api/ask`) or a stream ending without `done`. A closed tab does not get it back, because the answer is still produced. Visitors no longer see provider errors (the same visitor saw "Error code: 402 - Insufficient Balance").

### DeepSeek credit alert (`src/balance.js`, 2026-11-07)
Cron `17 * * * *` on `gov-rag-llm`: email to `ALERT_EMAIL` when the credit is below `BALANCE_ALERT_USD` ($5, about 4 days at the public ceiling) or `is_available` is false, at most one a day; a failing check mails after 3 in a row. State in KV (`alert:*`). Offline tests only. The credit was under $5 when written, so the first run after a deploy will send a mail.

### Refreshing the index (`scripts/refresh_bills.py`, 2026-11-07)
Works in `data/refresh` on a copy of the live index; `--publish` is what changes production. Only new documents are parsed (`--only-new`, `ingest-si --keys`), so existing chunk ids and vectors stay valid. Publishing refuses if a document, chunk or summary was lost, a new document cannot be found by its own title, or MRR on the 72 recent questions is below 0.80. Real runs without `--publish`: found bill 087/2026 (Prevention of Organised Crimes, 20 Oct 2026; the presented version, its Gazette text 075/2026 was already indexed), 105 chunks, 111 embedded, MRR 0.878, checks passed. That build was published 2026-11-07 by running `upload_r2.py --include-index` by hand, because `--publish` would have replaced the full `bills.csv` in R2 with the scraped years only (547 rows instead of 5,142). Now a scrape with `--since-year` writes `bills.since-YYYY.csv`, which is not uploaded. **`--publish` itself has still not been run end to end, and nothing is scheduled.** Over Wi-Fi the publish took ~19 min (one connection lost and retried).
Summaries used to fail: with thinking on, deepseek-flash spent the whole limit on reasoning and returned nothing (6 of 7 documents); `summarize` now uses `BILLRAG_GRADE_EFFORT`, and the rerun summarised all 7 (087/2026 and 6 Acts that had none).

### Scheduled refresh: daily check + buttons in the admin panel (`src/refresh.js`, `.github/workflows/refresh-index.yml`, `scripts/refresh_r2.py`; written 2026-11-08, NOT deployed, nothing run on Cloudflare or GitHub)
A Worker cannot run `refresh_bills.py`, so the work is split. **Daily cron** (`40 2 * * *` = 08:10 Sri Lanka, on `citizenlk-admin`, the hourly one does not run it): replays the documents.gov.lk listing (same server action as `scrape_bills.py`; var `DOCS_ACTION_ID` when the site redeploys and the check says "action id may be stale") and compares it with R2 `bills/indexed-keys.json` (the live index's keys, written by `upload_r2.py --include-index` after each publish) plus `refresh/skipped.json` (listed but not indexable at the last build) plus what you pressed "Ignore these" on. What is left is **pending**: a "Waiting for you" row on the To do list (source "Bill index") with **Build the index** and **Ignore these**. **Build** dispatches the GitHub workflow (`mode=build`): `refresh_bills.py --report` WITHOUT `--publish`, the built pair staged in R2 `refresh/staging/`, a report in `refresh/status.json`; the row becomes "built and checked" with **Publish** (workflow `mode=publish`: `upload_r2.py --include-index`, its checks, backup and swap) and **Discard** (the Worker deletes the staged files itself). The job reports through R2, so there is no inbound endpoint. The check also emails `NEW_BILLS_EMAIL` (var in `workers/admin/wrangler.toml`) once per new document (`state.notified`; a failed send is retried at the next check and shown as a row). A failed build, a job that never started (30 min) or one silent for 3 h, a failed listing fetch, a missing manifest and a check older than 36 h each get a row. After a publish the stored pending list is out of date, so the row asks for **Check now** instead of offering Build. One run at a time (`concurrency`); the Worker refuses a second.
**To turn on (owner):** (1) `python scripts/upload_r2.py --bucket gov-rag --manifest-only --data <dir with the live index.sqlite>` once, or the check says "no list of the documents in the live index"; (2) GitHub repo secrets `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (write to `gov-rag`), `R2_BUCKET`, `CLOUDFLARE_API_TOKEN` (Workers AI), `DEEPSEEK_API_KEY`; (3) `npx wrangler secret put GITHUB_DISPATCH_TOKEN -c workers/admin/wrangler.toml` (fine-grained token, this repo only, Actions: read and write); (4) the workflow file must be on `main` (GitHub only dispatches the default branch); (5) deploy the admin Worker. **Not checked:** that documents.gov.lk answers a Cloudflare Worker or a GitHub runner (the scraper has only run from the sandbox and the owner's machine), the GitHub dispatch, the workflow end to end (an hour-long job with real model and embedding calls), R2 staging of a 260 MB pair from a runner. Node and Python tests use a fake site, GitHub and bucket. **Known gaps:** a document listed before its English PDF exists is marked skipped by the next build and not looked at again unless the manifest changes; a local `--publish` between a build and Publish is overwritten by the staged pair (the build copied the live index when it started).

### Public lane (anonymous visitors; `src/public.js`, `quota.js`, `quota-do.js`; written 2026-11-06, ON since 16:52 UTC)
`PUBLIC_CHAT = "on"` in `wrangler.toml` since 2026-11-06 16:52 UTC (it was "off" before; "off" restores login-only). It only works when it is `"on"` AND `TURNSTILE_SECRET` (Worker secret, set 2026-11-06), the
`TURNSTILE_SITEKEY` var (public), `OTP_PEPPER`, `PROXY_SECRET`, the `QUOTA` Durable Object and the container binding all exist (`publicOn`; any
piece missing = login only, fails closed). Turnstile widget "citizenlk Bill Assistant" (managed mode, hostname `bills.citizenlk.com`) lives in the
Cloudflare account. Flow: page asks `GET /public/me` -> not signed in and no cookie -> Turnstile -> `POST /public/start {token}` -> signed `anon`
cookie (HMAC, 30 days; only an id) -> `/api/ask`, `/api/ask/stream`, `/healthz` allowed, everything else under `/api` stays members only.
- **Quota is counted on the server, never trusted from the browser or an IP alone** (many Sri Lankan users share mobile IPs): per question three
  counters in one Durable Object must all have room, reset at midnight Sri Lanka time (UTC+5:30): cookie `PUBLIC_DAILY_PER_COOKIE` (10), IP
  `PUBLIC_DAILY_PER_IP` (300, a flood backstop only), whole lane `PUBLIC_DAILY_GLOBAL` (500, the cost ceiling). New cookies: `PUBLIC_COOKIES_PER_IP`
  (50 a day per IP, failed tokens count). A refused question does not use the visitor's allowance; a container 5xx gives the count back.
  Re-solving Turnstile with a valid cookie keeps the same cookie (no reset). Reply 429 + `Retry-After` + `reason` (cookie|ip|global|issue).
- **The Worker rebuilds the request body** (session = `anon:<cookie id>`, question trimmed, <= 600 chars, `answer` fast|thorough) and marks it
  `X-Lane: public` (a client-sent X-Lane is dropped). Without that rewrite every visitor would share the session "default".
- **Container:** the `public` gate lane gets `BILLRAG_PUBLIC_SLOTS` (2) of the slots and its own `BILLRAG_PUBLIC_QUEUE` (6) queue, so public
  traffic cannot take members' capacity; `anon:` chats are evicted first when sessions exceed `MAX_SESSIONS`. Log field `lane`.
- **Page:** `public/index.html` shows "N free questions left today" (header `X-Quota-Left`), hides "Find clauses only" and turns "Sign out" into
  "Sign in". All page text is English (DESIGN.md principle 7).
- **Cost:** about $0.0005-0.003 per answer (`cost` in the log), so the global cap bounds the public spend to a few dollars a day.
- **Tested** offline (test/public.test.mjs with an in-memory counter, billrag gate/lane tests, a Playwright run of the page with a stubbed
  Worker and stubbed Turnstile). Verified live only by the owner's report that it works on their phone (2026-11-06; the real Turnstile
  widget, siteverify call and Durable Object counters were exercised that way). NOT checked: chat-log records with `lane: public`, load, shared mobile
  IPs, speaker review of the Sinhala/Tamil texts. To turn on (done 2026-11-06): deploy, set `PUBLIC_CHAT = "on"`, redeploy, try it from a phone, read `lane: public` logs.
  Abuse not covered: an attacker with many IPs and solved challenges can use up the global cap for a day (availability, not cost).

### Coverage notice (what the index holds; written 2026-11-06)
`Store.coverage()` -> `{bills, first, last, recent[]}`: `bills` counts each bill once (a Gazette text and its presented Bill share `bill_meta.family`;
without `bill_meta` versions are grouped by title and year), `first`/`last` are the oldest and newest document dates, `recent` lists up to 8
bills newest first under their plain title (the " - Bill" / " - GS" suffix is stripped). Measured on the production index: 995 bills (1,513
documents), 2003-07-07 to 2026-10-21. `GET /healthz` carries it as `coverage` (computed once per process; a failure never fails the health check),
so the numbers follow the index in R2 automatically. The page (`public/index.html`) shows one line in the footer note ("Covers N bills from
documents.gov.lk, 2003 to 21 Oct 2026.") and a "Latest bills" list on the start screen; clicking an item asks "Summarise the <title> bill <NNN/YYYY>".
The last answer is kept in `localStorage` (`billrag-coverage`) so both show at once while a sleeping server starts. The dates are the bills' own
dates, not when we indexed them, and the index is a snapshot: nothing refreshes it automatically (scrape, ingest, embed, upload to R2 by hand).
Anonymous visitors get it too (`/healthz` is open to a verified visitor). Not shown on the homepage (static, no container) or in "found nothing"
answers (the greeting also has no evidence, so it would be wrong there). Offline tests + a Playwright run with a stubbed server; not deployed.

### No "GS" jargon (2026-11-06)
A live user asked "what is GS": Sources lines and answers showed our internal kind codes. `pipeline.version_tag` / `KIND_LABEL` now show
"Gazette text" (kind GS) and "Bill" (presented version) and drop the " - GS" / " - Bill" title suffix; kinds without a plain name (Acts,
other documents) show only the date. The same words go to the grader and the answering model (and `shown_text` rewrites the stored
bill-level chunk header "Bill 084/2026 - Title - GS (GS, date)" on the way out; stored text and vectors are unchanged), and the answer prompt says
never to write "GS". `Store`, `bills.kind`, the agent (`--mode agent`) and `/api/search` still use the codes. `test/page.test.mjs` compiles the
page's inline script (a stray `//` comment once broke the whole page) and checks that no page text names the languages.

### Small-memory hosting facts
Index: 42,696 chunks x 768 dims; `index.sqlite` 201 MB, `vectors.npz` 61 MB as float16 (114 MB float32). Server RSS ~124 MB after a
dense search; ~100 MB right after warm-up; one shared `Retriever` serves all conversations. `shrink_vectors.py` converts a float32 file;
`embed_all(..., dtype="float16")` writes it directly. Top-20 overlap with float32 was 99.8% on 400 test queries (top-1 identical).

## Deployed vs main (keep this honest)
"Merged" is not "live". **Open sign-up ON, live 2026-11-09 ~01:35 UTC: commit `7589beb` (merge of PR #93, on `main`; deployed from `main`), Python build `f42fcb93` (unchanged). Bill Assistant only: Worker `gov-rag-llm` version `294ad7f9`, container app version 33 ("no changes", no rollout). `OPEN_SIGNUP = "on"` and `PUBLIC_DAILY_PER_COOKIE = "3"` (was 10); anyone can now create a free account on Bill Assistant (20 a day, 150 a month), and registered accounts can use Procurement Rules (5 a day, `OPEN_TO`). Checked from outside: the sign-in page carries the Turnstile widget and "create a free account" text, and a code request with a bad token gets 403. NOT confirmed: a real sign-up end to end (real widget token, the code email, consent, a counted question on real counters), and the limits on real Durable Object counters. Roll back by setting `OPEN_SIGNUP = "off"` and `PUBLIC_DAILY_PER_COOKIE = "10"` and redeploying (free sessions stop working at once; staff are unaffected). Previous: Worker `00c9d070`, commit `786c939`.** **Consent wording names citizenlk.com, live 2026-11-09 ~01:24 UTC: commit `bcccbf0` (merge of PR #91, on `main`; deployed from `main`), Python build `f42fcb93` (unchanged). Bill Assistant: Worker `gov-rag-llm` version `00c9d070`, container app version 33 (image `sha256:773c99dd...`, rebuilt from the same code, rollout completed). Procurement Rules: Worker `citizenlk-procurement` version `2a83ac5a`, container app version 8 (image `sha256:00bdd033...`, rollout completed). Only `src/consent.js` changed (Procurement statement now starts "citizenlk.com is an independent service, not the National Procurement Commission or the Treasury, and its answers are not official guidance"; `VERSION` 2026-11-09, so every account is asked again). Checked from outside: login pages 200 on both. NOT confirmed: the served consent page was not seen (needs a signed-in session). Previous and rollback target: commit `47a7137`, Bill Assistant Worker `245ad82c` / container v32, Procurement Worker `dc3d70a0` / container v7.** **Phase 3 (shared account, consent, history, free accounts, Procurement for registered accounts), live 2026-11-09 ~01:10 UTC: commit `5eb0dfa` (merge of PR #89, on `main`; deployed from `main`), Python build `f42fcb93`. Bill Assistant: Worker `gov-rag-llm` version `245ad82c`, container app version 32 (image `sha256:65066cb5...`, rollout completed 01:08 UTC, 1/1 instances). Procurement Rules: Worker `citizenlk-procurement` version `dc3d70a0`, container app version 7 (image `sha256:1554d512...`, rollout completed 01:10 UTC, 1/1 instances). The admin Worker and homepage were not redeployed (the admin Worker still runs the older `auth.js`, which behaves as before). Contains: one domain-wide session cookie `clk_session` and one KV for both services (everyone signs in once more; Procurement's old KV is unused), the consent step, 30-day chat history on both pages, open sign-up and per-account limits (code present, `OPEN_SIGNUP = "off"`, so NOT active), free turns marker from the container, and Procurement Rules open to registered accounts (`OPEN_TO = "registered"`: nobody is registered yet, because accounts are only made while Bill Assistant sign-up is on). Checked from outside: login and privacy pages 200 on both, `/api/ask` 401 without a session, `/public/me` answers on both, Bill Assistant's privacy page carries the history text. NOT confirmed: no signed-in session has been seen on the new code (consent page, `clk_session` Domain cookie across both hosts, history save/list/delete on real KV, a chat log with `build: f42fcb93`), and the Turnstile widget on the sign-in page and the cross-Worker counters are only tested offline. To turn sign-up on: set `OPEN_SIGNUP = "on"` and `PUBLIC_DAILY_PER_COOKIE = "3"` in `wrangler.toml` and redeploy Bill Assistant. Previous and rollback target: commit `82ec594`; Bill Assistant Worker `bd788ffd`, container v31 (image `sha256:6a8a3930...`); Procurement Rules Worker `dc3d70a0`'s predecessor (version id not noted), container v6 (image `sha256:7fb78ede...`).** **Admin Worker with the daily new-bills check and email, live since 2026-11-07 ~18:30 UTC: commit `4726d4d` (head of PR #87, `claude/new-bills-email`; NOT merged into `main`, deployed from the branch because the owner said "deploy" while the merge was held; PR #85, the refresh code and workflow, is on `main`). Only `citizenlk-admin` changed: Worker version `00640cfe`, crons `23 * * * *` and `40 2 * * *`, var `NEW_BILLS_EMAIL`; Bill Assistant, Procurement Rules and their containers were not touched. R2 `bills/indexed-keys.json` written the same day from the live index (1,613 documents, 99 Acts). Checked from outside: `admin.citizenlk.com` and `/admin/source/index` redirect signed out to `/login`. NOT confirmed: the first daily check (02:40 UTC) or "Check now" has not run, so whether documents.gov.lk answers a Cloudflare Worker, and the email arriving, are unseen. Expect a large first email (documents with no English PDF count as new until a build skips them). Not set up on purpose: GitHub secrets and `GITHUB_DISPATCH_TOKEN`, so Build and Publish say "Not set up" (see "Scheduled refresh"). Previous and rollback target: Worker `59216649` (`npx wrangler rollback -c workers/admin/wrangler.toml`, or redeploy commit `69b8d88`'s predecessor on `main`).** **Footer says answers can be incomplete or wrong, live 2026-11-07 ~14:30 UTC: commit `da8a9a6` (merge of PR #82, on `main`; deployed from `main`), Python build `1720fe64` (unchanged). Bill Assistant: Worker `gov-rag-llm` version `bd788ffd`, container app version 31 (image `sha256:6a8a3930...`, "no changes", no rollout). Homepage: Worker `citizenlk-home` version `83811b74`. New: the footer line "Answers cite the official text but can be incomplete or wrong: check the document" on the chat page and the homepage (DESIGN.md principle 4). NOT confirmed: the sandbox proxy blocks `bills.citizenlk.com` and `citizenlk.com`, so the served pages were not fetched; a Chromium check of the files at 400 px wide before the deploy showed a two-line footer and no sideways scroll. Previous and rollback target: commit `73c62d2` (main before this change), Worker `ce6a401f` (Bill Assistant); the previous homepage Worker version was not noted. Roll back by checking out `73c62d2` and redeploying each Worker.** **Gist questions (overview, title index, "which bill?"), live since 2026-11-07 ~13:10 UTC: commit `f94d55a` (merge of PR #80, on `main`; deployed from `main`), Python build `1720fe64`, Worker `gov-rag-llm` version `ce6a401f`, container app version 31 (image `sha256:6a8a3930...`, rollout completed 13:11 UTC, 1/1 instances). Index in R2 replaced 13:11 UTC: same 47,080 chunks plus the `title_index` table (1,082 rows), the R2 copy compared byte for byte with the local build; previous pair at `backups/bills/20261107T131147Z/`; an instance restores it when it next starts, so an instance that was awake keeps the old index (no `title_index`, so the fallback `titled_bills` runs) until it sleeps. NOT confirmed: no question has reached the new container, so no chat log with `build: 1720fe64`, no `overview_added` event and no "Which did you mean?" reply has been seen. Previous and rollback target: commit `0e46a6d` (main before this change), build `f6c858c3`, Worker `cd1686a0`, container v30 (image `sha256:36990acb...`); the index change is harmless to old code (it ignores the table).** **Login mail from `login@citizenlk.com`, live since 2026-11-07 ~08:45 UTC: commit `8fd339d` (merge of PR #78, on `main`; deployed from `main`), Python build `f6c858c3` (unchanged). Only `FROM_ADDRESS` changed. Workers: `gov-rag-llm` `cd1686a0`, `citizenlk-procurement` `dafb3a50`, `citizenlk-admin` `59216649`; containers unchanged ("no changes": Bill Assistant v30, Procurement v6). Checked: a test send from `login@citizenlk.com` with the local email token was accepted and queued; `/login` 200 on all three. NOT confirmed: a real sign-in code arriving through the live `CF_EMAIL_TOKEN` secret (it cannot be read back and may differ from the local token), and the credit alert from the new address. Rollback: `FROM_ADDRESS = "login@tripletrackr.com"` (still enabled for sending) and redeploy; previous Workers `a2203b6e` (Bill Assistant), `a77112bc` (Procurement), `051822b8` (admin).** **Wordmark links to the homepage, live since 2026-11-07: commit `f8a007b` (merge of PR #76, on `main`; deployed from `main`), Python build `f6c858c3` (unchanged). Bill Assistant: Worker `gov-rag-llm` version `a2203b6e`, container app version 30 (image `sha256:36990acb...`, "no changes", no rollout). Procurement Rules: Worker `citizenlk-procurement` version `a77112bc`, container app version 6 (image `sha256:7fb78ede...`, "no changes", no rollout). New: the `citizenlk` wordmark on both chat pages and both privacy pages goes to `https://citizenlk.com` instead of `/`; privacy pages have a "Back to <service>" link. Checked from outside: the new links in the served HTML of the Bill Assistant chat page and both privacy pages; `/login` and `/privacy` 200 on both, `/api/search` 401 on both, `/public/me` enabled, homepage 200. NOT confirmed: the Procurement chat page's link (behind sign-in, so not fetched), and no click in a browser. Previous and rollback target: commit `db512c3`, Worker `54ecd50e` (Bill Assistant), Worker `35af7fa6` (Procurement).** **Privacy pages and English-only service pages, live since 2026-11-07 06:58 UTC: commit `db512c3` (merge of PR #74, on `main`; deployed from `main`), Python build `f6c858c3` (unchanged). Bill Assistant: Worker `gov-rag-llm` version `54ecd50e`, container app version 30 (image `sha256:36990acb...`, "no changes", no rollout). Procurement Rules: Worker `citizenlk-procurement` version `35af7fa6`, container app version 6 (image `sha256:7fb78ede...`, same code rebuilt, rollout completed, 1/1 instances). New: `/privacy` on both, footer links, no Sinhala or Tamil on the chat, sign-in and privacy pages. `PRIVACY_CONTACT` is unset, so the contact sentence is left out; chat logs still have no deletion date and the page says so. Checked from outside: `/privacy` and `/login` 200 on both, with the service's own text and no Sinhala or Tamil, footer links present, `/api/search` and Procurement `/api/ask` 401, `/internal/chat` 403. NOT confirmed: no question has reached the Phase 1 code yet (`chatlogs/` and `procurement-logs/` have no records dated 2026-11-07), so `build: f6c858c3` has not been seen in a log. Previous and rollback target: commit `5589fa7`, Worker `4960dd1f` (Bill Assistant); Worker `6b62cacd`, container v5, image `sha256:c2d81541...` (Procurement).** **Admin panel in its own Worker, live since 2026-11-07 06:42 UTC: commit `575e987` (merge of PR #72, on `main`; deployed from `main`). New Worker `citizenlk-admin` version `051822b8` on `admin.citizenlk.com` (own KV `citizenlk-admin-auth`, id `c828e50de45c430b897982f6ce06cb28`; secrets `OTP_PEPPER`, `CF_EMAIL_TOKEN`, `DEEPSEEK_API_KEY`; cron `23 * * * *`). `gov-rag-llm` version `4960dd1f`, Python build `f6c858c3` (unchanged), container app version 30 (image `sha256:36990acb...`, "no changes", no rollout), without the panel, its host, or the `LOGS` and `PROCUREMENT` bindings. Checked from outside: `admin.citizenlk.com` serves the sign-in page and sends every other signed-out GET there, POST `/admin/public-pause` 401; `bills.citizenlk.com` `/admin` 404, `/` and `/login` 200, `/api/search` 401, `/public/me` enabled. NOT confirmed: no signed-in view yet, so login mail on the new Worker, the cross-Worker Durable Object calls and R2 are unseen. Evidence to look for: `admin:settings` in Bill Assistant's KV after 07:17 UTC, `admin-rollups/` in R2 after 07:23 UTC. Previous and rollback target: commit `e705102`, `gov-rag-llm` Worker `c9c868a6` (it held the panel and `admin.citizenlk.com`; delete `citizenlk-admin` first so the domain is free).** **Admin panel moved to `admin.citizenlk.com`, live since 2026-11-07 06:31 UTC: commit `83fc85f` (merge of PR #70, on `main`; deployed from `main`), Python build `f6c858c3` (unchanged), Worker `gov-rag-llm` version `c9c868a6`, container app version 30 (image `sha256:36990acb...`, same as v29; rollout completed, 1/1 instances). Checked from outside: `admin.citizenlk.com` `/` -> `/admin` -> `/login` (panel wording), its `/api/search`, `/healthz`, `/public/me`, `/index.html` 404; `bills.citizenlk.com` `/admin` 404, `/` and `/login` 200, `/api/search` 401, `/public/me` enabled. NOT confirmed: still no signed-in view of the panel (see the entry below). Previous and rollback target: commit `f875697`, Worker `3a38f828`, container v29.** **Admin panel, live since 2026-11-07 06:23 UTC: commit `88371f1` (merge of PR #67, on `main`; deployed from `main`), Python build `f6c858c3` (unchanged, `billrag/` did not change), Worker `gov-rag-llm` version `3a38f828`, container app version 29 (image `sha256:36990acb...`, rebuilt from the same code; rollout completed, 1/1 instances). New: `/admin` (see "Admin panel"), new bindings `LOGS` and `PROCUREMENT` (state only), `CF_VERSION_METADATA`, var `ADMIN_EMAILS`. Procurement Rules and the homepage were not redeployed. Checked from outside: signed-out `/admin` redirects to `/login?next=/admin`, POST `/admin/public-pause` 401, login and chat pages 200, `/public/me` still enabled, `/api/search` 401. NOT confirmed: no signed-in view of the panel yet, so the `PROCUREMENT` binding, `getState()` over RPC and reading R2 from the Worker are unseen; first rollups expected from the 07:17 UTC cron (look for `admin-rollups/` in R2). Previous and rollback target: commit `70dd560`, Worker `f18b1552`, container v28 (image `sha256:9af80684...`).** **Launch Phase 1, both services live since 2026-11-07 05:43 UTC: commit `939ada6` (merge of PR #65, on `main`; deployed from `main`), Python build `f6c858c3`. Bill Assistant: Worker `gov-rag-llm` version `f18b1552`, container app version 28 (image `sha256:9af80684...`, rollout completed, 1/1 instances), cron `17 * * * *` added. Procurement Rules: Worker `citizenlk-procurement` version `6b62cacd`, container app version 5 (image `sha256:c2d81541...`, rollout completed, 1/1 instances). New: one SQLite connection per thread, gate 8/20 (public 2/6), cheaper vector scan, backup model on Workers AI, failed streamed answers no longer charged, credit alert, Procurement cost logging, gate and date notes (see "Load test" and the sections after it, and `procurement/README.md` "Dates"). Checked from outside: login pages 200, `/api/search` and Procurement `/api/ask` without a session 401, `/internal/chat` and `/internal/rerank` without the secret 403, the new page is served. NOT confirmed: no signed-in or visitor question has reached the new containers (look for `build: f6c858c3` in `chatlogs/` and `type: "ask"` with `calls` and `cost` in `procurement-logs/`), the Worker AI binding route for Procurement's backup, load on Cloudflare. Index in R2 replaced 2026-11-07 06:12 UTC: adds bill 087/2026, 47,080 chunks, the R2 copy compared byte for byte with the checked build, previous pair at `backups/bills/20261107T055340Z/`; an instance restores it when it next starts. The first credit check (06:17 UTC) recorded `alert:balance-low`, so the mail service accepted the email; its arrival is not confirmed. Previous and rollback target: commit `9bf12d8` (main before this change); Bill Assistant Worker `8b9cd384`, container v27 (image `sha256:9e0a50ce...`); Procurement Worker `e623ea98`, container v5's predecessor v4 (image `sha256:67f19dbd...`). The old code has the shared-connection bug.** **Procurement Rules, live since 2026-11-06 21:00 UTC: commit `ffd3afd` (merge of PR #62, on `main`; deployed from `main`), build `a9a5b67d`, Worker `citizenlk-procurement` version `e623ea98`, container app `a0382dc8-02a7-46d4-86f7-28e0b409284c` version 4 (image `sha256:67f19dbd...`, rollout completed 21:00 UTC, 1/1 instances, 0 failed). New: heading chain and clause pointers, follow-up rewriting (`history`, "Read as: ..."), "IPC" search term, the non-binding GPMCM guide in the baked index, `type: "ask"` + `standalone` in `procurement-logs/` (see `procurement/README.md` "Eval"). Checked: the same Dockerfile and code run locally at 256 MiB (74 MiB in use) answering a follow-up on deepseek-flash, without the reranker; on the live domain login 200, `/api/ask` without a session 401, `/internal/rerank` without the secret 403. Not yet confirmed: a signed-in question through the Worker to the new container, so no `procurement-logs/` record with `type: "ask"` has been seen. Previous: Worker `6b352ccd`, container v3 (image `sha256:05da8bca...`); rollback: check out `4b3ea5b` (main before this change) and redeploy (the baked index may keep GPMCM; that code does not search it).** **Bill Assistant with certified Acts, code live 2026-11-06 20:10 UTC: commit `8389703` (merge of PR #59, on `main`), Python build `a9a5b67d`, Worker `gov-rag-llm` version `8b9cd384`, container app version 27 (image `sha256:9e0a50ce...`, rollout completed 20:10 UTC). The index in R2 was replaced 20:27 UTC: 99 Acts, 46,975 chunks, both files checked by SHA-256 against the local build. The instance was asleep when this was written, so it restores the new index on its next start. NOT yet confirmed from a chat log or a signed-in session: look for `build: a9a5b67d`, `[Act ...]` citations and the `acts` field. Previous and rollback target: commit `a7a59bb`, build `96ceec81`, Worker `75fa5933`, container v26 (image `sha256:9df89b82...`); the previous index + vectors are at `gov-rag/backups/before-acts-2026-11-07/`: copy them back over `bills/` FIRST, then check out `a7a59bb` and redeploy. Merged but not live: Procurement Rules clause pointers and heading chains (PR #58, separate Worker).** **Procurement Rules question log, live 2026-11-06: commit `39c341f` (branch `ccr-304a7bd5-e1qwpt`, NOT yet merged into `main`), Worker `citizenlk-procurement` version `6b352ccd`, new R2 binding `LOGS` -> `procurement-logs/`. The container was NOT rebuilt: the deploy pointed at the existing image `sha256:05da8bca...` (container app version 3, "no changes"), because the baked index (`data/procurement/index.sqlite`) is not available in the sandbox. Not confirmed: no question has been asked since, so no `procurement-logs/` object has been seen (wrangler could not check that the R2 binding resolves). Previous Worker version: `2d76ff65`; rollback: redeploy the previous commit's Worker.** **Bill Assistant, live since 2026-11-06 19:14 UTC: commit `a7a59bb` (PR #56; deployed from the feature branch, merged right after with a merge commit), Python build `96ceec81`, Worker `gov-rag-llm` version `75fa5933`, container app version 26 (image `sha256:9df89b82...`, rollout completed 19:14 UTC, 1/1 instances). New: plain words instead of "GS" ("Gazette text" / "Bill") in Sources lines and in what the grader and answering model read (see "No GS jargon"), and the Latest-bills click no longer says "the The ...". Not confirmed: the live site is unreachable from the sandbox, so no chat log with `build: 96ceec81` has been seen; look for "Gazette text" in Sources lines and no "GS" in answers. Previous: container v25 (image `sha256:b2a01581...`, build `f26fba9a`, commit `4fdc3ca`) / Worker `a6d7a59a`; rollback: check out `9de77bc` (main before this change) and redeploy.** Before that: **Bill Assistant, live 2026-11-06 18:50-19:14 UTC: commit `4fdc3ca` (PR #55, coverage notice; deployed from the feature branch, merged right after as `9de77bc`), Python build `f26fba9a`, Worker `gov-rag-llm` version `a6d7a59a`, container app version 25 (image `sha256:b2a01581...`, rollout completed 18:50 UTC, 1/1 instances). New: `Store.coverage()` + `coverage` in `/healthz`, the footer line "Covers N bills from documents.gov.lk, ..." and the "Latest bills" list on the start screen (see "Coverage notice"). The first deploy attempt was blocked by the auto-mode check and the owner asked for a retry. Not confirmed: the live site is unreachable from the sandbox, so neither the footer line nor the list, nor a chat log with `build: f26fba9a`, has been seen. Previous: container v24 (build `2cd56dfb`, commit `b8f32f6`) / Worker `d4d8b242`; rollback: check out `b8f32f6` and redeploy.** Before that: **Bill Assistant, live 2026-11-06 18:09-18:50 UTC: commit `b8f32f6` (merge of PR #53, on `main`; deployed from `main`), Python build `2cd56dfb`, Worker `gov-rag-llm` version `d4d8b242`, container app version 24 (image `sha256:20a4ddc8...`, rollout completed 18:09 UTC, 1/1 instances). New: "answer that in Tamil/Sinhala/English" follow-ups re-render the previous answer (`Pipeline.switch_language`), a machine-translation note under translated Tamil/Sinhala answers (`translate.MACHINE_NOTE`, wording NOT reviewed by a speaker), and the chat page no longer names the languages it answers in (DESIGN.md principle 6). The public lane stays on. Not confirmed: the live site is unreachable from the sandbox, so no chat log with `build: 2cd56dfb`, no `path: language_switch` record and no Tamil/Sinhala note has been seen. Previous: container v23 (image `sha256:482d21e4...`, build `ce5784c6`, commit `e52f6d5`) / Worker `5311367e`; rollback: check out `e52f6d5` and redeploy.** Before that: **Bill Assistant PUBLIC LANE ON, live since 2026-11-06 16:52 UTC: commit `e52f6d5` (only `PUBLIC_CHAT = "on"` in `wrangler.toml` changed), Python build `ce5784c6` (unchanged), Worker `gov-rag-llm` version `5311367e`, container app version 23 (same image `sha256:482d21e4...` as v22, rollout completed 16:52 UTC, 1/1 instances). Deployed from the feature branch, then merged into `main` with a merge commit. Confirmed by the owner 2026-11-06 after the deploy: "the public lane works on my phone" (what exactly was tried is not recorded). Not confirmed: any `lane: public` chat log record (the live site is unreachable from the sandbox), behaviour under real load or on shared mobile IPs, and the Sinhala/Tamil quota messages (not reviewed by a speaker). Rollback: set `PUBLIC_CHAT = "off"` and redeploy (previous: Worker `223300aa`, container v22, same image).** Before that: **Bill Assistant, live since 2026-11-06 15:03 UTC: commit `8d852bd` (PR #49, public lane, see "Public lane"; `PUBLIC_CHAT = "off"` so behaviour for visitors is unchanged), Python build `ce5784c6`, Worker `gov-rag-llm` version `223300aa`, container app version 22 (image `sha256:482d21e4...`, rollout completed 15:02 UTC, 1/1 instances), Durable Object migration `v2` (`QuotaCounter`) shipped with it. Deployed from the feature branch, then merged into `main` with a merge commit. Not confirmed: the live site is unreachable from the sandbox, so neither the migration, the new container, nor a chat log with `build: ce5784c6` has been checked. Previous: container v21 (image `sha256:e0f19bb5...`, build `ba22b53e`, commit `4c0e56c`); rollback: check out `4c0e56c` and redeploy.** Before that: **Bill Assistant, live 2026-11-06 14:42 UTC: commit `4c0e56c` (concurrent questions + queue, see "Concurrency"), Python build `ba22b53e`, Worker `gov-rag-llm` version `6bfd5a8d`, container app version 21 (image `sha256:e0f19bb5...`, rollout completed 14:42 UTC, 1/1 instances). Deployed from the feature branch first, then merged into `main` with a merge commit. Not yet confirmed from a chat log (look for `build: ba22b53e`, `queue_wait_s`, `concurrent`) and not load-tested against the real container. Previous: container v20 (image `sha256:03fd7aaa...`, Python build `ec82b284`, commit `b1fcca3`); rollback: check out `b1fcca3` and redeploy.** Procurement Rules, live since 2026-11-06 13:05 UTC: commit `202ad3d` (merge of PR #46), `procurement.citizenlk.com`, Worker `citizenlk-procurement` version `2d76ff65`, container app `a0382dc8-02a7-46d4-86f7-28e0b409284c` version 3 (image `sha256:05da8bca...`). Checked: the same image run locally against the live reranker and DeepSeek; login, 401/403 gates and the login mail request on the live domain. Not yet confirmed: a signed-in question through the Worker to the deployed container. Homepage: Worker `citizenlk-home` version `231f3b18` (commit `ce13d2c`, links to both services).** **Live since 2026-11-06 12:45 UTC: commit `b1fcca3` (merge of PR #42, on `main`): Bill Assistant moved to `bills.citizenlk.com`, workers.dev turned off, homepage live on `citizenlk.com` (Worker `citizenlk-home`, version `37a6a5ae`). Python build `ec82b284` is unchanged. Worker `gov-rag-llm` version `95735d1e`, container app version 20 (image `sha256:03fd7aaa...`). Not yet confirmed from a chat log or a signed-in session. Previous: commit `70b48da`, Worker `b14d1f49`, container v19, image `sha256:1a9bdffc...`; rollback: check out `70b48da` and redeploy.** Before that: **Live since 2026-11-06 12:27 UTC: commit `70b48da` (merge of PR #40, on `main`). Python build `ec82b284` is unchanged (`billrag/` did not change since the previous deploy). Worker version `b14d1f49`, container app version 19 (image `sha256:1a9bdffc...`, rebuilt from the same `billrag/` code; rollout completed 12:28 UTC, 1/1 healthy).** New in this deploy: the citizenlk look on the chat page (`public/index.html`) and the sign-in page (`LOGIN_PAGE` in `src/auth.js`), see `DESIGN.md`. Not yet confirmed in a browser or from a chat log (workers.dev is not reachable from the sandbox). **Previous deploy and rollback target:** commit `e5dd298` (merge `2a4c1a3`, PR #35), build `ec82b284`, Worker `be713b7d`, container v18 (image `sha256:ab012c93...`), live 2026-11-06 10:11 UTC; everything described below was already live then. Contains: bill metadata
(`meta.py`), newest-instance rule, section-reference following (incl. penalty back-references), "which bill?" clarification, literal
translate-in, prompt-echo guard, per-response cost log (`cost`, `calls[].usd`), the `CHAT_PROVIDER` switch (`src/chatconfig.js`) and
the independent-judge eval. The index in R2 was re-uploaded 2026-11-06 10:08 UTC with the metadata tables (`bill_meta`, `section_refs`).
**Chat model:** `CHAT_PROVIDER = "deepseek"` and the `DEEPSEEK_API_KEY` Worker secret exist (owner added it 2026-11-06) and the container was restarted by the v18 rollout, so it should be answering with `deepseek-flash`. Not yet confirmed from a chat log: no question had been asked on the new instance when this was written; look for `build: ec82b284`, `model: deepseek-flash`, `cost` and empty `warnings`. If a log shows `model: @cf/openai/gpt-oss-120b`, the key was not picked up (Worker log warns "DEEPSEEK_API_KEY secret is missing"). Previous: container v16, build `479f9cd3` = `main` at `ce7e189`, Worker
`76ad7347`, image `sha256:586ad1ec...`. Rollback: check out `ce7e189` and redeploy (steps 1-5 above; the new index tables are ignored by old
code). `main` also has later commits (procurement prototype, worktree script) that touch no deployed code. Check `git log origin/main`
against the `build` seen in the latest chat log before telling the owner a change is live.

## Rules

- **Secrets:** never write credentials (R2, API keys) into the repo, docs, commits or logs. Read them from env vars
  only (`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `CLOUDFLARE_API_TOKEN`, ...).
  Production secrets live as Worker secrets (`wrangler secret put`), never in `wrangler.toml`. Tokens pasted into a chat
  are in that transcript forever: rotate them afterwards. `data/` is git-ignored; keep it that way (PDFs are ~700 MB).
- **Production deploys need the owner's explicit go-ahead** in that turn (the harness blocks them otherwise). Merging a PR
  does NOT deploy: there is no GitHub auto-deploy. Always say what is merged vs what is live.
- **Search is English only.** Retrieval, embeddings and evals run on the English text. The official Sinhala text of
  2025+ bills is stored alongside (`local_sections`, see "Official Sinhala text") and used only to word Sinhala answers;
  never put it into `chunks` (that changes `fingerprint()` and invalidates `vectors.npz`). Sinhala PDFs up to 2024 (FM
  Abhaya legacy font) and all Tamil PDFs are not handled yet (see "Tamil: official text" for why and the plan).
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

**Translation model** (`BILLRAG_TRANSLATE_MODEL`, Worker var `TRANSLATE_MODEL`; `translate.make_translator`): unset = the chat model.
A chat model id is called on the same endpoint WITHOUT `reasoning_effort`. An id containing `m2m100` (`@cf/meta/m2m100-1.2b`) is a
pure MT model called via `/ai/run`: `M2M.translate_keeping_cites` translates line by line and keeps citations, bullets and
punctuation-only pieces out of the model; `ai` language detection then still uses a chat model. Any failure (error, reasoning text,
wrong script, changed citations) falls back to the main chat model (one firmer retry on the last model), and the warning lists each
attempt's reason. `drop_invented_refs` removes any `NNN/YYYY` the translation added (a bill number restricts retrieval to that bill).

**Measured 2026-11-05** on the first live Sinhala question ("which criteria define a terrorist act in this new bill") and its
712-char English answer, translated back to Sinhala and Tamil (n=1 question, 3 runs for the question; read the outputs, not just ok):

| model | question -> English | answer -> si / ta | verdict |
|---|---|---|---|
| `@cf/openai/gpt-oss-120b` (chat model) | ~1 s; added "summary" 2/3 runs | 7-12 s / 23 s; "terrorist act" -> තර්ජන ("threat") | fast, wrong legal term |
| `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | ~1.3 s; identical and correct 3/3 | 34-37 s / 28 s; correct ත්‍රස්තවාදී | **chosen**: quality over speed |
| `@cf/meta/llama-4-scout-17b-16e-instruct` | ~0.9 s; correct | 11 s / 23 s; Chinese characters (意图) inside the Sinhala | rejected |
| `@cf/meta/m2m100-1.2b` (MT) | 3.6 s; meaning lost ("framework") | 18 s / 26 s; broken Sinhala conjuncts, "Sri Lanka" wrong, English left in Tamil | rejected |
| `@cf/google/gemma-4-26b-a4b-it` | no usable output | ~107 s; reasoning/empty | rejected |

`@cf/google/gemma-3-12b-it` does not exist on this account (check `GET /accounts/<id>/ai/models/search` before configuring a model).
`@cf/ai4bharat/indictrans2-en-indic-1B` (Translation) is listed too, untested. With an example bill number in the prompt
(`like 077/2026`), both Llama models added "077/2026" to a question that named no bill: never put concrete numbers in prompts.
To trade quality for speed, unset `TRANSLATE_MODEL` (gpt-oss translates, ~30 s faster on a Sinhala answer).

## Official Sinhala text (2025+ bills)

Bills are enacted in Sinhala and Tamil with an English translation, and bills say so themselves: e.g. 078/2026 §75: "In the
event of any inconsistency between the Sinhala and Tamil texts of this Act, the Sinhala text shall prevail." So a Sinhala answer should use the
bill's own Sinhala wording, not a machine translation of the English. `billrag/parse_si.py` + `python -m billrag ingest-si`:

- **What extracts:** Sinhala PDFs from 2025 on use Unicode fonts (IskoolaPota) and extract as real Sinhala with **PyMuPDF**
  (`pypdf` and `pdfminer` both scramble the pre-base vowel sign: හෝ -> "තෝහා"). Up to 2024 they use **FM Abhaya** (Sinhala glyphs on
  Latin code points: needs an FM -> Unicode table, not done). 35 of the 180 bills from 2025-26 use a **visual-order** font
  (ෙ before the consonant, ්‍ර conjunct dropped: "ශී ලංකා"): letters are missing, so they are rejected rather than quoted misspelt.
- **Cleanup:** the font map doubles vowel signs (ආරක්ෂාා, හෝ�ෝ˪) and some ligatures (රාජ්‍යයය): `clean` collapses repeats and
  drops stray glyphs. Layout by PyMuPDF block geometry: running header (top 10%), line numbers 5/10/15/20/25 (digit-only blocks),
  margin notes (blocks outside the text column; kept as `heading`).
- **Sections:** start after the enacting clause ("... මෙසේ පනවනු ලැබේ", may wrap mid-word); each number is printed twice ("13." alone,
  then "13. text"); text may start with a digit ("13. 6 වන ..."); a ZWJ can precede the number. Sequence-checked like English.
- **Linking (fail-safe):** a bill is linked only if its Sinhala sections are exactly the English bill's 1..max(section_no); within
  it, a section whose length is outside 0.4-2.5x the English one is left out (schedules or tables slipping into a section).
  `local_bills` records every bill's status. **Result (2026-11-05): 143 of 180 bills linked, 3,456 sections**; 35 visual-order
  font, 2 with no English sections. Length ratio Sinhala/English over 3,164 sections: median 0.99, p5 0.76, p95 1.18.
- **Use:** retrieval and the English answer are unchanged. For a Sinhala question, `Pipeline.official_texts` looks up the evidence
  sections (or a sibling GS/Bill version) and the translate-out call gets them as a reference block (labels without [ ] so they
  cannot pass the citation check). `Result.official` / log field `official` lists them; sources say "official Sinhala text used".
- **Measured** (one question, 070/2026 §3; official terms terror/hostage/protest): Llama 3.3 without reference 0/3 in 45 s, with
  1,400 chars 2/3 in 38 s, with all 3.4k chars 3/3 but 2 citation-check failures first (79 s). gpt-oss with reference: ~7 s but
  inconsistent (0-2/3, one reasoning leak). Reference is capped at 2,500 chars per section. Translate-out stays the slow step.
- **Production:** the container restores `index.sqlite` from R2, so after `ingest-si` upload it (`upload_r2.py --include-index`)
  as part of the deploy. Old code ignores the new tables, so the upload is safe before the code is live.

### One-step Sinhala answer (`Result.path`, log fields `path`, `answer_en`, `direct_fallback`, `quote_fixes`)
The first live Sinhala question (2026-11-05 17:41) took 118 s, 103 s of it Llama translate-out, and the answer contained a
degenerate repetition (ආන්ඩ්‍රාත්‍රාත්‍රාත්‍රාය) that `check_out` let through. Now:
- **`path = "direct"`:** a Sinhala question whose kept evidence has official Sinhala text (`official_texts`) is answered by the
  answer model (gpt-oss) directly in Sinhala: the answer prompt gets each section's English text plus its official Sinhala text
  (`LOCAL_NOTE`: take terms from the official text, quote penalties/offences/definitions from it). The user's Sinhala question
  and the English search rendering both go in. The answer must pass `translate.check_script` (script, other scripts, garble),
  contain a citation and pass `Pipeline.verify`; otherwise `direct_fallback` records why and we take the old path.
  `Pipeline(direct_local=False)` turns it off. `answer_en` is empty on this path (no English answer exists).
- **`path = "translated"`:** English answer + translate-out (as before): bills without official text, evidence that is only
  bill-level (no sections), Tamil/other languages, or a rejected direct answer. `answer_en` is logged now.
- **Garble check (`translate.garbled`)**, used by `check_out` and the direct path: a 2-12 char unit containing a letter repeated
  3+ times in a row, one letter/mark 5+ times, a word > 45 chars (after stripping dotted leaders), unassigned code points (a
  live answer had U+0DCD), and for si/ta any character from another script (e.g. Chinese). Zero hits on all 3,456 official
  Sinhala sections; on 21,630 English 2020+ sections it flags 11, all genuinely broken PDF text.
- **`degenerate()` counted only `isalpha()`**: Sinhala vowel signs are marks, so real Sinhala sits at 46-53% "letters", right
  at the 40% junk cutoff; a short Sinhala answer with citations was treated as junk. Marks now count and citations are ignored.
- **`translate.fix_quotes`**: gpt-oss writes ථ්‍රස්තවාදී for ත්‍රස්තවාදී even *inside a quotation of the official text*. A quoted
  span (or each piece between `…`) that is >= 93% similar to a same-length window of the official text is replaced by that
  window, so a quote is always the official wording. Text outside quotes is never changed. **A word-level "snap to official
  spelling" was tried and rejected:** on official text vs. other sections of the same bill it changed 602 of 669k correct words,
  some changing meaning (නීතියක් "law" -> රීතියක් "rule", ප්‍රදාන "grant" <-> ප්‍රධාන "main"; case endings ක/ට/ල are consonants).
- **Stored text artifact:** 646 of 3,456 sections had a vowel sign wrapped onto the next line and doubled (ත්‍රස්තවා\nාදී).
  `parse_si.join_marks` repairs it when the text is read (and for future `ingest-si`); the R2 copy is unchanged. Still in the
  stored text: a doubled ය after ්‍ය in 444 sections (ජාත්‍යයන්තර, අමාත්‍යයවරයා; but රාජ්‍යය is correct, so no blind fix), and
  words split across lines (නැතහො\nත්). The model copies these.
- **Measured live 2026-11-05** (gpt-oss answers, Llama 3.3 translates, same index; 4 Sinhala questions, 10 runs; read the outputs):

  | question | path | total s | answer step s | notes |
  |---|---|---|---|---|
  | live Q (terrorist act, "this new bill") | direct x2 | 47.7 / 64.3 | 35.3 / 42.1 | official terms (භීෂණය); one "හනිය" typo, one "ගෞරවදායක" ("honourable") for serious harm; relies on 085/2026 §14 only |
  | same, old path (`direct_local=False`) | translated | 64.3 | 5.9 + 39.7 translate-out | output cut off mid-word; "ප්‍රතිකාර" ("treatment") for Prevention |
  | same, run 3 | translated | 43.8 | 6.1 + 30.4 | grader kept only the 078/2026 bill overview -> no sections -> no official text |
  | 085/2026 foreign terrorist fighter | direct x3 | 17.2 / 54.4 / 36.5 | 10.5 / 45.6 / 22.8 | run 1 (before the quote-the-penalty prompt line) made up "ද්විත්ව වරදකරු ගැණුම" for "imprisonment of either description"; runs 2-3 quoted the official text (run 2's ථ misspelling is fixed by `fix_quotes`) |
  | 071/2026 visa cancellation | direct x2 | 15.8 / 26.5 | 4.4 / 18.3 | accurate; official terms (පාලක ජනරාල්වරයා, අවනිශේධනය, ඇතුළුවීමේ පිටසන) |
  | 078/2026 hostage penalty | translated | 18.6 | - | **translate-in failure:** Llama rendered the title as "Prevention of Terrorism Act" -> nothing kept -> "not found" (the right English finds 078/2026 §13) |

  Zero citation warnings, zero garble hits, zero direct-path fallbacks in these runs. Hosted-model latency varies 3-10x between
  runs of the same prompt, so n=2 per question says little about speed; the direct path removes the translate-out call
  (30-103 s). Quality is better than translate-out but not clean: expect occasional wrong words. Open problems: translate-in
  can lose a bill title; on the live question the grader's kept evidence changes from run to run (070 §3 + 085 §14, or only
  085/2026 §14, or only the 078/2026 overview), so the "which bill" problem is partly in grading, not only in the answer step.
- **Cut-off replies:** `Reply.stop` / `Reply.truncated` (OpenAI `finish_reason == "length"`, Anthropic `max_tokens`; Workers AI
  reports it, verified). Llama translate-out copying long official passages was cut off mid-word twice at 2,400 tokens and
  `check_out` passed it. Now a cut-off translation counts as a failed attempt and a cut-off direct answer falls back; limits for
  Sinhala output are 4,000 tokens (Sinhala needs several tokens per word). `direct_rejected` logs the rejected direct answer
  (one live run was rejected for "no citations" and could not be diagnosed).

### "This new bill" without a name (`Pipeline.focus_bill`, `Result.focus`, log field `focus`)
If the English question matches `UNNAMED` ("this/that [new|proposed|...] bill/act/law/draft", or "the new/proposed ... bill",
not followed by "no."), has no `NNN/YYYY`, and the kept evidence spans more than one bill, the bill family (its GS + Bill versions,
grouped by `title_norm`) with the highest grade wins (ties: total grade, then newest). Its clauses go first and the answer prompt
(English and direct Sinhala) gets `FOCUS_NOTE`: answer about that bill, name it first, mention other bills only as clearly
different bills. Fires on 2 of 134 eval questions (both name their bill, so the focus is the named one). An earlier pattern that
also accepted plain "the Act"/"the law" fired on 9, mostly wrong ("the law governing ..."). Live (2 runs of the logged question)
it did not fire because the grader kept only one bill each time: it fixes the answer step, not grading. 2026-11-06 it fired
(grader kept 070/2026 §3 = 3, 085/2026 §14 = 2): the direct answer named Protection of the State from Terrorism (070/2026) and
used the official terms භීෂණකාරී, ප්‍රාණ ඇපකරුවන්.

### Summary questions, retries, unverified quotes (2026-11-06)
- **`Pipeline.bill_sections`:** for a Sinhala question whose kept evidence is ONLY bill-level (overview) chunks - typical for
  "tell me about / summarise the X bill" - the top bill's own best-matching sections (retrieval restricted to it and its GS/Bill
  versions, k=4, no extra LLM call, grade 2) are added, so the answer rests on clause text and the official Sinhala text and can
  take the direct path. Log field `added_sections`, step `bill_sections` (~1 s). English questions are unchanged (bill-level
  questions are what the eval's bill-level items test; not re-measured with sections added).
- **Direct answer retried once** before falling back: a rejection is usually one stray character (live: 枠, ।, Chinese), the retry
  costs ~15 s, while the translate-out fallback costs 30-100 s and reads worse. `direct_fallback = "retried after: ..."` when the
  retry succeeded; both reasons when both failed. The prompt now also says: only Sinhala + ASCII, and quotation marks only for
  official wording.
- **Accented Latin counts as a foreign script** in si/ta answers (a live translate-out answer had "උදාහරණként", Hungarian -ként).
  Plain ASCII stays allowed (bill titles). No official section is flagged.
- **`fix_quotes(..., code=...)` de-quotes unverified quotes:** a quotation in the answer's language that matches no official
  passage (not even >= 93%) loses its quotation marks (logged in `quote_fixes` as `(quote, None)`); live answers put paraphrases
  of the auto-generated summary in quotes. English quotes (titles) are untouched. U+200B is stripped from direct answers.
- **Measured live** (gpt-oss, same index): cricket "Mata kiyalanna 2026 cricket bill Eka gana" 67 s live (translated, "municipal
  body" for corporate body, Hungarian suffix) -> 28-30 s direct (one run after a retry); "...summary eka" 61 s -> 25-28 s direct;
  "2026 ආගමන පනත් කෙටුම්පත ගැන කෙටියෙන් කියන්න" 94 s (direct rejected for 枠, translated) -> 17.9 s direct after the retry
  change. Remaining quality issues: odd coinages (අධික්ෂේපණය, කොපරේට් ශරීරය), labels like "[072/2026 summary]", and one
  run of the terrorist-act question where the grader kept nothing in both rounds ("found nothing"; grader variance, not new).

### Glossary: official titles and defined terms (`billrag/glossary.py`, table `glossary`, 2026-11-06)
Mined deterministically (no model) from the linked section pairs; lives outside `chunks` (fingerprint/vectors unaffected).
- **Sources:** each bill's §1 short title ("cited as the X Act" <-> "... දරන Y පනත යනුවෙන් හඳුන්වනු ලැබේ"; also "... සංශෝධනය ලෙස",
  lower-case Appropriation text, the misspelt හදුන්වනු, stray ZWJ/space "යනු‍වෙ න්"); interpretation sections ('"X" means/includes'
  <-> '"Y" යන්නෙන්/යන්නට'); "hereinafter referred to as the "X"" <-> '"Y" යනුවෙන් සඳහන්'. Terms are paired in order and only when
  both sides of a section have the same count (mismatched sections are skipped, not guessed). Order holds in the samples read.
- **Result:** 306 entries: 76 titles (140 of 141 linked bills; 573/2025's English §1 is mis-parsed), 147 defined, 83 referred.
  Common ones: principal enactment = ප්‍රධාන ප්‍රඥප්තිය (49 bills), bank = බැංකුව (12), written law = ලිඛිත නීතිය (10).
- **Doubled ය:** the artefact font is in 83 bills and is MORE frequent than the correct spelling (අමාත්‍යයවරයා 415 vs 376), and even the raw
  PDF cannot tell it from a real ්‍යය (රාජ්‍යය ආයතන vs රාජ්‍යය සහ in one file). For glossary terms only, the spelling found in the clean
  bills wins (`fix_double_ya`, 5 fixes, recorded in `note`); ව්‍යයවස්ථා in the 22nd Amendment title stays (no clean evidence).
- **Bill text, not our bug:** 038/2026 §17 defines "eligible exporter" as සුදුසුකම් ලබන **ආනයනකරු** ("importer"). Kept as the bill says.
- **Use:** (1) translate-in: `Glossary.in_question` finds titles and multi-word terms in a Sinhala question and the TO_EN prompt gets
  them as "official English names"; `Pipeline.keep_titles` appends a title the translation still dropped. Log `glossary_hints`.
  (2) answers: `for_english` picks entries whose English term occurs in the evidence (multi-word, or one word in >= 3 bills; at most 15)
  and both the direct prompt and translate-out get them as "OFFICIAL SINHALA TERMS" (no [ ]). Log `glossary_terms`.
- **Measured live** (5 Sinhala questions): the hostage question that failed (Llama: "Prevention of Terrorism Act" -> found nothing)
  now searches "Protection of the State from Terrorism Act" and finds 070/2026 §3, with the official ප්‍රාණ ඇපකරුවන්. It still does
  not give the penalty: §4(b) sets it by reference to §3(2)(c) paragraphs and never says "hostage", so it needs cross-reference
  following (not done). Others: 17.7-46.4 s, all direct, no warnings; one "not found" for the terrorist-act question (grader kept only
  018/2026 §16: grader variance).
- **Production:** the container restores `index.sqlite` from R2, so the glossary only goes live after `upload_r2.py --include-index`
  (old code ignores the table; the code without the table just has no hints).

## Tamil: official text - feasibility check (2026-11-06, nothing built yet)

Today a Tamil question takes the **translated path only**: translate-in, English answer, translate-out by the chat model. There is no official
Tamil text, no Tamil glossary, no direct path (`pipeline.py` gates all of that on `lang == "si"`), and Tamil quality has never been measured.
A live public-lane Tamil follow-up ("In tamil", after a budget-bill answer) was treated as a NEW question, searched "can you say this in Tamil?"
and answered about the "Sinhala text shall prevail" clause: the condense step does not turn "answer that in Tamil/Sinhala/English" into a re-render
of the previous answer (open bug, also for Sinhala).

**Question: can we get the official Tamil text out of the PDFs like we did for Sinhala? Answer: not from the text layer; yes by OCR.**
Fetched all 180 Tamil PDFs of 2025-26 (`fetch_index.py --pdfs TAMIL --since-year 2025`, 65 MB, 5,940 pages; 95 from 2025, 85 from 2026) and
looked at the extracted text (page samples + automatic symptom counts, not a line-by-line audit):
- All 180 have real Tamil Unicode (fraction of Tamil characters 0.85-0.95), none are scanned. But **no file has usable text**, in three ways:
  - **~146 files (most 2026 bills): broken font mapping** (embedded Arial Unicode MS subsets): doubled vowel signs (`செெயல்` for `செயல்`,
    `இலங்்க`), doubled consonants (`ததவறு`), U+FFFD where `ோ`/`ொ` should be, a stray Syriac letter (`இணܡந்த`), and each line printed twice.
    Information is lost, not just reordered. PyMuPDF, pdfminer, pypdf and `pdftotext` all fail on it (differently), so the library is not the cause.
  - **~31 files (mostly 2025, 12 from 2026): legacy-style encoding in Unicode code points**: pre-base vowel signs in visual order (`ேசர்ெபᾠமதி`)
    and Greek letters standing for `ு`/`ூ` combinations (`ᾠ` = று, `ᾌ` = டு, `ᾞ` = ரு, `ᾙ` = மு, `ᾫ` = ளு). A deterministic repair table
    looks possible but was not tried; OCR covers these too, so it is not needed.
  - **~3 files: unassigned code points** in the Tamil block (`஌ ஺ ஻ ௄`).
- **A first-glance "no symptom" check is misleading**: the 31 legacy-encoded files pass a doubled-sign/U+FFFD check and are still unreadable.
  Count unassigned code points (`unicodedata.category == "Cn"`) and Greek/Syriac letters too.
- **OCR works.** `apt-get update && apt-get install -y tesseract-ocr tesseract-ocr-tam` works in the sandbox (apt lists are empty until `update`);
  `raw.githubusercontent.com` (tessdata) and PyPI are reachable, `github.com/.../releases` is 403 (so EasyOCR's model download is blocked).
  Pages rendered with PyMuPDF at 300 dpi, `tesseract page.png out -l tam --psm 4`: on 070/2026 (a garbled 2026 file) a contents page and a
  clause page came out as clean, readable Tamil with the right legal terms (`பயங்கரவாதத்திலிருந்து அரசைப் பாதுகாப்பதற்கான`). About 0.8 s wall per page
  on one page tried; 5,940 pages on 4 cores is therefore an estimate of about an hour, not a measurement. Flaws seen (2 pages only): list labels
  (i)/(ii) read as `()` / `(॥)`, the margin line numbers (5, 10, 15) merge into the text, one doubled `்`, a noise character at the end of a page.
  ZWNJ (U+200C) after the virama is normal in the output.
- Not checked: OCR accuracy over whole bills or on the other 179 files, tables/schedules, the 2025 files' layout, how Tamil section numbering and
  the enacting clause look, whether `local_sections.lang = 'ta'` linking (1..max, length ratio) succeeds for most bills.

**Plan if we build it (same design as Sinhala, fail-safe):** render + OCR every page with word boxes/confidence (TSV), drop the line-number and
margin-note columns by geometry, drop low-confidence lines, split into sections (sequence-checked against the English 1..max), link only bills
that match exactly and sections within the 0.4-2.5x length ratio, store in `local_sections` (`lang='ta'`; the table already has the column),
mine a Tamil glossary (short titles, interpretation sections), then generalise the `lang == "si"` gates in `pipeline.py`/`translate.py` (direct
path, glossary hints, `fix_quotes`, `check_script` already knows Tamil) and re-upload the index to R2. Risk to manage: a quotation would carry any
OCR misreading as "official wording", so keep the confidence filter and drop unlinked sections; with no Tamil eval set and no Tamil reviewer the
quality stays unmeasured until a native speaker reads ~20 answers. Interim steps (written 2026-11-06, see below; deployed only when the "Deployed vs main" paragraph says so):
- **Machine-translation note:** `translate.machine_note` / `MACHINE_NOTE` (ta, si): `web.py` appends an italic line under a Tamil or Sinhala
  answer that was translated from the English answer (`path` translated or language_switch, and the answer really is in that language);
  never for English, never for a direct Sinhala answer written from official text. The two texts were written by the assistant and are NOT
  reviewed by a speaker.
- **"Answer that in Tamil/Sinhala/English" follow-ups:** `translate.language_switch` recognises a short message that only names a language
  (plus filler like "can you say this in", "pls", "kiyanna", Sinhala/Tamil script variants; any other word, a second language, or more than 8
  words means it is a real question). With a previous answer in the chat, `Pipeline.switch_language` re-renders it (`path = "language_switch"`,
  one translate-out call, no search, same evidence/sources, official Sinhala terms when the target is Sinhala); "english" needs no call unless
  the previous answer was a direct Sinhala one (then it is translated back). A failed translation shows the English with a warning.
- **Languages are not advertised** (owner's decision 2026-11-06, see DESIGN.md principle 6): the chat page's placeholder and start text no longer
  say "Sinhala, Tamil or English". The capability stays quiet.

## Bill metadata (`billrag/meta.py`, tables `bill_meta`, `section_refs`)

Side tables like the glossary: `chunks`, `fingerprint()` and `vectors.npz` are untouched, old code ignores them, and code without
them degrades to no metadata (every read tolerates missing tables). `python -m billrag meta` (~seconds, deterministic; `ingest` runs it
too) fills `family` (GS + Bill versions of one title within 300 days), `stage`, `bill_type` (appropriation | incorporation | amendment |
new), `amends`, `is_latest` (newest instance of a recurring title; all "Appropriation ..." titles count as one) and `section_refs`
(11,361 "section N" pointers inside a bill). `python -m billrag tag --since-year 2024` adds LLM topic tags (fixed taxonomy
`meta.TAGS`, resumable, survive a rebuild); NOT run yet. Filters: `billrag search --type/--tag/--latest`, agent tools `find_bills` /
`search_bills` (`bill_type`, `tag`, `latest_only`), `get_section` lists the sections it refers to. `Pipeline(prefer_latest=True)`:
a question with no year and no bill number whose hits come from an older instance of a recurring title is searched again inside the
newest instance (event `newest_instance`). Fixes "appropriation bill" answering about 568/2025 instead of 069/2026 + 080/2026.
Measured 2026-11-06 (deepseek-flash, thinking off, 24 recent questions): strict 0.79 -> 0.83, cited gold 0.88 -> 0.96 (noise level).
**Production:** the container restores `index.sqlite` from R2, so metadata goes live only after `upload_r2.py --include-index`
and a deploy. `Pipeline.follow_section_refs` (flag `follow_refs`, default on) uses `section_refs`: for the TOP kept clause's bill (and its GS/Bill
versions only) it adds up to 2 sections the clause points at, and for a question about penalties/offences up to 2 sections that point
AT the clause and speak of imprisonment/fine/liability (the penalty section s.4 points back at the offence section s.3 and never
says "hostage"). Fixes the hostage penalty (070/2026 §4(b): <= 20 years rigorous imprisonment + fine <= Rs 20m); 4 of 5 phrasings
answer it, the 5th is not a penalty question and says honestly that the penalty section is not in its evidence. 72-item eval unchanged
(strict 0.93, cited gold 0.97). Not done: using tags in ranking.

## Priorities, DeepSeek, cost log, clarification (2026-11-06)

**Priority order set by the owner: honesty and accuracy first, then cost, speed last.** Choose models by strict/cited_gold and the
`warned` rate, not latency.
- **Eval:** `eval-answers --since-year 2025 --limit 0` is the 72 recent items; 24 items moved by +-0.12 between identical runs, so use
  72 and an independent judge (`BILLRAG_JUDGE_MODEL/_BASE_URL/_API_KEY`). The report has a `warned` column (answers with an
  unsupported citation or an unusable grader reply). Rows: `evals/answer_model_comparison/72.*.jsonl`.
  | 72 recent items, judge deepseek-v4-pro | strict | cited gold | warned | avg s |
  |---|---|---|---|---|
  | deepseek-flash, grading thinking OFF, answer thinking ON (best) | 0.93-0.94 | 0.94-0.96 | 0.00-0.03 | 9-10 |
  | same, with Acts in the index (`acts`, 2026-11-07; gold includes the Act's matching section) | 0.94 | 0.99 | 0.03 | 11.1 |
  | gpt-oss-120b (production) | 0.89 | 0.92 | 0.00 | 15.8 |
  | gpt-oss-20b | 0.83 | 0.88 | 0.00 | 12.3 |
  | deepseek-flash, all thinking | 0.90 | 0.93 | 0.54 (grader JSON unusable) | 21.8 |
  | deepseek-v4-pro, all thinking | 0.90 | 0.93 | 0.69 | 97.7 |
  The judge is DeepSeek-family (possible bias for DeepSeek rows; cited_gold does not need a judge). NOT deployed: production still uses
  Workers AI gpt-oss-120b. Using DeepSeek in production needs a Worker secret and `api.deepseek.com` reachable from the container.
- **Config for that setup:** `BILLRAG_CHAT_PROVIDER=openai BILLRAG_CHAT_MODEL=deepseek-flash BILLRAG_CHAT_BASE_URL=https://api.deepseek.com
  BILLRAG_CHAT_API_KEY=$DEEPSEEK_API_KEY BILLRAG_GRADE_EFFORT=off` (answer effort unset = thinking on). `off`/`none` sends
  `thinking: {type: disabled}` + `reasoning: {enabled: false}` (llm._effort_body). Thinking-on grading makes the grader's JSON unusable
  (pipeline falls back to retrieval order, warning) so keep grading off. `deepseek-v4-pro` costs ~98 s per question: not worth it.
- **Cost per response:** `billrag/calltrace.py` prices every chat call (`usd` in `calls`) and `web.py` logs `cost` = {usd, in, out,
  cache_hit, chat_calls, priced_calls, peak}; `ask -v` prints it; `read_chatlogs.py` prints total/median/p90 and cache-hit share.
  Prices come from the DeepSeek docs (api-docs.deepseek.com/quick_start/pricing, read 2026-11-06, per 1M tokens, off-peak: flash
  hit $0.003, miss $0.15, out $0.60; v4-pro $0.022 / $0.66 / $1.98); peak (01-04 and 06-10 UTC, Mon-Fri, not Chinese public holidays)
  is x2, holidays are not known so those days are an upper bound. Reasoning tokens are inside `completion_tokens`. Override with
  `BILLRAG_PRICES='{"model": {"hit":..,"miss":..,"out":..,"peak_factor":2}}'`. Workers AI models have no price here (tokens only,
  `priced_calls` < `chat_calls`). `GET api.deepseek.com/user/balance` only has cent precision: use it to reconcile totals, not per answer.
  Measured: one English question 2 calls, ~3k in / 0.4k out = $0.0013 at peak.
- **"Which bill?"** (`resolve.is_vague_bill_question`): a FIRST question made only of filler words around bill/act/law ("tell me about
  the new bill") is answered with the 5 newest bills instead of searching (live bug: it matched a 2018 "New Villages" bill and claimed
  nothing newer exists). Inside a running chat it is not asked back (the condense step resolves "this bill"). Works for Sinhala too.
- **Literal translate-in** (`translate.LITERAL`, both `TO_EN` and `DETECT_AND_TRANSLATE`): the old wording "Use the English name of an Act
  or body when you know it" made the model swap "terrorism act" for the real-world "Prevention of Terrorism Act" (not in the corpus),
  and the answer then hedged about an Act the user never named (3 of 4 runs; 0 of 4 with the literal wording, correct 20-year
  penalty in all 8). 8/8 Sinhala-script and 8/8 romanised questions still retrieve the right bill.
- **Prompt echo guard** (`translate.echoes_prompt`, in `check_script`): an answer containing the glossary block header or 2+ "English
  term = term" lines is rejected like any unusable output (retry / fallback). Live bug: the translated path appended the glossary list.

### Switching production to DeepSeek (prepared 2026-11-06, NOT done: it needs the owner's go-ahead for each production step)
Code is merged-ready and defaults to Workers AI, so nothing changes until the steps below. `src/chatconfig.js` maps `CHAT_PROVIDER`:
`deepseek` + the `DEEPSEEK_API_KEY` secret -> container env `BILLRAG_CHAT_MODEL=deepseek-flash`, `BASE_URL=https://api.deepseek.com`,
`BILLRAG_GRADE_EFFORT=off`, no Llama translator (a `@cf/` id cannot run on DeepSeek's endpoint, so the chat model translates); a missing
secret falls back to Workers AI with a Worker-log warning. Embeddings, rerank and R2 keep using the Cloudflare credentials.
Verified locally with exactly that env (`python -m billrag serve` + POST /api/ask): English hostage question 9.6 s, $0.0005 (90% of input
cache hits); Sinhala 15.7 s, $0.0032, direct path, no warnings. Steps:
1. Upload the index with the metadata tables first (old code ignores them): `python scripts/upload_r2.py --bucket gov-rag --include-index`
   (needs `python -m billrag meta` run on that index; `ingest` and `fetch_index` copies already have it locally only).
2. `printf %s "$DEEPSEEK_API_KEY" | npx wrangler secret put DEEPSEEK_API_KEY` (stdin; never in `wrangler.toml`). Rotate the key if it ever
   appeared in a chat transcript.
3. Set `CHAT_PROVIDER = "deepseek"` in `wrangler.toml` and deploy (see "Deploying from the cloud sandbox"). Check the container can reach
   `api.deepseek.com` (Cloudflare Containers allow outbound internet by default; not testable from the sandbox).
4. Verify from the chat logs: `model: deepseek-flash`, `cost.usd`, `warnings` (see "Reading the chat logs"); `read_chatlogs.py` prints cost.
Rollback: `CHAT_PROVIDER = "workers-ai"` and redeploy (secret can stay). Risks to accept knowingly: an external provider outage takes
answers down (no automatic fallback to Workers AI at request time), prompts and bill text go to DeepSeek, cost is variable (see the
cost log), prices change (`BILLRAG_PRICES`). The judge for the eval numbers was DeepSeek-family, and Sinhala quality was spot-checked
(18 + 16 questions), not measured on a labelled set.

## Certified Acts next to bills (`billrag/acts.py`, 2026-11-07)

**Why:** a bill is not the law, and the text can change on the way. Bill 063/2026 §17 made offences "cognizable and non-bailable"; Act
25/2026 has no such clause, and Bill §7 (s.81) went too, so every later section number shifted. The index could not tell. Comparing all linked pairs: most
sections match, but many differ in wording (`decide` -> `determine`, whole qualifying phrases), so this is not a rare case.

**Stored as:** `bills` + `chunks` rows with kind `Act`, key `Act 25/2026`, title `X Act, No. 25 of 2026`, `date` = certified date from the cover
(differs from the listing date on 7 Acts). The key cannot be confused with bill 025/2026, and citations read `[Act 25/2026 §7]`. Unlike `local_sections`
and the glossary, this DOES change `chunks`, so `fingerprint()` changes and `embed` must run; ids of existing chunks do not change, so only the new text is
embedded. Scope: 2024+ (`acts.ACT_SINCE`), 99 of 101 Acts, about 4,300 chunks. Not indexed: `0/2026` (no number), `49/2024` (no English PDF). Sinhala text linked
with the same rules as bills: 36 of the 53 from 2025+ (17 visual-order font); 2024 Acts are FM Abhaya (legacy font), so not handled.

**Links (`act_links`):** decided from text, because titles recur and one title can have two Acts on one day (26/2026, 27/2026). A bill goes to the Act it
shares most word 3-grams with. `direct=1` = the last round before the Act; `direct=0` = an earlier round later re-presented, which must not be described as having
become the Act. Result: 89 of 99 Acts linked, 173 bills direct, 27 earlier rounds. The 10 unlinked are mostly 2024 incorporation Acts whose bills are far older or
reworded: their bills say "no Act held". Thresholds are at the top of `acts.py`.

**Comparison (`act_sections`):** `same | changed | dropped | moved | added`, by order-preserving alignment. Lessons, each from a real error:
- **A similarity threshold cannot decide "same":** "twenty" -> "ten" in a long clause scores 0.99. `material_differences` lists the word differences and only recognised noise
  (margin notes, split words, the bill's blank Act number) is ignored. It errs towards `changed`, which just shows both texts. Roughly 9 of 14 sampled `changed` rows were real edits; the rest was noise.
- **Amendment clauses read alike:** "Section 81 ... amended" and "Section 82 ... amended" match on boilerplate, so a different target section never pairs, and a clause whose target no Act section amends is `dropped` even if its words occur elsewhere.
- **`dropped` is a strong claim:** it requires the clause's text to be absent from the whole Act including schedules, else `moved`. A section-only check called a schedule's text dropped (567/2025 §12). 34 dropped, 30 moved. It says the WORDING is not in the Act, not that the Act is silent on the topic (382/2023 §3 is rewritten, not removed).
- Schedules are not compared (no section number).

**Answers (`Pipeline.apply_acts`, after grading, no model call; log field `acts`):** a `same` bill clause is replaced by the Act's section; `changed` shows the Act's then the bill's; `dropped`/`moved`/bill-level keep the bill's with the Act's overview beside it. A question naming a bill number or
comparing versions (`COMPARE`) keeps the bill's clause. Every passage gets a `STATUS:` line (`acts.status_lines`). What it may say is limited: no Act held is "not shown to have passed", never "failed"; a bill older than 2024 is "not checked". The Act's status names its bills ("has the bill become law?" hedged without it) and long title (a Sinhala answer took a "No. 19 of 1994" inside a clause for the Act being amended).
- `store.versions` leaves Acts out, and retrieval's `also_in` never pairs a bill with an Act: `also_in` means "same section number there", which is false here. Identical text keeps the Act's chunk.
- `"Act No. 25 of 2026"` restricts retrieval to that Act; a bare `25/2026` stays a bill number, as before. The principal Act of an amendment (mostly older than 2024) is not indexed, so we can say what an amendment does, not what the amended law now reads.
- Evals count the Act's matching section as gold too (`answer_eval.with_act_gold`).

**Measured** (72 recent items, deepseek-flash, thinking off for grading): retrieval MRR 0.87 -> 0.88, R@10 0.97 -> 0.96 (one item, noise). Answers: see the table row `acts` under "Priorities, DeepSeek". The eval's references come from bills, so correct Act answers can be marked wrong: of 4 misses, the judge failed the 1 July date (Act 10/2026 §3; bill 023/2026 §3 says 1 April) and the unenacted bail clause (063/2026 §17), both checked against the text and both right. Read misses before trusting a drop; re-reference items whose bill became an Act with `changed` or `dropped` status. `scripts/eval_acts.py`
(hand-written set, `evals/act_eval.jsonl`): 12/12 mechanical checks, but those are phrase matches: reading the answers is what found the three bugs above. Known weak spots: one run cited `Act 25/2026 §17` without that section in evidence (the citation check warned); and a pre-2024 bill can be called "not law" when it did become an Act we do not hold (319/2023 became the 2023 principal Act). Summaries: 9 Acts have none (the model returned unusable output three times; `summarize` retries them).

**Production (done 2026-11-06, see "Deployed vs main"). To upload a rebuilt index:** old code cannot read `[Act ...]` citations, while new code works on an old index, so deploy code first. A container restores the index whenever it starts, so no restart is needed: an awake instance picks it up after it next sleeps. `upload_r2.py --include-index` publishes the pair safely (`publish_index`): it refuses a pair whose fingerprints differ, stages both and checks them by SHA-256, backs up the live pair to `backups/<prefix>/<UTC time>/`, then swaps by server-side copy and puts things back if the swap fails. R2 has no multi-object transaction, so the live keys disagree for 3.5-6 s (measured) during the second copy; `fetch_index.py` checks the pair after downloading and fetches again if it overlapped a swap (this half needs an image deploy to take effect). Why: on 2026-11-06 the old script uploaded the two files independently and the index failed after the vectors succeeded, leaving a pair that gives `VectorsError` on any start for 16 minutes (nothing woke; the instance was asleep). Delete old backups by hand. `data/acts` is not in R2; re-scrape it (resumable).

## Gist questions: overview, title index, "which bill?" (written 2026-11-07, branch `claude/overview-questions`, NOT merged or deployed)

**Why:** live question "key provisions of Act No. 6 of 2026" got "partial account only": the Act was fully indexed, but no clause matches such a question, so
the grader kept scattered sections and never the Act's overview. `Pipeline.ask` now treats a question matching `OVERVIEW_Q` ("key provisions", "summarise",
"explain", "tell me about", ...) specially:
- **Named by number** (`NNN/YYYY`, `Act No. N of YYYY`): the overview (`_act_overview_evidence`) goes first and the evidence counts as enough (`added_overview`, event `overview_added`).
- **Named by title:** `titles.Ranker` (table `title_index`, built by `python -m billrag titles` and by `meta`; one row per bill family and per Act; side table like `bill_meta`, `chunks`/vectors untouched).
  Hand-weighted scorer: idf-weighted share of what the user SAID that is in the title (`cov_q`, must be ~1; filler like "key provisions" ignored; close spellings count), share of the title said (`cov_t`),
  word order, year, and "act"/"bill". Decision `one` -> retrieval is restricted to that family's keys and its overview added; `many` (several titles fit equally, 2-30 of them, and the user said bill/act)
  -> reply "Which did you mean?" with up to 5 newest candidates, before any search or model call (`Result.clarify`, nothing kept in history); `none` -> as before. An Act beats its own bill unless the user says "bill".
  Without a `title_index` table (older index) `Pipeline.titled_bills` does a cruder version after retrieval.
- **Measured** (`python -m billrag titles --eval`, 300-400 synthetic questions: full title, without bracketed words, a typo, with a year, lower case; gold = that title): right winner ~88%, right title among the asked candidates ~10%, confident wrong winner 1-1.5% (the
  wrong ones are mostly a shorter, equally valid title, or a typo that lands on another title), no decision <1%. Not measured: real users' wording, Sinhala titles/aliases (not in the index yet), the 72-item answer eval (no eval question both names a bill number and matches `OVERVIEW_Q`).
- **Gist eval** (`scripts/eval_gist.py`, gold `evals/gist_eval.jsonl`, 30 questions: 8 by number, 22 by full/partial title, a typo, 2 ambiguous; facts are word groups from the official long title, checked to occur in the instrument's own text). 2 runs each, deepseek-flash, 2026-11-07: main strict 47/60, branch 53/60; hedged ("evidence not enough") 30 -> 14; facts 0.93 -> 0.96; cites the target 58/60 both; citation warnings 2 -> 3 (sporadic, 6 reruns of the three cases showed none). Numbered questions 5/8 -> 7/8, titled 42/52 -> 46/52. The words in `must` are narrow ("establish": the Cricket answers say "create", so 4 branch rows fail on wording; a widened group gives 54/60 vs 47/60). Written after reading 12 earlier A/B answers, so not blind; one model, one judge-free mechanical score.
- **Production:** the index must be rebuilt and uploaded (`meta` or `titles`, then `upload_r2.py --include-index`) for the ranker; code alone falls back to `titled_bills`. `ingest` and `ingest-acts` rebuild it (so `refresh_bills.py` does too).

## Long sections: window around the match (`Pipeline.expand`, `Store.section_parts`, written 2026-11-07, branch `claude/chunk-window`, NOT merged or deployed)

The answering model gets at most `SECTION_CHARS` (3,000) characters of a section; 496 of 6,765 sections of 2025+ bills and Acts are longer, and the old cut kept only the opening, so a
match late in a long section was lost (live: Act 26/2025 §4 is 4,721 characters, its §4B on tax returns and withholding tax started near character 2,700 and the answer said it was cut off).
Now, if a matched chunk (any kept chunk of that section) ends past the first 3,000 characters, the section is shown as a window: the matched chunks plus neighbouring parts while the budget lasts, in reading order, with
"(... N more part(s) of section S are not shown ...)" where parts are left out. Otherwise the text is exactly what it was (first 3,000 characters, no notes).
**First try, a window for every long section, was dropped:** the gist eval fell (47 and 49 of 60 vs 50-53 for the code without it; more hedging and citation warnings; the notes may nudge the model, not tested) although the 72-item eval did not move (0.96 vs 0.94). Measured 2026-11-07, deepseek-flash, same index: late-match-only variant: gist 53/60 vs 50/60 for `main` run at the same time (warnings 3 vs 6; run-to-run spread is about 3), 72-item eval strict 0.96, cited gold 0.99 (main 0.94 / 0.99). Not done: following continuation or "section N" links between chunks (`chunk_links`), whole-section requests ("full text of section 4").

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
