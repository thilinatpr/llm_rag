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

test("page is static; only /api/* and /healthz go to the upstream, and only when configured", async () => {
  const t = setup();
  const cookie = await login(t);
  const seen = [];
  const withUp = createHandler({ sendMail: async () => {}, upstream: async (req) => (seen.push(new URL(req.url).pathname), new Response("from container")) });
  const call = (env, path, method = "GET") => withUp(new Request("https://x.test" + path, { method, headers: { Cookie: cookie } }), env);
  const bound = { ...t.env, BILLRAG: {}, PROXY_SECRET: "s".repeat(16) };
  // no BILLRAG binding -> static assets for everything
  assert.equal(await (await call(t.env, "/api/ask", "POST")).text(), "hello page");
  // bound: the page comes from assets without touching the container
  assert.equal(await (await call(bound, "/")).text(), "hello page");
  assert.equal(seen.length, 0);
  // API and health probe go upstream
  assert.equal(await (await call(bound, "/api/ask", "POST")).text(), "from container");
  assert.equal(await (await call(bound, "/healthz")).text(), "from container");
  assert.deepEqual(seen, ["/api/ask", "/healthz"]);
  // bound but PROXY_SECRET missing -> fail closed for upstream routes
  assert.equal((await call({ ...t.env, BILLRAG: {} }, "/api/ask", "POST")).status, 500);
  // unauthenticated requests never reach the upstream
  const anon = await withUp(new Request("https://x.test/api/ask", { method: "POST" }), bound);
  assert.equal(anon.status, 401);
  assert.equal(seen.length, 2);
});

import { chatEnv, DEEPSEEK_BASE_URL } from "../src/chatconfig.js";

test("chat provider: Workers AI by default, DeepSeek when asked and keyed", () => {
  const base = { CF_ACCOUNT_ID: "acct", CLOUDFLARE_API_TOKEN: "cf-token", TRANSLATE_MODEL: "@cf/meta/llama-3.3-70b-instruct-fp8-fast" };
  const wa = chatEnv(base);
  assert.equal(wa.provider, "workers-ai");
  assert.equal(wa.vars.BILLRAG_CHAT_MODEL, "@cf/openai/gpt-oss-120b");
  assert.equal(wa.vars.BILLRAG_CHAT_BASE_URL, "https://api.cloudflare.com/client/v4/accounts/acct/ai/v1");
  assert.equal(wa.vars.BILLRAG_CHAT_API_KEY, "cf-token");
  assert.equal(wa.vars.BILLRAG_TRANSLATE_MODEL, "@cf/meta/llama-3.3-70b-instruct-fp8-fast");
  assert.equal(wa.vars.BILLRAG_GRADE_EFFORT, undefined);                       // unchanged behaviour: the Python default applies

  const ds = chatEnv({ ...base, CHAT_PROVIDER: "DeepSeek", DEEPSEEK_API_KEY: "ds-key", CHAT_MODEL: "@cf/openai/gpt-oss-120b" });
  assert.equal(ds.provider, "deepseek");
  assert.equal(ds.vars.BILLRAG_CHAT_MODEL, "deepseek-flash");                   // a Workers AI model id is not sent to DeepSeek
  assert.equal(ds.vars.BILLRAG_CHAT_BASE_URL, DEEPSEEK_BASE_URL);
  assert.equal(ds.vars.BILLRAG_CHAT_API_KEY, "ds-key");                         // never the Cloudflare token
  assert.equal(ds.vars.BILLRAG_GRADE_EFFORT, "off");
  assert.equal(ds.vars.BILLRAG_TRANSLATE_MODEL, undefined);                     // the Llama translator cannot run on DeepSeek's endpoint
  assert.equal(chatEnv({ ...base, CHAT_PROVIDER: "deepseek", DEEPSEEK_API_KEY: "k", CHAT_MODEL: "deepseek-v4-pro" }).vars.BILLRAG_CHAT_MODEL, "deepseek-v4-pro");
});

test("chat provider: deepseek without its secret falls back to Workers AI", () => {
  const warn = console.warn; let warned = "";
  console.warn = (m) => { warned = m; };
  try {
    const r = chatEnv({ CF_ACCOUNT_ID: "a", CLOUDFLARE_API_TOKEN: "t", CHAT_PROVIDER: "deepseek" });
    assert.equal(r.provider, "workers-ai");
    assert.match(warned, /DEEPSEEK_API_KEY/);
  } finally { console.warn = warn; }
});

test("chat provider: DeepSeek gets Workers AI as its backup, never the other way round", () => {
  const base = { CF_ACCOUNT_ID: "acc", CLOUDFLARE_API_TOKEN: "cf-token" };
  const ds = chatEnv({ ...base, CHAT_PROVIDER: "deepseek", DEEPSEEK_API_KEY: "k" }).vars;
  assert.equal(ds.BILLRAG_FALLBACK_MODEL, "@cf/openai/gpt-oss-120b");
  assert.equal(ds.BILLRAG_FALLBACK_BASE_URL, "https://api.cloudflare.com/client/v4/accounts/acc/ai/v1");
  assert.equal(ds.BILLRAG_FALLBACK_API_KEY, "cf-token");
  assert.equal(chatEnv({ ...base, CHAT_PROVIDER: "deepseek", DEEPSEEK_API_KEY: "k", FALLBACK_MODEL: "@cf/x/y" }).vars.BILLRAG_FALLBACK_MODEL, "@cf/x/y");
  assert.equal(chatEnv(base).vars.BILLRAG_FALLBACK_MODEL, undefined);
  assert.equal(chatEnv({ CF_ACCOUNT_ID: "acc", CHAT_PROVIDER: "deepseek", DEEPSEEK_API_KEY: "k" }).vars.BILLRAG_FALLBACK_MODEL, undefined);
});

test("privacy page: open without signing in, on GET only, per service, and the contact is optional and escaped", async () => {
  const { privacyPage } = await import("../src/privacy.js");
  const env = { AUTH: { get: async () => null, put: async () => {}, delete: async () => {} }, ALLOWED_EMAILS: "", ASSETS: { fetch: async () => new Response("page") } };
  const h = createHandler({ privacy: "bills" });
  const r = await h(new Request("https://bills.test/privacy"), env);
  assert.equal(r.status, 200);
  const text = await r.text();
  for (const fact of ["DeepSeek", "Cloudflare", "Google Fonts", "Turnstile", "no fixed deletion date", "30 days", "Bill Assistant"]) assert.ok(text.includes(fact), fact);
  assert.ok(!text.includes("mailto:"));
  assert.notEqual((await h(new Request("https://bills.test/privacy", { method: "POST" }), env)).status, 200);
  assert.notEqual((await createHandler({})(new Request("https://bills.test/privacy"), env)).status, 200); // a Worker that did not ask for one
  const proc = privacyPage("procurement");
  assert.ok(proc.includes("Procurement Rules") && !proc.includes("Turnstile") && !proc.includes("free questions"));
  assert.ok(privacyPage("bills", 'a"<b>@x.lk').includes("a&quot;&lt;b&gt;@x.lk"));
});

test("service pages have no Sinhala or Tamil text, and link to the privacy page", async () => {
  const { readFileSync } = await import("node:fs");
  const { LOGIN_PAGE } = await import("../src/auth.js");
  const { privacyPage } = await import("../src/privacy.js");
  const pages = { chat: readFileSync("public/index.html", "utf-8"), procurement: readFileSync("workers/procurement/public/index.html", "utf-8"), login: LOGIN_PAGE,
    bills: privacyPage("bills"), proc: privacyPage("procurement") };
  for (const [name, html] of Object.entries(pages)) assert.ok(!/[඀-෿஀-௿]/.test(html), name);
  for (const name of ["chat", "procurement", "login"]) assert.ok(pages[name].includes('href="/privacy"'), name);
});
