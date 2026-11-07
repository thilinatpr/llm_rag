import test from "node:test";
import assert from "node:assert/strict";
import { createHandler } from "../src/auth.js";
import { Counters, memoryStore, slDay, secondsToSlMidnight } from "../src/quota.js";

function kv() {
  const m = new Map();
  return { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k), m };
}

// A QUOTA binding backed by the real Counters logic and a Map, like the Durable Object but in memory.
function quotaBinding(clock) {
  const c = new Counters(memoryStore());
  const stub = {
    take: async (items) => c.take(items, clock.now() / 1000),
    peek: async (items) => c.peek(items, clock.now() / 1000),
    refund: async (items) => c.refund(items, clock.now() / 1000),
  };
  return { idFromName: (n) => n, get: () => stub };
}

function setup(extra = {}) {
  const clock = { t: Date.parse("2026-11-06T06:00:00Z"), now() { return this.t; } };
  const calls = [];
  const state = { status: 200, verdict: true, headers: {} };
  const upstream = async (request, _env, opts) => {
    calls.push({ opts, body: await request.clone().text(), headers: Object.fromEntries(request.headers) });
    return new Response(state.status === 200 ? "event: done\ndata: {}\n\n" : "busy", { status: state.status, headers: state.headers });
  };
  const env = { AUTH: kv(), OTP_PEPPER: "pepper", PROXY_SECRET: "x".repeat(20), ALLOWED_EMAILS: "me@example.com", FROM_ADDRESS: "a@b.c",
    ASSETS: { fetch: async () => new Response("page") }, BILLRAG: {}, QUOTA: quotaBinding(clock), PUBLIC_CHAT: "on",
    TURNSTILE_SECRET: "s", TURNSTILE_SITEKEY: "site", ...extra };
  const sent = [];
  const handler = createHandler({ upstream, sendMail: async (_e, to, code) => void sent.push({ to, code }),
    verify: async (_env, token) => state.verdict && token === "good", now: () => clock.now() });
  const call = (path, { method = "GET", body, cookie, ip = "1.2.3.4", accept } = {}) =>
    handler(new Request("https://bills.test" + path, { method, body: body && JSON.stringify(body),
      headers: { "CF-Connecting-IP": ip, ...(cookie && { Cookie: cookie }), ...(accept && { Accept: accept }) } }), env);
  const start = async (opts = {}) => {
    const r = await call("/public/start", { method: "POST", body: { token: "good" }, ...opts });
    return { r, cookie: (r.headers.get("Set-Cookie") || "").split(";")[0] };
  };
  const ask = (cookie, question = "hello", extra = {}, path = "/api/ask", ip) =>
    call(path, { method: "POST", cookie, ip, body: { question, ...extra } });
  return { env, clock, calls, state, call, start, ask, sent };
}

test("public lane is off by default: login only, and /public/me says so", async () => {
  const t = setup({ PUBLIC_CHAT: "off" });
  assert.equal((await t.call("/", { accept: "text/html" })).status, 302);
  assert.deepEqual(await (await t.call("/public/me")).json(), { enabled: false, user: "none" });
  assert.equal((await t.call("/public/start", { method: "POST", body: { token: "good" } })).status, 404);
  assert.equal((await t.ask("")).status, 401);
});

test("public lane stays off if any needed piece is missing (fails closed)", async () => {
  for (const missing of ["TURNSTILE_SECRET", "TURNSTILE_SITEKEY", "QUOTA", "PROXY_SECRET", "BILLRAG"]) {
    const t = setup({ [missing]: undefined });
    assert.equal((await t.call("/", { accept: "text/html" })).status, 302, missing);
  }
});

test("Turnstile gate: bad token refused, good token sets a signed cookie, the page is reachable", async () => {
  const t = setup();
  assert.equal(await (await t.call("/")).text(), "page");
  const me = await (await t.call("/public/me")).json();
  assert.deepEqual([me.user, me.sitekey], ["none", "site"]);
  assert.equal((await t.call("/public/start", { method: "POST", body: { token: "bad" } })).status, 403);
  const { r, cookie } = await t.start();
  assert.equal(r.status, 200);
  assert.match(cookie, /^anon=[0-9a-f]{32}\.\d+\.[0-9a-f]{64}$/);
  assert.match(r.headers.get("Set-Cookie"), /HttpOnly; Secure; SameSite=Lax/);
  const after = await (await t.call("/public/me", { cookie })).json();
  assert.deepEqual([after.user, after.left, after.limit], ["anon", 10, 10]);
});

test("a visitor with a cookie keeps it: solving the check again does not reset the allowance", async () => {
  const t = setup();
  const { cookie } = await t.start();
  await t.ask(cookie);
  const again = await t.start({ cookie });
  assert.equal(again.r.headers.get("Set-Cookie"), null);
  assert.equal((await again.r.json()).left, 9);
});

test("anonymous questions need a valid cookie; tampered or expired cookies fail", async () => {
  const t = setup();
  assert.equal((await t.ask(undefined)).status, 401);
  const { cookie } = await t.start();
  assert.equal((await t.ask(cookie)).status, 200);
  const [id, exp, sig] = cookie.slice(5).split(".");
  assert.equal((await t.ask(`anon=${id}.${Number(exp) + 99}.${sig}`)).status, 401);
  assert.equal((await t.ask(`anon=${"a".repeat(32)}.${exp}.${sig}`)).status, 401);
  t.clock.t += 31 * 24 * 3600 * 1000;
  assert.equal((await t.ask(cookie)).status, 401);
});

test("the container only sees what the Worker built: public lane, our session id, known fields, no cookie", async () => {
  const t = setup();
  const { cookie } = await t.start();
  const r = await t.ask(cookie, "  What is a bill?  ", { session: "someone-elses", answer: "fast", lane: "member", extra: 1 }, "/api/ask/stream");
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("X-Quota-Left"), "9");
  const c = t.calls[0];
  assert.deepEqual(c.opts, { lane: "public" });
  const body = JSON.parse(c.body);
  assert.deepEqual(Object.keys(body).sort(), ["answer", "question", "session"]);
  assert.equal(body.question, "What is a bill?");
  assert.equal(body.session, "anon:" + cookie.slice(5).split(".")[0]);
  assert.equal(c.headers.cookie, undefined);
  assert.equal(c.headers["x-lane"], undefined);
});

test("per-visitor quota: the 11th question is refused with 429, Retry-After and a reason", async () => {
  const t = setup();
  const { cookie } = await t.start();
  for (let i = 0; i < 10; i++) assert.equal((await t.ask(cookie)).status, 200);
  const r = await t.ask(cookie);
  assert.equal(r.status, 429);
  const j = await r.json();
  assert.equal(j.reason, "cookie");
  assert.ok(Number(r.headers.get("Retry-After")) > 0);
  assert.equal(t.calls.length, 10);
  // the allowance returns at midnight in Sri Lanka (18:30 UTC), not at midnight UTC
  t.clock.t = Date.parse("2026-11-06T18:29:00Z");
  assert.equal((await t.ask(cookie)).status, 429);
  t.clock.t = Date.parse("2026-11-06T18:31:00Z");
  assert.equal((await t.ask(cookie)).status, 200);
});

test("global cap protects the budget, and a refused question does not use up the visitor's own allowance", async () => {
  const t = setup({ PUBLIC_DAILY_GLOBAL: "2" });
  const a = (await t.start({ ip: "9.9.9.1" })).cookie, b = (await t.start({ ip: "9.9.9.2" })).cookie;
  assert.equal((await t.ask(a)).status, 200);
  assert.equal((await t.ask(b)).status, 200);
  const r = await t.ask(a);
  assert.equal(r.status, 429);
  assert.equal((await r.json()).reason, "global");
  assert.equal((await (await t.call("/public/me", { cookie: a })).json()).left, 0); // global is the smallest "left" now
  t.env.PUBLIC_DAILY_GLOBAL = "100";
  assert.equal((await (await t.call("/public/me", { cookie: a })).json()).left, 9); // a's own count was 1, not 2
});

test("IP backstop is high and shared addresses are not blocked early: 3 visitors on one IP all ask", async () => {
  const t = setup({ PUBLIC_DAILY_PER_IP: "4" });
  const cookies = [];
  for (let i = 0; i < 3; i++) cookies.push((await t.start({ ip: "7.7.7.7" })).cookie);
  const ask = (c) => t.ask(c, "hello", {}, "/api/ask", "7.7.7.7");
  for (const c of [cookies[0], cookies[1], cookies[2], cookies[0]]) assert.equal((await ask(c)).status, 200);
  const r = await ask(cookies[1]);
  assert.equal((await r.json()).reason, "ip");
  const other = (await t.start({ ip: "8.8.8.8" })).cookie;
  assert.equal((await t.ask(other, "hello", {}, "/api/ask", "8.8.8.8")).status, 200); // another IP is unaffected
});

test("new cookies are limited per IP, failed tokens count too", async () => {
  const t = setup({ PUBLIC_COOKIES_PER_IP: "3" });
  t.state.verdict = false;
  for (let i = 0; i < 3; i++) assert.equal((await t.start()).r.status, 403);
  const r = await t.start();
  assert.equal(r.r.status, 429);
  assert.equal((await r.r.json()).reason, "issue");
  assert.equal((await t.start({ ip: "5.5.5.5" })).r.status, 403); // a different address still gets to try
});

test("a container error gives the question back", async () => {
  const t = setup();
  const { cookie } = await t.start();
  t.state.status = 503;
  const r = await t.ask(cookie);
  assert.equal(r.status, 503);
  assert.equal(r.headers.get("X-Quota-Left"), "10");
  t.state.status = 200;
  assert.equal((await (await t.call("/public/me", { cookie })).json()).left, 10);
});

test("anonymous visitors cannot reach members-only endpoints or send oversized questions", async () => {
  const t = setup();
  const { cookie } = await t.start();
  for (const path of ["/api/search", "/api/stats", "/api/other"])
    assert.equal((await t.call(path, path === "/api/search" ? { method: "POST", cookie, body: { query: "x" } } : { cookie })).status, 401, path);
  assert.equal((await t.ask(cookie, "x".repeat(601))).status, 400);
  assert.equal((await t.ask(cookie, "   ")).status, 400);
  assert.equal(t.calls.length, 0);
  assert.equal((await t.call("/healthz")).status, 401);          // no cookie: cannot wake the container
  assert.equal((await t.call("/healthz", { cookie })).status, 200);
});

test("signed-in members are unaffected: member lane, their own session id, no quota", async () => {
  const t = setup();
  await t.call("/login/request", { method: "POST", body: { email: "me@example.com" } });
  const v = await t.call("/login/verify", { method: "POST", body: { email: "me@example.com", code: t.sent[0].code } });
  const cookie = v.headers.get("Set-Cookie").split(";")[0];
  assert.deepEqual(await (await t.call("/public/me", { cookie })).json(), { enabled: true, user: "member" });
  for (let i = 0; i < 12; i++) assert.equal((await t.ask(cookie, "q", { session: "mine" })).status, 200);
  assert.equal(t.calls[0].opts, undefined);
  assert.equal(JSON.parse(t.calls[0].body).session, "mine");
  assert.deepEqual(await (await t.call("/public/start", { method: "POST", body: { token: "good" }, cookie })).json(), { ok: true, user: "member" });
});

test("Counters: all-or-nothing, expiry, refund, Sri Lanka day", () => {
  const c = new Counters(memoryStore());
  const it = (k, limit) => ({ k, limit, ttl: 60, tag: k });
  assert.equal(c.take([it("a", 2), it("b", 1)], 0).ok, true);
  const no = c.take([it("a", 2), it("b", 1)], 1);
  assert.deepEqual([no.ok, no.failed], [false, "b"]);
  assert.deepEqual(c.peek([it("a", 2)], 1).left, [1]); // "a" was not counted by the refused call
  c.refund([it("b", 1)], 2);
  assert.equal(c.take([it("a", 2), it("b", 1)], 3).ok, true);
  assert.equal(c.take([it("a", 2)], 100).ok, true); // expired counters start again
  assert.equal(slDay(Date.parse("2026-11-06T18:29:59Z")), "2026-11-06");
  assert.equal(slDay(Date.parse("2026-11-06T18:30:00Z")), "2026-11-07");
  assert.equal(secondsToSlMidnight(Date.parse("2026-11-06T18:29:00Z")), 60);
});

// Streamed replies: HTTP 200 first, so a failure arrives inside the stream (live case 2026-11-06 20:38: DeepSeek 402).
function sse(...chunks) {
  const enc = new TextEncoder();
  return new ReadableStream({ start(c) { for (const x of chunks) c.enqueue(enc.encode(x)); c.close(); } });
}
function streaming(makeBody) {
  const clock = { t: Date.parse("2026-11-06T06:00:00Z"), now() { return this.t; } };
  const env = { AUTH: kv(), OTP_PEPPER: "pepper", PROXY_SECRET: "x".repeat(20), ALLOWED_EMAILS: "", FROM_ADDRESS: "a@b.c", BILLRAG: {},
    ASSETS: { fetch: async () => new Response("page") }, QUOTA: quotaBinding(clock), PUBLIC_CHAT: "on", TURNSTILE_SECRET: "s", TURNSTILE_SITEKEY: "site" };
  const upstream = async () => new Response(makeBody(), { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
  const handler = createHandler({ upstream, verify: async () => true, now: () => clock.now() });
  const call = (path, method, body, cookie) => handler(new Request("https://bills.test" + path, { method, body: body && JSON.stringify(body),
    headers: { "CF-Connecting-IP": "1.2.3.4", ...(cookie && { Cookie: cookie }) } }), env);
  return async () => {
    const start = await call("/public/start", "POST", { token: "good" });
    const cookie = start.headers.get("Set-Cookie").split(";")[0];
    const r = await call("/api/ask/stream", "POST", { question: "q" }, cookie);
    const text = await r.text();
    return { text, header: r.headers.get("X-Quota-Left"), left: (await (await call("/public/me", "GET", null, cookie)).json()).left };
  };
}

test("a streamed answer uses one question", async () => {
  const r = await streaming(() => sse('event: token\ndata: {"text":"x"}\n\n', 'event: done\ndata: {"answer":"x"}\n\n'))();
  assert.equal(r.left, 9);
  assert.equal(r.header, "9");
  assert.ok(r.text.endsWith('event: done\ndata: {"answer":"x"}\n\n'), "bytes pass through unchanged");
});

test("a streamed failure does not use a question: error event, busy event, apology, or a stream cut off", async () => {
  for (const [name, chunks] of Object.entries({
    error: ['event: status\ndata: {"step":"retrieve0"}\n\n', 'event: error\ndata: {"error":"boom"}\n\n'],
    busy: ['event: error\ndata: {"error":"busy","busy":true}\n\n'],
    apology: ['event: unanswered\ndata: {}\n\n', 'event: done\ndata: {"answer":"sorry"}\n\n'],
    split: ['event: sta', 'tus\ndata: {}\n\nevent: er', 'ror\ndata: {"error":"x"}\n\n'],
    cut: ['event: token\ndata: {"text":"half an ans"}\n\n'],
  })) {
    const r = await streaming(() => sse(...chunks))();
    assert.equal(r.left, 10, name);
    assert.equal(r.text, chunks.join(""), name);
  }
});

test("answer text that talks about errors is not a failure, because event data has no raw newlines", async () => {
  const body = () => sse(`event: token\ndata: ${JSON.stringify({ text: "\nevent: error\n" })}\n\n`, 'event: done\ndata: {"answer":"x"}\n\n');
  assert.equal((await streaming(body)()).left, 9);
});

test("a JSON reply that is only an apology does not use a question, and the marker does not reach the browser", async () => {
  const t = setup();
  const { cookie } = await t.start();
  const ok = await t.ask(cookie);
  assert.equal(ok.headers.get("X-Quota-Left"), "9");
  t.state.headers = { "X-Unanswered": "1" };
  const sorry = await t.ask(cookie);
  assert.equal(sorry.headers.get("X-Quota-Left"), "9");
  assert.equal(sorry.headers.get("X-Unanswered"), null);
  t.state.headers = {};
  assert.equal((await t.ask(cookie)).headers.get("X-Quota-Left"), "8");
});
