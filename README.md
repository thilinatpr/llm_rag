# llm_rag

## Cloudflare Worker with email OTP login

`src/auth.js` (entry: `src/worker.js`) puts an email one-time-code login in front of the site (static `public/` page, or the Python UI via `ORIGIN`).
Codes are sent with Cloudflare Email Routing's `send_email` binding, which can only deliver to **verified destination
addresses**, so only people on that list can sign in.

1. Enable Email Routing on your domain and add each user under Email Routing -> Destination addresses (they confirm by email).
2. `npx wrangler kv namespace create AUTH` and paste the id into `wrangler.toml`.
3. Set `ALLOWED_EMAILS` and `FROM_ADDRESS` in `wrangler.toml`; `npx wrangler secret put OTP_PEPPER` (any long random string).
4. `npx wrangler deploy`. Tests: `npm test`.

Codes are 6 digits, valid 5 minutes, single use, 5 attempts, one request per address per minute; sessions last 7 days.
