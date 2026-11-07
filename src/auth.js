// Email-OTP login in front of the app, using Cloudflare Email Routing's send_email binding.
//
// Flow: POST /login/request {email} -> 6-digit code emailed (only if the address is in ALLOWED_EMAILS)
//       POST /login/verify {email, code} -> session cookie
//       everything else requires the cookie; it is proxied to ORIGIN if set, else served from static assets.
//
// Bindings (see wrangler.toml): AUTH (KV), ASSETS, vars ALLOWED_EMAILS / FROM_ADDRESS / CF_ACCOUNT_ID / ORIGIN.
// Secrets: OTP_PEPPER (random string) and CF_EMAIL_TOKEN (Cloudflare API token allowed to send email).
// Mail goes out through the Cloudflare Email Service REST API, so recipients need no Cloudflare-side verification;
// ALLOWED_EMAILS is the only allowlist.

import { getCookie, hex, json, readJson, safeEqual } from "./util.js";
import { anonApi, publicMe, publicOn, publicStart, signupOn, verifyTurnstile } from "./public.js";
import { countEvents, counted } from "./events.js";
import { historyApi } from "./history.js";
import { memberAsk, memberLeft, takeCodeRequest } from "./members.js";
import { privacyPage } from "./privacy.js";
import { consentPage, hasConsent, saveConsent } from "./consent.js";

const PAUSE_KEY = "admin:public-paused"; // set by the admin panel (admin.js, citizenlk-admin Worker)
const OTP_TTL = 300; // seconds a code is valid
const RESEND_COOLDOWN = 60; // seconds between code requests per address
const MAX_ATTEMPTS = 5;
const SESSION_TTL = 7 * 24 * 3600;
const COOKIE = "session"; // default; the services that share one account pass `cookie` to createHandler

const enc = new TextEncoder();

export const normalizeEmail = (e) => String(e || "").trim().toLowerCase();
export const isEmail = (e) => /^[^\s@]{1,64}@[^\s@]{1,255}$/.test(e) && !/[\r\n<>]/.test(e);

export function allowed(env, email) {
  return String(env.ALLOWED_EMAILS || "").split(",").map(normalizeEmail).filter(Boolean).includes(email);
}

// May this address sign in here? Staff (ALLOWED_EMAILS) always; anyone when open sign-up is on; and with OPEN_TO = "registered" any address that already
// has an account, i.e. has accepted the terms on a service that is open (consent record in the shared KV). That is how Procurement Rules is open to
// registered users without taking sign-ups of its own: nobody new can get in from here, so it needs no person-check.
async function mayJoin(env, email) {
  if (allowed(env, email) || signupOn(env)) return true;
  return env.OPEN_TO === "registered" && !!(await env.AUTH.get(`consent:${email}`));
}

export function newCode() {
  const n = new Uint32Array(1);
  // rejection sampling to avoid modulo bias
  do crypto.getRandomValues(n); while (n[0] >= 4294000000);
  return String(n[0] % 1000000).padStart(6, "0");
}

async function digest(env, email, code) {
  return hex(await crypto.subtle.digest("SHA-256", enc.encode(`${env.OTP_PEPPER}|${email}|${code}`)));
}

export function buildMessage(from, to, code) {
  const mins = OTP_TTL / 60;
  return {
    from,
    to,
    subject: `Your sign-in code: ${code}`,
    text: `Your sign-in code is ${code}\n\nIt expires in ${mins} minutes. If you did not request it, ignore this email.`,
    html: `<p style="font:16px sans-serif">Your sign-in code is</p><p style="font:600 32px monospace;letter-spacing:6px">${code}</p><p style="font:13px sans-serif;color:#666">It expires in ${mins} minutes. If you did not request it, ignore this email.</p>`,
  };
}

export const sendMailViaCloudflare = (env, to, code) => sendMessage(env, buildMessage(env.FROM_ADDRESS, to, code));

export async function sendMessage(env, message) {
  if (!env.CF_ACCOUNT_ID || !env.CF_EMAIL_TOKEN || !env.FROM_ADDRESS) throw new Error("email sending is not configured");
  const url = `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/email/sending/send`;
  for (let attempt = 0; ; attempt++) {
    const res = await globalThis.fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.CF_EMAIL_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify(message),
    });
    if (res.ok) return;
    if (res.status === 429 && attempt < 2) {
      await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
      continue;
    }
    throw new Error(`Cloudflare email send failed: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
}

// COOKIE_DOMAIN (Worker var, "citizenlk.com") makes the cookie valid on every subdomain: that is how one sign-in works on all services.
// Without it the cookie is host-only (the admin panel, local runs). Set and cleared with the same Domain, or the browser keeps the old one.
const sessionCookie = (env, name, value, maxAge) =>
  `${name}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}${env.COOKIE_DOMAIN ? `; Domain=${env.COOKIE_DOMAIN}` : ""}`;

async function requestCode(env, request, sendMail, verify, nowMs) {
  const body = await readJson(request);
  const email = normalizeEmail(body.email);
  if (!isEmail(email)) return json({ error: "Enter a valid email address." }, 400);
  // Same response whether or not the address is allowed, so the form can't be used to probe the allowlist.
  const reply = json({ ok: true, message: "If that address is allowed, a code is on its way." });
  if (!(await mayJoin(env, email))) return reply;
  if (signupOn(env)) {
    // Open sign-up: a person-check first, then per-network and per-address daily caps. The same for staff, so the replies never tell the lists apart.
    const ip = request.headers.get("CF-Connecting-IP") || "";
    if (!(await verify(env, body.token, ip, new URL(request.url).hostname))) return json({ error: "The check failed. Reload the page and try again." }, 403);
    if (!(await takeCodeRequest(env, ip, email, nowMs)).ok) return json({ error: "Too many sign-in codes requested. Please try again tomorrow." }, 429);
  }
  if (await env.AUTH.get(`cool:${email}`)) return reply;
  await env.AUTH.put(`cool:${email}`, "1", { expirationTtl: RESEND_COOLDOWN });
  const code = newCode();
  await env.AUTH.put(`otp:${email}`, JSON.stringify({ h: await digest(env, email, code), n: 0 }), { expirationTtl: OTP_TTL });
  try {
    await sendMail(env, email, code);
  } catch (e) {
    await env.AUTH.delete(`otp:${email}`);
    await env.AUTH.delete(`cool:${email}`);
    console.error("send failed", e && e.message);
    return json({ error: "Could not send the code. Try again shortly." }, 502);
  }
  return reply;
}

async function verifyCode(env, request, name) {
  const body = await readJson(request);
  const email = normalizeEmail(body.email);
  const code = String(body.code || "").trim();
  const bad = () => json({ error: "Invalid or expired code." }, 401);
  if (!isEmail(email) || !/^\d{6}$/.test(code) || !(await mayJoin(env, email))) return bad();
  const raw = await env.AUTH.get(`otp:${email}`);
  if (!raw) return bad();
  const rec = JSON.parse(raw);
  if (rec.n >= MAX_ATTEMPTS) {
    await env.AUTH.delete(`otp:${email}`);
    return bad();
  }
  if (!safeEqual(rec.h, await digest(env, email, code))) {
    await env.AUTH.put(`otp:${email}`, JSON.stringify({ ...rec, n: rec.n + 1 }), { expirationTtl: OTP_TTL });
    return bad();
  }
  await env.AUTH.delete(`otp:${email}`); // single use
  const sid = hex(crypto.getRandomValues(new Uint8Array(32)));
  await env.AUTH.put(`session:${sid}`, email, { expirationTtl: SESSION_TTL });
  return json({ ok: true }, 200, { "Set-Cookie": sessionCookie(env, name, sid, SESSION_TTL) });
}

async function currentUser(env, request, name) {
  const sid = getCookie(request, name);
  if (!sid || !/^[0-9a-f]{64}$/.test(sid)) return null;
  const email = await env.AUTH.get(`session:${sid}`);
  return email && (await mayJoin(env, email)) ? email : null;
}

export const LOGIN_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Sign in - citizenlk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Abhaya+Libre:wght@800&family=Yaldevi:wght@400;500&family=Catamaran:wght@500&display=swap">
<style>
  /* citizenlk look: see DESIGN.md */
  :root { color-scheme: light dark;
    --lime: #F4F2EE; --ink: #1C1511; --muted: #6E6157; --line: #DAD3CA; --box: #FFFFFF; --sindoor: #A3241B; --ochre: #E0A93A; --err: #B3261E;
    --f-display: "Abhaya Libre", "Noto Serif Sinhala", Georgia, serif;
    --f-body: "Yaldevi", "Noto Sans Sinhala", "Segoe UI", system-ui, sans-serif;
    --f-ta: "Catamaran", "Noto Sans Tamil", sans-serif; }
  @media (prefers-color-scheme: dark) { :root {
    --lime: #15100D; --ink: #F1E6D6; --muted: #B3A391; --line: #3A2D24; --box: #211913; --sindoor: #E0675A; --ochre: #E8B550; --err: #FF8A80; } }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: var(--lime); color: var(--ink); font: 400 1rem/1.6 var(--f-body); display: flex; flex-direction: column; }
  :focus-visible { outline: 2px solid var(--ochre); outline-offset: 3px; border-radius: 4px; }
  .petals { height: 14px; flex: none; background: radial-gradient(circle at 50% 0, var(--sindoor) 0 7px, transparent 7.5px) 0 0 / 20px 14px repeat-x; }
  main { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; gap: 22px;
    padding: 48px max(16px, env(safe-area-inset-right, 0px)) 48px max(16px, env(safe-area-inset-left, 0px)); }
  h1 { margin: 0; font: 800 clamp(3rem, 10vw, 5rem)/1 var(--f-display); letter-spacing: -.01em; }
  h1 b { color: var(--sindoor); font-weight: 800; }
  .sub { margin: -8px 0 0; color: var(--muted); }
  form { width: min(100%, 560px); display: flex; align-items: center; gap: 10px; padding: 6px 6px 6px 20px; border: 1px solid var(--line);
    border-radius: 999px; background: var(--box); transition: border-color .2s, box-shadow .2s; }
  form:hover, form:focus-within { border-color: var(--ochre); box-shadow: 0 2px 10px -2px color-mix(in srgb, var(--ink) 18%, transparent); }
  form[hidden] { display: none; }
  input { flex: 1; min-width: 0; border: 0; background: transparent; padding: 9px 0; font: inherit; color: inherit; outline: none; }
  input:focus-visible { outline: none; }
  input::placeholder { color: var(--muted); }
  #code { letter-spacing: .3em; }
  #code::placeholder { letter-spacing: normal; }
  button { flex: none; border: 0; border-radius: 999px; padding: 9px 18px; background: var(--ink); color: var(--lime); font: inherit; font-weight: 500; cursor: pointer; }
  button:hover { background: var(--sindoor); color: #fff; }
  #msg { min-height: 1.6em; margin: 0; font-size: .92rem; color: var(--muted); max-width: 34em; }
  #msg.err { color: var(--err); }
  footer { flex: none; border-top: 1px solid var(--line); font-size: .84rem; color: var(--muted); display: flex; justify-content: space-between; gap: 8px 24px; flex-wrap: wrap;
    padding: 16px max(16px, env(safe-area-inset-right, 0px)) 16px max(16px, env(safe-area-inset-left, 0px)); }
</style></head>
<body>
<div class="petals" aria-hidden="true"></div>
<main>
  <h1>citizen<b>lk</b></h1>
  <p class="sub">Sign in to the Bill Assistant</p>
  <form id="f1"><input id="email" type="email" placeholder="you@example.com" aria-label="Email address" autocomplete="email" required autofocus>
    <button>Email me a code</button></form>
  <form id="f2" hidden><input id="code" inputmode="numeric" pattern="\\d{6}" maxlength="6" placeholder="6-digit code" aria-label="Sign-in code" autocomplete="one-time-code" required>
    <button>Sign in</button></form>
  <p id="msg" aria-live="polite">Access is by invitation for now.</p>
</main>
<footer><span>Sri Lanka</span><span>Not legal advice · Not a government service · <a href="/privacy" style="color:inherit">Privacy</a></span></footer>
<script>
const $ = (id) => document.getElementById(id), msg = (t, e) => { $('msg').textContent = t; $('msg').className = e ? 'err' : ''; };
async function post(path, body) {
  const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j;
}
$('f1').onsubmit = async (e) => { e.preventDefault();
  try { const j = await post('/login/request', { email: $('email').value }); msg(j.message); $('f2').hidden = false; $('code').focus(); }
  catch (x) { msg(x.message, true); } };
$('f2').onsubmit = async (e) => { e.preventDefault();
  try { await post('/login/verify', { email: $('email').value, code: $('code').value }); location.href = '/'; }
  catch (x) { msg(x.message, true); } };
</script></body></html>`;

// With open sign-up the sign-in page also creates accounts: a Turnstile widget above the buttons, its token sent with the code request.
const attr = (v) => String(v).replace(/[^A-Za-z0-9_-]/g, "");
export function openLoginPage(page, sitekey) {
  return page
    .replace('<p id="msg" aria-live="polite">Access is by invitation for now.</p>', `<div class="cf-turnstile" data-sitekey="${attr(sitekey)}"></div>\n  <p id="msg" aria-live="polite">Enter your email to sign in or create a free account.</p>`)
    .replace("</head>", '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script></head>')
    .replace("post('/login/request', { email: $('email').value })", "post('/login/request', { email: $('email').value, token: (document.querySelector('[name=cf-turnstile-response]') || {}).value })");
}

// upstream(request, env): optional, forwards signed-in requests (e.g. to a container). Used when env.BILLRAG is bound.
// verify(env, token, ip, host) checks a Turnstile token; now() is the clock (both replaceable in tests).
// app(request, env, user): handles everything but the login routes, with user = null when signed out (the admin Worker's panel).
// loginTitle replaces the line under the wordmark on the sign-in page.
// cookie: the session cookie's name (services on one account share one name and one AUTH namespace). consent: {service, name} asks every signed-in
// account to accept consent.js's statements before anything but sign-in, sign-out and the privacy page (null = no consent step: the admin panel).
export function createHandler({ sendMail = sendMailViaCloudflare, upstream = null, verify = verifyTurnstile, now = () => Date.now(), app = null, privacy = null, loginTitle = null, cookie = COOKIE, consent = null } = {}) {
  return async function fetch(request, realEnv) {
    let env = realEnv;
    const url = new URL(request.url);
    const { pathname } = url;
    // Anonymous visitor counts (events.js): the beacon endpoint is open to anyone, and the page gets its counter wherever it is served from.
    if (pathname === "/e" && realEnv.EVENTS) return countEvents(request, realEnv);
    if (realEnv.EVENTS && realEnv.ASSETS) env = { ...realEnv, ASSETS: { fetch: async (req) => counted(await realEnv.ASSETS.fetch(req), req, realEnv) } };
    if (pathname === "/login" && request.method === "GET") {
      let page = loginTitle ? LOGIN_PAGE.replace("Sign in to the Bill Assistant", loginTitle) : LOGIN_PAGE;
      if (signupOn(env)) page = openLoginPage(page, env.TURNSTILE_SITEKEY);
      else if (env.OPEN_TO === "registered")
        page = page.replace("Access is by invitation for now.", 'Sign in with your citizenlk account. No account yet? <a href="https://bills.citizenlk.com/login" style="color:inherit">Create a free one</a> first.');
      return new Response(page, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    }
    if (privacy && pathname === "/privacy" && request.method === "GET") // readable before signing in or starting a chat
      return new Response(privacyPage(privacy, env.PRIVACY_CONTACT), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    if (pathname === "/login/request" && request.method === "POST") return requestCode(env, request, sendMail, verify, now());
    if (pathname === "/login/verify" && request.method === "POST") return verifyCode(env, request, cookie);
    if (pathname === "/logout" && request.method === "POST") {
      const sid = getCookie(request, cookie);
      if (sid && /^[0-9a-f]{64}$/.test(sid)) await env.AUTH.delete(`session:${sid}`);
      return json({ ok: true }, 200, { "Set-Cookie": sessionCookie(env, cookie, "", 0) });
    }

    const user = await currentUser(env, request, cookie);
    if (app) return app(request, env, user);
    if (consent && pathname === "/consent") {
      if (!user) return Response.redirect(new URL("/login", url).toString(), 302);
      if (request.method === "GET") return new Response(consentPage(consent.service, consent.name), { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
      if (request.method === "POST") {
        const ok = await saveConsent(env, user, consent.service, (await readJson(request)).ids, now());
        return ok ? json({ ok: true }) : json({ error: "Every statement must be accepted." }, 400);
      }
    }
    // Signed in but has not accepted the current statements (new account, wording changed, or this service asks for more): nothing is answered until they do.
    if (consent && user && !(await hasConsent(env, user, consent.service))) {
      if (request.method === "GET" && (request.headers.get("Accept") || "").includes("text/html")) return Response.redirect(new URL("/consent", url).toString(), 302);
      return json({ error: "Please accept the terms first.", consent_required: true }, 403);
    }
    // The panel's pause button: a visitor is treated as if PUBLIC_CHAT were off. Cached a minute, so this is not a KV call per request.
    if (!user && env.PUBLIC_CHAT === "on" && (await env.AUTH.get(PAUSE_KEY, { cacheTtl: 60 }))) env = { ...env, PUBLIC_CHAT: "off" };
    // Public (anonymous) lane: see public.js. Off unless PUBLIC_CHAT = "on" and everything it needs is configured.
    // Saved chats of a signed-in account (history.js). Only with the consent step on, i.e. the services that share an account.
    if (consent && (pathname === "/history" || pathname.startsWith("/history/"))) {
      if (!user) return json({ error: "Not signed in." }, 401);
      return historyApi(env, request, user, consent.service, now());
    }
    const free = !!(user && consent && !allowed(env, user)); // a free account (open sign-up), not staff: counted per day and month (members.js)
    if (pathname === "/public/me" && request.method === "GET") {
      if (free && env.QUOTA) return json({ enabled: publicOn(env), user: "member", free: true, left: await memberLeft(env, consent.service, user, now()) }, 200, { "Cache-Control": "no-store" });
      return publicMe(env, request, !!user, now());
    }
    if (pathname === "/public/start" && request.method === "POST")
      return user ? json({ ok: true, user: "member" }) : publicStart(env, request, verify, now());
    if (!user) {
      if (upstream && publicOn(env)) {
        if (pathname === "/api/ask" || pathname === "/api/ask/stream" || pathname === "/healthz")
          return anonApi(env, request, pathname, upstream, now());
        if (request.method === "GET" && !pathname.startsWith("/api/")) return env.ASSETS.fetch(request);
      }
      if (request.method === "GET" && (request.headers.get("Accept") || "").includes("text/html"))
        return Response.redirect(new URL("/login", url).toString(), 302);
      return json({ error: "Not signed in." }, 401);
    }

    if (free && upstream && env.BILLRAG && request.method === "POST" && (pathname === "/api/ask" || pathname === "/api/ask/stream")) {
      if (!env.PROXY_SECRET) return json({ error: "Server is not configured." }, 500);
      return memberAsk(env, request, user, consent.service, (req, e) => upstream(req, e), now());
    }
    if (upstream && env.BILLRAG) {
      // The page is static (instant, even if the container sleeps); only the API and the health probe need the container.
      if (pathname.startsWith("/api/") || pathname === "/healthz") {
        if (!env.PROXY_SECRET) return json({ error: "Server is not configured." }, 500);
        return upstream(request, env);
      }
      return env.ASSETS.fetch(request);
    }
    if (env.ORIGIN) {
      // Forward to the app server without leaking our session cookie to it.
      const headers = new Headers(request.headers);
      headers.delete("Cookie");
      return fetch(new Request(new URL(pathname + url.search, env.ORIGIN), { method: request.method, headers, body: request.body }));
    }
    return env.ASSETS.fetch(request);
  };
}
