# Launch TODO

Plan for opening citizenlk.com services to the public, written 2026-11-07. General open work is in `TODO.md`.

Legend: **O** = needs the owner. Sizes are rough: **S** = hours, **M** = a day or two, **L** = several days.

## Packages (proposed)

Every package gets the same answers, citations and honesty checks: nobody pays for a more accurate answer.

| | Public | Free account | Paid: LKR 1,000/month |
|---|---|---|---|
| Sign-in | None | Email | Email |
| Bill Assistant | 3 questions/day | 20/day, up to 150/month | 50/day, up to 500/month |
| Procurement Rules | None | 5 questions/day | Included in the Bill Assistant limit |
| Chat history | Current chat only | Last 30 days | Unlimited, searchable |
| New-bill alerts | None | Weekly email | Pick topics, alerts as bills are published |
| Extras | None | None | PDF export, citation links to the PDF page |
| When busy | Shared lane, may wait | Normal queue | First in the queue |

Numbers behind this (live logs and estimates, 2026-11-06; the sample is small):
- Bill Assistant costs a median of $0.002 per question, highest seen $0.0047. Procurement Rules is about $0.0005 (eval rate, not logged live).
- At about LKR 300 to the dollar (assumption), LKR 1,000 is about $3.33, roughly $3.20 after a 3-4% payment fee (estimate). The most a paid user can cost in model calls is about $2.35/month.
- Running costs are about $13/month with both containers always awake, so about 5 paid users break even at typical use. Workers AI and R2 costs are not included.
- The public lane's maximum spend is set by `PUBLIC_DAILY_GLOBAL` (500/day: about $30-70/month), not the per-visitor limit.

## Phase 1: Make it safe to run (before any new users)

Written and tested 2026-11-07, not yet deployed. Details and measurements: `AGENTS.md`.

- [x] **S** Low-balance alert for DeepSeek (running out failed a real visitor's question on 2026-11-06, 20:38 UTC)
- [x] **M** Automatic fallback to Workers AI when DeepSeek fails
- [x] **S** Token and cost logging for Procurement Rules
- [x] **S** Check whether failed answers use up a visitor's allowance, and fix it if so. They did; fixed.
- [x] **M** Load test the same images at the same limits under Docker. It found a bug that failed 9 of 16 questions at 8 people at once; fixed. Procurement Rules now takes 2 at once plus a queue.
- [x] **M** One command to add newly published bills: `scripts/refresh_bills.py`
- [x] **S** Read the flagged Procurement answers and the failed follow-up rewrite (findings below)
- [x] **O** Deploy both services (live 2026-11-07 05:43 UTC)
- [ ] **O** Ask a question on each site and check the logs (no question has reached the new code yet)
- [x] **O** Publish the new index (2026-11-07 06:12 UTC; adds the presented version of Prevention of Organised Crimes, 087/2026)
- [x] Schedule the refresh: daily check in the admin panel, Build/Publish buttons that run `refresh_bills.py` on GitHub Actions. Written 2026-11-08, not deployed.
- [ ] **O** **Later, once real users are on the site:** turn the scheduled refresh on. Until then refresh by hand with `scripts/refresh_bills.py`. The code is merged (PR #85) but not deployed. Steps (details in `AGENTS.md` "Scheduled refresh"):
  - [ ] Write `bills/indexed-keys.json` to R2: `upload_r2.py --bucket gov-rag --manifest-only --data data/bills`, with the live index in `data/bills`
  - [ ] GitHub repo secrets: `R2_ACCOUNT_ID`, `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (own R2 token, Object Read & Write on `gov-rag`), `CLOUDFLARE_API_TOKEN` (own token, Workers AI only), `DEEPSEEK_API_KEY`
  - [ ] Fine-grained GitHub token (this repo only, Actions: read and write, with an expiry), then `wrangler secret put GITHUB_DISPATCH_TOKEN -c workers/admin/wrangler.toml`
  - [ ] Deploy the admin Worker (`npx wrangler deploy -c workers/admin/wrangler.toml`), press **Check now**, then **Build** once and read the run
- [ ] **O** Run `scripts/load_test.py` against the live site with a signed-in cookie (a small run: each question costs money)
- [ ] **O** Top up DeepSeek: under $5 when this was written

**Done when:** a simulated DeepSeek failure still gets users an answer (done locally), and the load test shows how many people at once the container can handle (about 8-14 at the same instant at the worst-case CPU; not yet confirmed on Cloudflare).

Findings from the flagged Procurement answers:
- `FR 107`: the regulation is in our sources, but the evidence list was full, so a pointer to it was dropped and the answer said it was missing. Fixed: pointers get their own room.
- `FR 704`: not in our sources, and the answer only repeated what FR 113 says about it. The warning was a false alarm that told readers to doubt a sound answer. Fixed.
- Failed rewrite: reasoning used up to 513 of the 600 tokens allowed, so a cut-off is the likely cause (not seen again in 4 tries). The fallback search then mixed two questions, and the answer drifted to money owed *to* the Government (FR 113 waivers) when the question was about money the Government owes. Rewrites now run with thinking off.
- A later answer quoted relief measures "valid until 30th September 2026" without saying that date has passed, because answers did not know today's date. Fixed: see `procurement/README.md` "Dates". Whether the circular still covers a given contract is left to the Treasury; an officer should check that wording in the Phase 2 review.

## Phase 2: Earn trust (before opening sign-up)

- [ ] **O** Have a Sinhala reader check the Financial Regulations translations (`procurement/npc_texts/FRA.md`, `PFC0120.md`)
- [ ] **O** Have Sinhala and Tamil speakers review the machine-translation note (service pages are English only since 2026-11-07, so nothing else on them needs it)
- [ ] **O** Get a procurement officer to review 20+ new questions, as the first real accuracy figure (`evals/procurement_eval.jsonl` is only a regression check)
- [ ] **M** Find NPC's Contract Management Guidelines and the works bidding documents, or say clearly in answers that they are missing
- [x] **M** Privacy page for each service at `/privacy`. Needs the owner: a contact address (`PRIVACY_CONTACT`) and the retention decision below, then its "how long" section changes.
- [ ] **S** Set a retention period for chat logs (they hold private questions, with no expiry today)
- [ ] **O** Get the terms and "not legal advice" wording checked, ideally by a lawyer
- [x] **O** Send login mail from citizenlk.com instead of `tripletrackr.com` (domain enabled 2026-11-07, sender `login@citizenlk.com`)

**Done when:** the officer's review finds no serious errors, or the ones it finds are fixed.

## Phase 3: Launch the free account

- [x] **M** Open sign-up with abuse protection (Turnstile, 20 codes per network and 5 per address a day). Written 2026-11-08, OFF (`OPEN_SIGNUP = "off"`), not deployed. Bill Assistant only; Procurement Rules stays staff-only until the owner decides
- [x] **M** One account across Bill Assistant and Procurement Rules: one session cookie for `.citizenlk.com`, one KV, one consent record. Written 2026-11-08, not deployed (see `AGENTS.md` "One account"). Still open: each service keeps its own `ALLOWED_EMAILS`, so Procurement Rules stays owner-only until sign-up is open
- [x] **S** Consent at first sign-in (accuracy, not legal advice, data sent to third-party providers outside Sri Lanka); Procurement Rules adds three of its own. Written 2026-11-08, not deployed. **O** Have the wording checked with the terms (Phase 2)
- [x] **M** Per-user limits: 20/day and 150/month for Bill Assistant, 5/day for Procurement Rules (counted on the server, per service). Written, not deployed
- [ ] **M** Save 30 days of chat history, with a way for users to delete it
- [ ] **M** Weekly new-bill email, with unsubscribe
- [x] **S** Don't count greetings, clarifications, translations of an earlier answer or failures against the limit (Bill Assistant; Procurement Rules does not send the marker yet, so there every question counts). Written, not deployed
- [ ] **S** Lower the public limit from 10 to 3 (`PUBLIC_DAILY_PER_COOKIE`): a config change to make together with `OPEN_SIGNUP = "on"`. The "create a free account for more" message is written and shows only while sign-up is open
- [ ] **S** Weekly report: sign-ups, active users, people hitting limits, cost per user
- [ ] **O** To turn sign-up on (in this order): deploy; send a test code to an address NOT in `ALLOWED_EMAILS` after setting `OPEN_SIGNUP = "on"` and `PUBLIC_DAILY_PER_COOKIE = "3"`; check the Turnstile widget on the sign-in page (its hostname list must include `bills.citizenlk.com`, it does); redeploy; read the first free account's logs
- [ ] **O** Soft launch to about 20-50 people first, then announce it

**Done when:** a month of usage shows how often people hit limits, how many sign up after hitting one, and what each user costs. If many hit the public limit and few sign up, go back to 5.

## Phase 4: Test paid, then build it

- [ ] **O** Offer LKR 1,000/month to 5-10 procurement officers by hand, with bank transfer and a manual upgrade
- [ ] **S** Plan field and paid limits (50/day, 500/month), plus first place in the queue
- [ ] **M** Citation links to the PDF page, and PDF export
- [ ] **M** Topic alerts and searchable history
- [ ] **O** Choose a payment provider (such as PayHere); check its fees and whether it requires a registered business
- [ ] **O** Get advice on tax and refund terms
- [ ] **L** Build payments and renewals, only once 5 or more people are paying by hand

## Decisions needed from the owner

- [ ] **Who is it for first?** Assumed here: procurement officers pay, citizens use Bill Assistant free.
- [ ] **Open Procurement Rules to everyone with an account?** Depends on the translation review in Phase 2.
- [ ] **Who handles the money?** Personally or through a registered business; this decides which payment providers are available.

## Timing risk

Phase 2 depends on finding reviewers, not on code, so start looking for them now, alongside Phase 1.
