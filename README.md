# llm_rag

## Cloudflare Worker with email OTP login

`src/auth.js` (entry: `src/worker.js`) puts an email one-time-code login in front of the site (static `public/` page, or the Python UI via `ORIGIN`).
Codes are sent through the Cloudflare Email Service REST API, so recipients need no verification in Cloudflare;
`ALLOWED_EMAILS` in `wrangler.toml` is the only allowlist.

1. Onboard your sender domain under Cloudflare Email -> Email Sending, and create an API token that can send email.
2. `npx wrangler kv namespace create AUTH` and paste the id into `wrangler.toml`; also set `CF_ACCOUNT_ID`, `ALLOWED_EMAILS`, `FROM_ADDRESS`.
3. `npx wrangler secret put OTP_PEPPER` (any long random string) and `npx wrangler secret put CF_EMAIL_TOKEN`.
4. `npx wrangler deploy`. Tests: `npm test`.

Codes are 6 digits, valid 5 minutes, single use, 5 attempts, one request per address per minute; sessions last 7 days.
