// Public (anonymous) chat lane in front of the Bill Assistant. OFF unless PUBLIC_CHAT = "on" and every piece it needs exists
// (Turnstile keys, the QUOTA Durable Object, the secrets); with anything missing the Worker behaves as before: login only.
//
// A visitor passes an invisible Cloudflare Turnstile check once (POST /public/start) and gets a signed `anon` cookie. The cookie
// is only an id: the allowance behind it is counted on the server (Durable Object), so clearing browser storage buys nothing.
// Per question three counters must all have room (they reset at midnight in Sri Lanka):
//   cookie  PUBLIC_DAILY_PER_COOKIE  (10)   one visitor
//   ip      PUBLIC_DAILY_PER_IP      (300)  a flood backstop, deliberately high: many Sri Lankan users share one mobile IP
//   global  PUBLIC_DAILY_GLOBAL      (500)  the hard cost ceiling for the whole public lane
// and new cookies are limited per IP (PUBLIC_COOKIES_PER_IP, 50) so Turnstile cannot be farmed from one address.
// Anonymous requests reach the container as lane "public" with their own session id ("anon:<cookie id>", never client chosen).
import { secondsToSlMidnight, slDay } from "./quota.js";
import { getCookie, hex, json, readJson, safeEqual } from "./util.js";

const COOKIE = "anon";
const COOKIE_TTL = 30 * 24 * 3600;
export const MAX_QUESTION = 600; // characters; the model sees the question and the bills, not an essay
const enc = new TextEncoder();

const num = (v, d) => (v !== undefined && v !== "" && Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : d);
export const limits = (env) => ({
  cookie: num(env.PUBLIC_DAILY_PER_COOKIE, 10),
  ip: num(env.PUBLIC_DAILY_PER_IP, 300),
  global: num(env.PUBLIC_DAILY_GLOBAL, 500),
  issue: num(env.PUBLIC_COOKIES_PER_IP, 50),
});

export const publicOn = (env) =>
  env.PUBLIC_CHAT === "on" && !!(env.TURNSTILE_SECRET && env.TURNSTILE_SITEKEY && env.OTP_PEPPER && env.PROXY_SECRET && env.QUOTA && env.BILLRAG);

const MESSAGES = {
  cookie: "You have used today's free questions. Please come back tomorrow.",
  ip: "Too many questions from your network today. Please come back tomorrow.",
  global: "Today's free questions are used up. Please come back tomorrow.",
  issue: "Too many new visits from your network today. Please come back tomorrow.",
};

const counter = (env) => env.QUOTA.get(env.QUOTA.idFromName("main"));
const clientIp = (request) => request.headers.get("CF-Connecting-IP") || "";

function items(env, anonId, ip, nowMs) {
  const L = limits(env), day = slDay(nowMs), ttl = secondsToSlMidnight(nowMs) + 3600;
  return [
    { k: `c|${anonId}|${day}`, limit: L.cookie, ttl, tag: "cookie" },
    { k: `i|${ip || "?"}|${day}`, limit: L.ip, ttl, tag: "ip" },
    { k: `g|${day}`, limit: L.global, ttl, tag: "global" },
  ];
}

async function sign(env, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(`${env.OTP_PEPPER}|anon-cookie`), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, enc.encode(msg)));
}

async function issueAnon(env, nowMs) {
  const id = hex(crypto.getRandomValues(new Uint8Array(16)));
  const exp = Math.floor(nowMs / 1000) + COOKIE_TTL;
  return { id, value: `${id}.${exp}.${await sign(env, `${id}.${exp}`)}` };
}

async function readAnon(env, request, nowMs) {
  const m = /^([0-9a-f]{32})\.(\d{1,12})\.([0-9a-f]{64})$/.exec(getCookie(request, COOKIE) || "");
  if (!m || Number(m[2]) * 1000 < nowMs) return null;
  return safeEqual(m[3], await sign(env, `${m[1]}.${m[2]}`)) ? { id: m[1] } : null;
}

const anonCookie = (value, maxAge) => `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;

export async function verifyTurnstile(env, token, ip, host) {
  if (!token || typeof token !== "string" || token.length > 2048) return false;
  const form = new URLSearchParams({ secret: env.TURNSTILE_SECRET, response: token });
  if (ip) form.set("remoteip", ip);
  try {
    const res = await globalThis.fetch("https://challenges.cloudflare.com/turnstile/v0/siteverify", { method: "POST", body: form });
    const d = await res.json();
    return d.success === true && (!d.hostname || d.hostname === host);
  } catch {
    return false;
  }
}

// GET /public/me: what the page needs to decide between member view, "verify" view and anonymous view.
export async function publicMe(env, request, member, nowMs) {
  const headers = { "Cache-Control": "no-store" };
  if (!publicOn(env)) return json({ enabled: false, user: member ? "member" : "none" }, 200, headers);
  if (member) return json({ enabled: true, user: "member" }, 200, headers);
  const anon = await readAnon(env, request, nowMs);
  if (!anon) return json({ enabled: true, user: "none", sitekey: env.TURNSTILE_SITEKEY, limit: limits(env).cookie }, 200, headers);
  const { left } = await counter(env).peek(items(env, anon.id, clientIp(request), nowMs));
  return json({ enabled: true, user: "anon", left: Math.min(...left), limit: limits(env).cookie }, 200, headers);
}

// POST /public/start {token}: Turnstile token -> `anon` cookie. A visitor who already has a valid cookie keeps it (re-solving the
// check never resets the allowance).
export async function publicStart(env, request, verify, nowMs) {
  if (!publicOn(env)) return json({ error: "Not found." }, 404);
  const ip = clientIp(request);
  const existing = await readAnon(env, request, nowMs);
  if (existing) {
    const { left } = await counter(env).peek(items(env, existing.id, ip, nowMs));
    return json({ ok: true, left: Math.min(...left), limit: limits(env).cookie });
  }
  // Count the attempt before verifying, so failed tokens cannot be used to hammer the verification API from one address.
  const day = slDay(nowMs), ttl = secondsToSlMidnight(nowMs) + 3600;
  const took = await counter(env).take([{ k: `n|${ip || "?"}|${day}`, limit: limits(env).issue, ttl, tag: "issue" }]);
  if (!took.ok) return json({ error: MESSAGES.issue, reason: "issue" }, 429, { "Retry-After": String(secondsToSlMidnight(nowMs)) });
  const body = await readJson(request);
  if (!(await verify(env, body.token, ip, new URL(request.url).hostname)))
    return json({ error: "The check failed. Reload the page and try again.", reason: "turnstile" }, 403);
  const anon = await issueAnon(env, nowMs);
  const { left } = await counter(env).peek(items(env, anon.id, ip, nowMs));
  return json({ ok: true, left: Math.min(...left), limit: limits(env).cookie }, 200, { "Set-Cookie": anonCookie(anon.value, COOKIE_TTL) });
}

// /api/ask, /api/ask/stream and /healthz for a visitor who is not signed in. Everything else under /api is members only.
export async function anonApi(env, request, pathname, upstream, nowMs) {
  const anon = await readAnon(env, request, nowMs);
  if (!anon) return json({ error: "Please complete the check first.", reason: "verify" }, 401);
  if (pathname === "/healthz") return upstream(request, env, { lane: "public" });
  if (request.method !== "POST") return json({ error: "Not found." }, 404);
  const body = await readJson(request);
  const text = String(body.question || "").trim();
  if (!text) return json({ error: "Empty question." }, 400);
  if (text.length > MAX_QUESTION)
    return json({ error: `Please keep your question under ${MAX_QUESTION} characters.`, reason: "long" }, 400);
  const its = items(env, anon.id, clientIp(request), nowMs);
  const took = await counter(env).take(its);
  if (!took.ok)
    return json({ error: MESSAGES[took.failed] || MESSAGES.global, reason: took.failed, left: 0 }, 429, {
      "Retry-After": String(secondsToSlMidnight(nowMs)),
    });
  // Rebuild the body: the session id is ours (a client-chosen one could join someone else's chat), only known fields pass.
  const payload = { session: `anon:${anon.id}`, question: text };
  if (body.answer === "fast" || body.answer === "thorough") payload.answer = body.answer;
  const forwarded = new Request(request.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const res = await upstream(forwarded, env, { lane: "public" });
  if (res.status >= 500) await counter(env).refund(its); // busy or down: the visitor got nothing, so the question is not used
  const out = new Response(res.body, res);
  out.headers.set("X-Quota-Left", String(res.status >= 500 ? Math.min(...took.left) + 1 : Math.min(...took.left)));
  return out;
}
