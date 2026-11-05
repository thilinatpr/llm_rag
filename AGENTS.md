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
billrag/parse_si.py       official Sinhala text per section (Unicode PDFs, 2025+) -> table local_sections; `ingest-si`
billrag/agent.py          (`--mode agent`) tool-calling loop: search_bills, list_bills, get_bill_overview, get_section, diff_bills
billrag/llm.py            Anthropic + OpenAI-compatible chat adapters, OpenAI/sentence-transformers embedders
billrag/enrich.py         optional: dense vectors (`embed`) and LLM bill summaries (`summarize`)
billrag/config.py         all settings come from BILLRAG_* env vars (documented at the top of the file)
billrag/resolve.py        question grounding before retrieval: greetings, abbreviation (PTSA) -> bill title matching, "closest bills"
billrag/chatlog.py        one JSON record per chat turn -> R2 `chatlogs/` + local JSONL; `build_id()` = hash of the running code
billrag/web.py            `python -m billrag serve`: stdlib HTTP API (/api/ask, /api/search, /healthz); secret-gated in production
public/index.html         the chat page (static asset, served by the Worker behind the login; also used by local `serve`)
src/worker.js             Worker entrypoint: login gate (auth.js) + forwards /api/* and /healthz to the container
src/auth.js               email one-time-code login (KV codes/sessions, Cloudflare Email Service REST sender), routing
src/container.js          `BillragContainer` (Durable Object): starts the Python container, passes env/secrets to it
Dockerfile, docker/entrypoint.sh, requirements-serve.txt   the container image (restores the index from R2, then serves)
wrangler.toml             Worker + assets + KV + container + Durable Object config; test/worker.test.mjs = Node tests (`npm test`)
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
python -m pytest tests -q                                           # must pass before every commit
```

## Production: architecture, Cloudflare, deploying, logs

**Live site:** https://gov-rag-llm.patprathnayaka.workers.dev (login required; only addresses in `ALLOWED_EMAILS` can sign in).

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
| Email | zone `tripletrackr.com`; sender `login@tripletrackr.com` via the Email Service REST API (`/accounts/<id>/email/sending/send`) - no per-recipient verification in Cloudflare; `ALLOWED_EMAILS` is the only allowlist |

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
**Rollback:** check out the previous deployed commit and repeat steps 1-5; the old image digest stays in
`registry.cloudflare.com`, and `wrangler deployments list` shows earlier Worker versions.

### Reading the chat logs
Every question the live app answers is logged (best-effort, off the request path) to `r2://gov-rag/chatlogs/YYYY-MM-DD/<HHMMSSfff>-<session>-<turn>.json`
(`BILLRAG_LOG_PREFIX`, `BILLRAG_LOG_DIR`; `BILLRAG_CHATLOG=0` turns it off). A record has: `ts`, `build`, `session`, `turn`, `type`
(`ask`/`search`), `question`, `answer`, `error`, `seconds`, `mode`, `model`, `queries` (what retrieval actually searched),
`enough` (did the grader think the evidence sufficed), `evidence` (bill, section, grade 0-3), `candidates_graded`, `warnings`
(unsupported citations, unusable model output), `llm_calls`, `step_seconds` and `events` (resolve/retrieve/evaluate with the grader's
"missing" text).
```bash
env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY python scripts/read_chatlogs.py [--since 2026-11-05] [--out logs.jsonl] [--show 20]
```
It prints totals, median/p90 seconds and lists: errors, "found nothing", "evidence judged insufficient", slowest. Read the raw
`--out` file for detail. Things to look for: answers starting `analysis:` or a run of `!` (model glitch; should now be retried),
a `queries` list that contains guessed bill names (grounding failure), `evidence` from the wrong bill, `enough: false` with
long answers, `build` not matching the commit you think is live, and anything with `error`. Logs contain users' questions: handle
them as private. Records before the first logging deploy do not exist.
Worker-side errors (`upstream_failed`, with path, ms, error) go to Workers Logs (dashboard: Workers & Pages -> gov-rag-llm -> Logs;
`[observability] enabled = true`), and container logs are enabled too.

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

### Small-memory hosting facts
Index: 42,696 chunks x 768 dims; `index.sqlite` 201 MB, `vectors.npz` 61 MB as float16 (114 MB float32). Server RSS ~124 MB after a
dense search; ~100 MB right after warm-up; one shared `Retriever` serves all conversations. `shrink_vectors.py` converts a float32 file;
`embed_all(..., dtype="float16")` writes it directly. Top-20 overlap with float32 was 99.8% on 400 test queries (top-1 identical).

## Deployed vs main (keep this honest)
"Merged" is not "live". Live since 2026-11-05 16:47 UTC: build `af86776e` = `main` at `fca8f3a` (merge of PR #14: translation
layer, `TRANSLATE = "ai"`, `TRANSLATE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"`), Worker version `e56e31b8`, container
app version 9 (image `sha256:f647ba62...`). Previous: build `4cfb4b63` (commit `a2ad066`), container v8 (`sha256:38b7eaea...`);
before that build `3bbe2bc6`, v7. Anything merged to `main` later is NOT live until someone runs `wrangler deploy`. Check
`git log origin/main` against the `build` seen in the latest chat log before telling the owner a change is live.

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
  Abhaya legacy font) and all Tamil PDFs are not handled yet.
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
