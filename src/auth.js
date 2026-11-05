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

const OTP_TTL = 300; // seconds a code is valid
const RESEND_COOLDOWN = 60; // seconds between code requests per address
const MAX_ATTEMPTS = 5;
const SESSION_TTL = 7 * 24 * 3600;
const COOKIE = "session";

const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

export const normalizeEmail = (e) => String(e || "").trim().toLowerCase();
export const isEmail = (e) => /^[^\s@]{1,64}@[^\s@]{1,255}$/.test(e) && !/[\r\n<>]/.test(e);

export function allowed(env, email) {
  return String(env.ALLOWED_EMAILS || "").split(",").map(normalizeEmail).filter(Boolean).includes(email);
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

function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
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

export async function sendMailViaCloudflare(env, to, code) {
  if (!env.CF_ACCOUNT_ID || !env.CF_EMAIL_TOKEN || !env.FROM_ADDRESS) throw new Error("email sending is not configured");
  const url = `https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/email/sending/send`;
  for (let attempt = 0; ; attempt++) {
    const res = await globalThis.fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${env.CF_EMAIL_TOKEN}`, "Content-Type": "application/json" },
      body: JSON.stringify(buildMessage(env.FROM_ADDRESS, to, code)),
    });
    if (res.ok) return;
    if (res.status === 429 && attempt < 2) {
      await new Promise((r) => setTimeout(r, 1000 * (attempt + 1)));
      continue;
    }
    throw new Error(`Cloudflare email send failed: HTTP ${res.status} ${(await res.text()).slice(0, 200)}`);
  }
}

const json = (obj, status = 200, headers = {}) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...headers } });

function getCookie(request, name) {
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === name) return v.join("=");
  }
  return null;
}

const sessionCookie = (value, maxAge) =>
  `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

async function requestCode(env, request, sendMail) {
  const email = normalizeEmail((await readJson(request)).email);
  if (!isEmail(email)) return json({ error: "Enter a valid email address." }, 400);
  // Same response whether or not the address is allowed, so the form can't be used to probe the allowlist.
  const reply = json({ ok: true, message: "If that address is allowed, a code is on its way." });
  if (!allowed(env, email)) return reply;
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

async function verifyCode(env, request) {
  const body = await readJson(request);
  const email = normalizeEmail(body.email);
  const code = String(body.code || "").trim();
  const bad = () => json({ error: "Invalid or expired code." }, 401);
  if (!isEmail(email) || !/^\d{6}$/.test(code) || !allowed(env, email)) return bad();
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
  return json({ ok: true }, 200, { "Set-Cookie": sessionCookie(sid, SESSION_TTL) });
}

async function currentUser(env, request) {
  const sid = getCookie(request, COOKIE);
  if (!sid || !/^[0-9a-f]{64}$/.test(sid)) return null;
  const email = await env.AUTH.get(`session:${sid}`);
  return email && allowed(env, email) ? email : null;
}

export const LOGIN_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; display: grid; place-items: center; min-height: 100vh; margin: 0; padding: 16px; }
  main { width: 100%; max-width: 22rem; }
  h1 { font-size: 1.25rem; }
  input, button { width: 100%; padding: 10px 12px; font: inherit; margin-top: 8px; box-sizing: border-box; }
  button { cursor: pointer; }
  #msg { min-height: 1.5em; margin-top: 12px; }
  .err { color: #d33; }
</style></head>
<body><main>
  <h1>Sign in</h1>
  <form id="f1"><input id="email" type="email" placeholder="you@example.com" autocomplete="email" required autofocus>
    <button>Email me a code</button></form>
  <form id="f2" hidden><input id="code" inputmode="numeric" pattern="\\d{6}" maxlength="6" placeholder="6-digit code" autocomplete="one-time-code" required>
    <button>Verify</button></form>
  <div id="msg"></div>
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
</script></main></body></html>`;

// upstream(request, env): optional, forwards signed-in requests (e.g. to a container). Used when env.BILLRAG is bound.
export function createHandler({ sendMail = sendMailViaCloudflare, upstream = null } = {}) {
  return async function fetch(request, env) {
    const url = new URL(request.url);
    const { pathname } = url;
    if (pathname === "/login" && request.method === "GET")
      return new Response(LOGIN_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
    if (pathname === "/login/request" && request.method === "POST") return requestCode(env, request, sendMail);
    if (pathname === "/login/verify" && request.method === "POST") return verifyCode(env, request);
    if (pathname === "/logout" && request.method === "POST") {
      const sid = getCookie(request, COOKIE);
      if (sid && /^[0-9a-f]{64}$/.test(sid)) await env.AUTH.delete(`session:${sid}`);
      return json({ ok: true }, 200, { "Set-Cookie": sessionCookie("", 0) });
    }

    if (!(await currentUser(env, request))) {
      if (request.method === "GET" && (request.headers.get("Accept") || "").includes("text/html"))
        return Response.redirect(new URL("/login", url).toString(), 302);
      return json({ error: "Not signed in." }, 401);
    }

    if (upstream && env.BILLRAG) {
      if (!env.PROXY_SECRET) return json({ error: "Server is not configured." }, 500);
      return upstream(request, env);
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
