// Free accounts: anyone who has signed in with a one-time code while OPEN_SIGNUP = "on". Addresses in ALLOWED_EMAILS are staff and unlimited, as before.
//
// Sign-up needs Turnstile on the code request and a daily cap per network and per address (the email costs money and a sign-up is a free
// allowance), all counted in the QUOTA Durable Object. Anything missing -> sign-up is off and only ALLOWED_EMAILS can sign in (fails closed).
// A free account's questions are counted per day and per month on the server, per service: Bill Assistant 20/day and 150/month,
// Procurement Rules 5/day (vars MEMBER_DAILY, MEMBER_MONTHLY, "none" = no monthly cap). Replies that gave nothing are given back (public.js settle).
import { counter, MAX_QUESTION, settle, signupOn } from "./public.js";
export { signupOn };
import { secondsToSlMidnight, slDay } from "./quota.js";
import { hex, json, readJson } from "./util.js";

const num = (v, d) => (v !== undefined && v !== "" && Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : d);
export const memberLimits = (env) => ({
  day: num(env.MEMBER_DAILY, 20),
  month: env.MEMBER_MONTHLY === "none" ? null : num(env.MEMBER_MONTHLY, 150),
  codesPerIp: num(env.SIGNUP_CODES_PER_IP, 20),
  codesPerAddress: num(env.SIGNUP_CODES_PER_ADDRESS, 5),
});

const enc = new TextEncoder();
const who = async (email) => hex(await crypto.subtle.digest("SHA-256", enc.encode(email))).slice(0, 16);

// Code requests: per network and per address per day. Counted before the mail is sent (a failed send gives them back, see auth.js).
export async function takeCodeRequest(env, ip, email, nowMs) {
  const L = memberLimits(env), day = slDay(nowMs), ttl = secondsToSlMidnight(nowMs) + 3600;
  const its = [
    { k: `si|${ip || "?"}|${day}`, limit: L.codesPerIp, ttl, tag: "ip" },
    { k: `sa|${await who(email)}|${day}`, limit: L.codesPerAddress, ttl, tag: "address" },
  ];
  const r = await counter(env).take(its);
  return { ok: r.ok, items: its };
}

const items = async (env, service, email, nowMs) => {
  const L = memberLimits(env), day = slDay(nowMs), id = await who(email);
  const out = [{ k: `mu|${service}|${id}|${day}`, limit: L.day, ttl: secondsToSlMidnight(nowMs) + 3600, tag: "day" }];
  if (L.month !== null) out.push({ k: `mm|${service}|${id}|${day.slice(0, 7)}`, limit: L.month, ttl: 32 * 86400, tag: "month" });
  return out;
};

export async function memberLeft(env, service, email, nowMs) {
  const { left } = await counter(env).peek(await items(env, service, email, nowMs));
  return Math.min(...left);
}

const TEXT = {
  day: "You have used today's questions on this free account. They come back tomorrow.",
  month: "You have used this month's questions on this free account. They come back next month.",
};

// A free account's /api/ask or /api/ask/stream. The body is the page's own, with our chat id (a client-chosen one could join someone else's chat).
export async function memberAsk(env, request, email, service, upstream, nowMs) {
  if (!env.QUOTA) return json({ error: "Server is not configured." }, 503);
  const body = await readJson(request);
  const text = String(body.question || "").trim();
  if (!text) return json({ error: "Empty question." }, 400);
  if (text.length > MAX_QUESTION) return json({ error: `Please keep your question under ${MAX_QUESTION} characters.`, reason: "long" }, 400);
  const its = await items(env, service, email, nowMs);
  const took = await counter(env).take(its);
  if (!took.ok)
    return json({ error: TEXT[took.failed] || TEXT.day, reason: took.failed, left: 0 }, 429, { "Retry-After": String(secondsToSlMidnight(nowMs)) });
  const chat = String(body.session || "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 32);
  const payload = { ...body, question: text, session: `m:${await who(email)}${chat ? ":" + chat : ""}` };
  const forwarded = new Request(request.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  return settle(env, await upstream(forwarded, env), its, Math.min(...took.left));
}
