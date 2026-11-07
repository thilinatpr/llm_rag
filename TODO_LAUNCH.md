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

- [ ] **S** Low-balance alert for DeepSeek (running out failed a real visitor's question on 2026-11-06, 20:38 UTC)
- [ ] **M** Automatic fallback to Workers AI when DeepSeek fails
- [ ] **S** Token and cost logging for Procurement Rules
- [ ] **S** Check whether failed answers use up a visitor's allowance, and fix it if so
- [ ] **M** Load test the real container (concurrency has offline tests only; Procurement Rules still answers one question at a time)
- [ ] **M** One command to add newly published bills (scrape, ingest, summarize, embed, upload)
- [ ] **S** Read the flagged Procurement answers (`FR 107`, `FR 704`) and the failed follow-up rewrite in `procurement-logs/`

**Done when:** a simulated DeepSeek failure still gets users an answer, and the load test shows how many people at once the container can handle.

## Phase 2: Earn trust (before opening sign-up)

- [ ] **O** Have a Sinhala reader check the Financial Regulations translations (`procurement/npc_texts/FRA.md`, `PFC0120.md`)
- [ ] **O** Have Sinhala and Tamil speakers review the interface messages and the machine-translation note
- [ ] **O** Get a procurement officer to review 20+ new questions, as the first real accuracy figure (`evals/procurement_eval.jsonl` is only a regression check)
- [ ] **M** Find NPC's Contract Management Guidelines and the works bidding documents, or say clearly in answers that they are missing
- [ ] **M** Privacy page: what is logged, how long it is kept, and that questions go to DeepSeek
- [ ] **S** Set a retention period for chat logs (they hold private questions, with no expiry today)
- [ ] **O** Get the terms and "not legal advice" wording checked, ideally by a lawyer
- [ ] **O** Send login mail from citizenlk.com instead of `tripletrackr.com` (needs dashboard access or a wider token)

**Done when:** the officer's review finds no serious errors, or the ones it finds are fixed.

## Phase 3: Launch the free account

- [ ] **M** Open sign-up in place of the `ALLOWED_EMAILS` list, with abuse protection (Turnstile on sign-up, limits per address)
- [ ] **M** One account across Bill Assistant and Procurement Rules (sign-ins are separate today)
- [ ] **M** Per-user limits: 20/day and 150/month for Bill Assistant, 5/day for Procurement Rules
- [ ] **M** Save 30 days of chat history, with a way for users to delete it
- [ ] **M** Weekly new-bill email, with unsubscribe
- [ ] **S** Don't count greetings, clarifications, translations of an earlier answer or failures against the limit
- [ ] **S** Lower the public limit from 10 to 3 (`PUBLIC_DAILY_PER_COOKIE`) and show "sign up for more" when it runs out. Not before sign-up exists: until then "or sign in" is a dead end for visitors.
- [ ] **S** Weekly report: sign-ups, active users, people hitting limits, cost per user
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
