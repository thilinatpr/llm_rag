// Open sign-up and free-account limits: src/members.js, auth.js. Staff (ALLOWED_EMAILS) stay unlimited.
import test from "node:test";
import assert from "node:assert/strict";
import { createHandler } from "../src/auth.js";
import { Counters, memoryStore } from "../src/quota.js";
import { required } from "../src/consent.js";

const kv = () => { const m = new Map(); return { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k), m }; };
function setup(extra = {}, service = "bills") {
  const clock = { t: Date.parse("2026-11-09T06:00:00Z"), now() { return this.t; } };
  const c = new Counters(memoryStore());
  const stub = { take: async (i) => c.take(i, clock.now() / 1000), peek: async (i) => c.peek(i, clock.now() / 1000), refund: async (i) => c.refund(i, clock.now() / 1000) };
  const state = { reply: () => new Response(JSON.stringify({ answer: "a" })) }, seen = [], sent = [];
  const upstream = async (req) => (seen.push(JSON.parse(await req.clone().text())), state.reply());
  const env = { AUTH: kv(), OTP_PEPPER: "p", PROXY_SECRET: "x".repeat(20), ALLOWED_EMAILS: "staff@example.com", FROM_ADDRESS: "a@b.c", BILLRAG: {},
    ASSETS: { fetch: async () => new Response("page") }, QUOTA: { idFromName: (n) => n, get: () => stub }, OPEN_SIGNUP: "on", TURNSTILE_SECRET: "s", TURNSTILE_SITEKEY: "site", ...extra };
  const h = createHandler({ upstream, cookie: "clk_session", privacy: service, consent: { service, name: service }, now: () => clock.now(),
    sendMail: async (_e, to, code) => void sent.push({ to, code }), verify: async (_e, token) => token === "good" });
  const call = (path, { method = "GET", body, cookie, ip = "1.1.1.1", accept } = {}) =>
    h(new Request("https://bills.test" + path, { method, body: body && JSON.stringify(body), headers: { "CF-Connecting-IP": ip, ...(cookie && { Cookie: cookie }), ...(accept && { Accept: accept }) } }), env);
  const join = async (email, { ip, token = "good", accept = true } = {}) => {
    const q = await call("/login/request", { method: "POST", ip, body: { email, token } });
    if (q.status !== 200) return { q };
    const code = sent.filter((s) => s.to === email).at(-1).code;
    const r = await call("/login/verify", { method: "POST", body: { email, code } });
    const cookie = r.headers.get("Set-Cookie").split(";")[0];
    if (accept) await call("/consent", { method: "POST", cookie, body: { ids: required(service).map((i) => i.id) } });
    return { q, cookie };
  };
  const ask = (cookie, body = { question: "hello", session: "chat1" }, path = "/api/ask") => call(path, { method: "POST", cookie, body });
  return { env, clock, state, seen, sent, call, join, ask };
}

test("sign-up is off unless switched on and every piece exists: only staff get codes", async () => {
  for (const off of [{ OPEN_SIGNUP: "off" }, { OPEN_SIGNUP: undefined }, { TURNSTILE_SECRET: undefined }, { TURNSTILE_SITEKEY: undefined }, { QUOTA: undefined }]) {
    const t = setup(off);
    await t.call("/login/request", { method: "POST", body: { email: "new@example.com", token: "good" } });
    assert.equal(t.sent.length, 0, JSON.stringify(Object.keys(off)));
  }
});

test("open sign-up: Turnstile is required, a new address gets a code and a session, and the login page shows the widget", async () => {
  const t = setup();
  assert.equal((await t.join("new@example.com", { token: "bad" })).q.status, 403);
  assert.equal(t.sent.length, 0);
  const { cookie } = await t.join("new@example.com");
  assert.equal((await t.ask(cookie)).status, 200);
  const page = await (await t.call("/login")).text();
  assert.ok(page.includes("cf-turnstile") && page.includes('data-sitekey="site"') && page.includes("create a free account"));
  const closed = await (await setup({ OPEN_SIGNUP: "off" }).call("/login")).text();
  assert.ok(!closed.includes("cf-turnstile"));
});

test("sign-up caps: 5 codes per address and 20 per network a day, then tomorrow works", async () => {
  const t = setup();
  for (let i = 0; i < 5; i++) { t.clock.t += 61_000; assert.equal((await t.call("/login/request", { method: "POST", body: { email: "a@example.com", token: "good" } })).status, 200); }
  t.clock.t += 61_000;
  assert.equal((await t.call("/login/request", { method: "POST", body: { email: "a@example.com", token: "good" } })).status, 429);
  const u = setup();
  for (let i = 0; i < 20; i++) assert.equal((await u.call("/login/request", { method: "POST", ip: "9.9.9.9", body: { email: `p${i}@example.com`, token: "good" } })).status, 200);
  assert.equal((await u.call("/login/request", { method: "POST", ip: "9.9.9.9", body: { email: "p21@example.com", token: "good" } })).status, 429);
  assert.equal((await u.call("/login/request", { method: "POST", ip: "8.8.8.8", body: { email: "p22@example.com", token: "good" } })).status, 200);
  u.clock.t += 24 * 3600_000;
  assert.equal((await u.call("/login/request", { method: "POST", ip: "9.9.9.9", body: { email: "p21@example.com", token: "good" } })).status, 200);
});

test("a free account: 20 a day, then refused with its own sentence; tomorrow it works again; staff are never counted", async () => {
  const t = setup({ MEMBER_MONTHLY: "none" });
  const { cookie } = await t.join("new@example.com");
  for (let i = 0; i < 20; i++) assert.equal((await t.ask(cookie)).status, 200, String(i));
  const over = await t.ask(cookie);
  assert.equal(over.status, 429);
  const j = await over.json();
  assert.equal(j.reason, "day");
  assert.match(j.error, /today/);
  t.clock.t += 24 * 3600_000;
  assert.equal((await t.ask(cookie)).status, 200);
  const staff = (await t.join("staff@example.com")).cookie;
  for (let i = 0; i < 25; i++) assert.equal((await t.ask(staff)).status, 200);
});

test("a free account's month cap (150) holds across days", async () => {
  const t = setup({ MEMBER_DAILY: "100" });
  const { cookie } = await t.join("new@example.com");
  let ok = 0;
  for (let day = 0; day < 3; day++) {
    for (let i = 0; i < 60; i++) if ((await t.ask(cookie)).status === 200) ok++;
    t.clock.t += 24 * 3600_000;
  }
  assert.equal(ok, 150);
  const over = await (await t.ask(cookie)).json();
  assert.equal(over.reason, "month");
  assert.match(over.error, /month/);
});

test("failures and free turns are given back: a 5xx, an apology, a greeting, and the same for a stream", async () => {
  const t = setup({ MEMBER_DAILY: "3", MEMBER_MONTHLY: "none" });
  const { cookie } = await t.join("new@example.com");
  const left = async () => (await (await t.call("/public/me", { cookie })).json()).left;
  assert.equal(await left(), 3);
  t.state.reply = () => new Response("down", { status: 502 });
  assert.equal((await t.ask(cookie)).status, 502);
  t.state.reply = () => new Response("{}", { headers: { "X-Unanswered": "1" } });
  await t.ask(cookie);
  t.state.reply = () => new Response("{}", { headers: { "X-Free": "1" } });
  const hello = await t.ask(cookie);
  assert.equal(hello.headers.get("X-Free"), null);          // the Worker's marker does not reach the browser
  assert.equal(await left(), 3);
  t.state.reply = () => new Response("event: status\ndata: {}\n\nevent: free\ndata: {}\n\nevent: done\ndata: {}\n\n", { headers: { "Content-Type": "text/event-stream" } });
  await (await t.ask(cookie, { question: "hi" }, "/api/ask/stream")).text();
  assert.equal(await left(), 3);
  t.state.reply = () => new Response("event: done\ndata: {}\n\n", { headers: { "Content-Type": "text/event-stream" } });
  await (await t.ask(cookie, { question: "real" }, "/api/ask/stream")).text();
  assert.equal(await left(), 2);
});

test("the container gets our chat id, never one a client could choose to join another person's chat; the left count is a header", async () => {
  const t = setup();
  const a = (await t.join("a@example.com")).cookie, b = (await t.join("b@example.com")).cookie;
  const ra = await t.ask(a, { question: " hi ", session: "../x chat" });
  await t.ask(b, { question: "hi", session: "../x chat" });
  assert.equal(ra.headers.get("X-Quota-Left"), "19");
  assert.match(t.seen[0].session, /^m:[0-9a-f]{16}:xchat$/);
  assert.equal(t.seen[0].question, "hi");
  assert.notEqual(t.seen[0].session, t.seen[1].session);
  assert.equal((await t.ask(a, { question: "x".repeat(601) })).status, 400);
});

test("Procurement Rules counts 5 a day on its own counter, apart from Bill Assistant's", async () => {
  const t = setup({ MEMBER_DAILY: "5", MEMBER_MONTHLY: "none" }, "procurement");
  const { cookie } = await t.join("new@example.com");
  for (let i = 0; i < 5; i++) assert.equal((await t.ask(cookie)).status, 200);
  assert.equal((await t.ask(cookie)).status, 429);
});

test("no QUOTA binding: a free account is refused rather than left uncounted", async () => {
  const t = setup();
  const { cookie } = await t.join("new@example.com");
  delete t.env.QUOTA;                                          // sign-up is now off; the free session no longer works at all
  assert.notEqual((await t.ask(cookie)).status, 200);
});

test("the public lane tells a visitor to sign up only when sign-up is open", async () => {
  const mk = async (extra) => {
    const t = setup({ PUBLIC_CHAT: "on", PUBLIC_DAILY_PER_COOKIE: "1", ...extra });
    const r = await t.call("/public/start", { method: "POST", body: { token: "good" } });
    const cookie = r.headers.get("Set-Cookie").split(";")[0];
    await t.ask(cookie);
    return (await (await t.ask(cookie)).json()).error;
  };
  assert.match(await mk({}), /free account/);
  assert.doesNotMatch(await mk({ OPEN_SIGNUP: "off" }), /free account/);
});
