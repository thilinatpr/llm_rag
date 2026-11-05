import test from "node:test";
import assert from "node:assert/strict";
import { createHandler, buildMessage, sendMailViaCloudflare, newCode, normalizeEmail } from "../src/auth.js";

function kv() {
  const m = new Map();
  return { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k), m };
}
function setup() {
  const sent = [];
  const env = { AUTH: kv(), OTP_PEPPER: "pepper", ALLOWED_EMAILS: "Me@Example.com", FROM_ADDRESS: "login@example.com",
    ASSETS: { fetch: async () => new Response("hello page") } };
  const handler = createHandler({ sendMail: async (_e, to, code) => void sent.push({ to, code }) });
  const call = (path, { method = "GET", body, cookie, accept } = {}) =>
    handler(new Request("https://x.test" + path, { method, body: body && JSON.stringify(body),
      headers: { ...(cookie && { Cookie: cookie }), ...(accept && { Accept: accept }) } }), env);
  return { env, sent, call };
}
const login = async (t) => {
  await t.call("/login/request", { method: "POST", body: { email: "me@example.com" } });
  const r = await t.call("/login/verify", { method: "POST", body: { email: "me@example.com", code: t.sent[0].code } });
  return r.headers.get("Set-Cookie").split(";")[0];
};

test("unauthenticated: redirect for browsers, 401 for API", async () => {
  const t = setup();
  const r = await t.call("/", { accept: "text/html" });
  assert.equal(r.status, 302);
  assert.match(r.headers.get("Location"), /\/login$/);
  assert.equal((await t.call("/api/ask", { method: "POST" })).status, 401);
});

test("allowed address gets a code; unknown address gets the same reply but no mail", async () => {
  const t = setup();
  const a = await t.call("/login/request", { method: "POST", body: { email: " ME@example.com " } });
  const b = await t.call("/login/request", { method: "POST", body: { email: "evil@example.com" } });
  assert.deepEqual(await a.json(), await b.json());
  assert.equal(t.sent.length, 1);
  assert.match(t.sent[0].code, /^\d{6}$/);
  assert.equal(t.sent[0].to, "me@example.com");
});

test("full login, session works, code is single use, logout revokes", async () => {
  const t = setup();
  const cookie = await login(t);
  assert.match(cookie, /^session=[0-9a-f]{64}$/);
  assert.equal(await (await t.call("/", { cookie })).text(), "hello page");
  const again = await t.call("/login/verify", { method: "POST", body: { email: "me@example.com", code: t.sent[0].code } });
  assert.equal(again.status, 401);
  await t.call("/logout", { method: "POST", cookie });
  assert.equal((await t.call("/", { cookie })).status, 401);
});

test("wrong codes are limited to 5 attempts, then even the right code fails", async () => {
  const t = setup();
  await t.call("/login/request", { method: "POST", body: { email: "me@example.com" } });
  const good = t.sent[0].code, wrong = good === "000000" ? "111111" : "000000";
  for (let i = 0; i < 5; i++)
    assert.equal((await t.call("/login/verify", { method: "POST", body: { email: "me@example.com", code: wrong } })).status, 401);
  assert.equal((await t.call("/login/verify", { method: "POST", body: { email: "me@example.com", code: good } })).status, 401);
});

test("resend cooldown and failed send cleanup", async () => {
  const t = setup();
  await t.call("/login/request", { method: "POST", body: { email: "me@example.com" } });
  await t.call("/login/request", { method: "POST", body: { email: "me@example.com" } });
  assert.equal(t.sent.length, 1);
  const env = { ...t.env, AUTH: kv() };
  const h = createHandler({ sendMail: async () => { throw new Error("nope"); } });
  const r = await h(new Request("https://x.test/login/request", { method: "POST", body: JSON.stringify({ email: "me@example.com" }) }), env);
  assert.equal(r.status, 502);
  assert.equal(env.AUTH.m.size, 0);
});

test("helpers", () => {
  assert.match(newCode(), /^\d{6}$/);
  assert.equal(normalizeEmail(" A@B.C "), "a@b.c");
  const m = buildMessage("login@example.com", "me@example.com", "123456");
  assert.equal(m.to, "me@example.com");
  assert.match(m.subject, /123456/);
  assert.match(m.text, /123456/);
});

test("REST sender posts to the Email Service endpoint, retries 429, throws on failure", async () => {
  const env = { CF_ACCOUNT_ID: "acc", CF_EMAIL_TOKEN: "tok", FROM_ADDRESS: "login@example.com" };
  const real = globalThis.fetch, calls = [];
  const reply = [new Response("slow down", { status: 429 }), new Response("{}", { status: 200 })];
  globalThis.fetch = async (url, init) => (calls.push({ url, init }), reply.shift());
  try {
    await sendMailViaCloudflare(env, "me@example.com", "123456");
    assert.equal(calls.length, 2);
    assert.equal(calls[0].url, "https://api.cloudflare.com/client/v4/accounts/acc/email/sending/send");
    assert.equal(calls[0].init.headers.Authorization, "Bearer tok");
    assert.equal(JSON.parse(calls[0].init.body).to, "me@example.com");
    globalThis.fetch = async () => new Response("denied", { status: 403 });
    await assert.rejects(sendMailViaCloudflare(env, "me@example.com", "1"), /HTTP 403/);
    await assert.rejects(sendMailViaCloudflare({}, "me@example.com", "1"), /not configured/);
  } finally {
    globalThis.fetch = real;
  }
});

test("signed-in requests go to the upstream only when it is bound and configured", async () => {
  const t = setup();
  const cookie = await login(t);
  const seen = [];
  const withUp = createHandler({ sendMail: async () => {}, upstream: async (req, env) => (seen.push(req.url), new Response("from container")) });
  const call = (env, path = "/x") => withUp(new Request("https://x.test" + path, { headers: { Cookie: cookie } }), env);
  // no BILLRAG binding -> falls through to static assets
  assert.equal(await (await call(t.env)).text(), "hello page");
  // bound but PROXY_SECRET missing -> fail closed
  assert.equal((await call({ ...t.env, BILLRAG: {} })).status, 500);
  assert.equal(seen.length, 0);
  const ok = await call({ ...t.env, BILLRAG: {}, PROXY_SECRET: "s".repeat(16) });
  assert.equal(await ok.text(), "from container");
  // unauthenticated requests never reach the upstream
  const anon = await withUp(new Request("https://x.test/api/ask", { method: "POST" }), { ...t.env, BILLRAG: {}, PROXY_SECRET: "s".repeat(16) });
  assert.equal(anon.status, 401);
  assert.equal(seen.length, 1);
});
