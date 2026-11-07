import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createAdmin, esc, snapshot, todoItems } from "../src/admin.js";
import { addRecord, emptyDay, lastDayProblems, listQuestions, loadDay, problemsOf, quantile, utcDay, validKey } from "../src/admin-stats.js";
import { createHandler } from "../src/auth.js";
import { reportSettings } from "../src/public.js";
import adminWorker from "../workers/admin/worker.js";
import { checkBalance } from "../src/balance.js";
import { Counters, memoryStore, slDay } from "../src/quota.js";

const NOW = Date.parse("2026-11-07T12:00:00Z");
const DAY = "2026-11-07";

// An R2 bucket in memory, counting calls: list in key order, startAfter, limit, cursor.
function bucket(objects = {}) {
  const m = new Map(Object.entries(objects));
  const b = {
    m, gets: 0, puts: 0, lists: 0,
    async get(k) { b.gets++; if (!m.has(k)) return null; const v = m.get(k); return { json: async () => JSON.parse(typeof v === "string" ? v : JSON.stringify(v)) }; },
    async put(k, v) { b.puts++; m.set(k, v); },
    async head(k) { return m.has(k) ? { size: 201e6, uploaded: new Date(NOW - 2 * 864e5) } : null; },
    async list({ prefix, startAfter, limit = 1000, cursor }) {
      b.lists++;
      const all = [...m.keys()].filter((k) => k.startsWith(prefix) && (!startAfter || k > startAfter) && (!cursor || k > cursor)).sort();
      const page = all.slice(0, limit);
      return { objects: page.map((key) => ({ key })), truncated: all.length > limit, cursor: page[page.length - 1] };
    },
  };
  return b;
}
function kv(init = {}) {
  const m = new Map(Object.entries(init));
  return { m, get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k) };
}
const ask = (extra = {}) => ({ ts: `${DAY}T10:00:00.000Z`, type: "ask", question: "q", answer: "a", seconds: 10, evidence: [{ bill: "070/2026", section: "3", score: 3 }], queries: ["q"], enough: true, warnings: [], ...extra });
const logKey = (hhmmss, day = DAY, prefix = "chatlogs") => `${prefix}/${day}/${hhmmss}000-abc-1.json`;

const SETTINGS = { publicWanted: true, publicWorks: true, proxySecret: true, limits: { cookie: 10, ip: 300, global: 500, issue: 50 } };
const settings = (o = {}) => ({ "admin:settings": JSON.stringify({ at: new Date(NOW).toISOString(), ...SETTINGS, ...o }) });

function setup({ logs = {}, used = 0, user = "me@example.com", extra = {}, kvInit = settings() } = {}) {
  const store = new Counters(memoryStore());
  if (used) for (let i = 0; i < used; i++) store.take([{ k: `g|${slDay(NOW)}`, limit: 500, ttl: 86400, tag: "global" }], NOW / 1000);
  const LOGS = bucket({ "bills/index.sqlite": "x", "bills/vectors.npz": "x", ...logs });
  const env = {
    AUTH: kv(), BILLS_KV: kv(kvInit), LOGS, DEEPSEEK_API_KEY: "k", ALERT_EMAIL: "me@example.com",
    QUOTA: { idFromName: () => "main", get: () => ({ peek: async (items) => store.peek(items, NOW / 1000) }) },
    ...extra,
  };
  const states = [];
  const containerState = async (_e, binding) => { states.push(binding); return { status: "stopped", lastChange: NOW - 3600e3 }; };
  const admin = createAdmin({ containerState, now: () => NOW });
  const call = (path, init = {}) => admin(new Request(`https://bills.test${path}`, { headers: init.method === "POST" ? { Origin: "https://bills.test", ...init.headers } : init.headers, method: init.method }), env, user);
  return { env, call, states, LOGS };
}
const text = (r) => r.text();

// --- who gets in ---
test("signed out goes to the login; actions need a session", async () => {
  const out = await setup({ user: null }).call("/admin");
  assert.equal(out.status, 302);
  assert.match(out.headers.get("Location"), /\/login$/);
  assert.equal((await setup({ user: null }).call("/admin/public-pause", { method: "POST" })).status, 401);
});

test("the admin Worker serves sign-in and the panel, and only ALLOWED_EMAILS can have a session", async () => {
  const sid = "a".repeat(64);
  const env = { AUTH: kv({ [`session:${sid}`]: "me@example.com", [`session:${"b".repeat(64)}`]: "gone@example.com" }), BILLS_KV: kv(settings()), LOGS: bucket({}), ALLOWED_EMAILS: "me@example.com" };
  const call = (p, s = sid) => adminWorker.fetch(new Request(`https://admin.test${p}`, { headers: s ? { Cookie: `session=${s}` } : {} }), env);
  assert.match(await (await call("/login")).text(), /Sign in to the admin panel/);
  assert.equal((await call("/")).headers.get("Location"), "https://admin.test/admin");
  assert.equal((await call("/admin")).status, 200);
  for (const p of ["/api/ask", "/api/search", "/healthz", "/public/me", "/index.html"]) assert.equal((await call(p)).status, 404, p);
  for (const s of [null, "b".repeat(64)]) assert.match((await call("/admin", s)).headers.get("Location"), /\/login$/);
  env.ALLOWED_EMAILS = "";
  assert.match((await call("/admin")).headers.get("Location"), /\/login$/, "empty list = nobody");
});

test("Bill Assistant's Worker has no panel any more: /admin is an ordinary missing page", async () => {
  const worker = readFileSync(new URL("../src/worker.js", import.meta.url), "utf8"), toml = readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.doesNotMatch(worker, /admin/i);
  for (const gone of ["r2_buckets", "citizenlk-procurement", "admin.citizenlk.com", "ADMIN_"]) assert.ok(!toml.includes(gone), gone);
  const handler = createHandler({ upstream: async () => new Response("container") });
  const env = { AUTH: kv({ [`session:${"a".repeat(64)}`]: "me@example.com" }), ALLOWED_EMAILS: "me@example.com", BILLRAG: {}, PROXY_SECRET: "s".repeat(16), ASSETS: { fetch: async () => new Response("no", { status: 404 }) } };
  assert.equal((await handler(new Request("https://bills.test/admin", { headers: { Cookie: `session=${"a".repeat(64)}` } }), env)).status, 404);
});

test("the credit alert settings match in both Workers, since Check now runs the same check", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  for (const k of ["ALERT_EMAIL", "BALANCE_ALERT_USD", "FROM_ADDRESS", "CF_ACCOUNT_ID"]) {
    const val = (t) => new RegExp(`^${k} = (.*)$`, "m").exec(t)?.[1];
    assert.ok(val(read("../wrangler.toml")), k);
    assert.equal(val(read("../workers/admin/wrangler.toml")), val(read("../wrangler.toml")), k);
  }
});

test("Bill Assistant reports its settings, never a secret's value", async () => {
  const AUTH = kv();
  await reportSettings({ AUTH, PUBLIC_CHAT: "on", PROXY_SECRET: "topsecretvalue", TURNSTILE_SECRET: "alsosecret", PUBLIC_DAILY_GLOBAL: "400" }, NOW);
  const raw = AUTH.m.get("admin:settings"), got = JSON.parse(raw);
  assert.deepEqual([got.publicWanted, got.publicWorks, got.proxySecret, got.limits.global], [true, false, true, 400]);
  assert.ok(!raw.includes("secret"));
});

test("actions need a same-site Origin", async () => {
  const t = setup();
  const r = await t.call("/admin/public-pause", { method: "POST", headers: { Origin: "https://evil.test" } });
  assert.equal(r.status, 403);
  assert.equal(await t.env.BILLS_KV.get("admin:public-paused"), null);
});

// --- nothing wakes a container, nothing leaks ---
test("home works, reads state only and has a strict CSP", async () => {
  const t = setup({ logs: { [logKey("100000")]: ask() } });
  const r = await t.call("/admin");
  assert.equal(r.status, 200);
  const csp = r.headers.get("Content-Security-Policy");
  assert.match(csp, /script-src 'nonce-[0-9a-f]+'/);
  assert.doesNotMatch(csp, /unsafe/);
  assert.match(r.headers.get("Cache-Control"), /no-store/);
  const html = await text(r);
  assert.match(html, /Today 1 question /);
  assert.deepEqual(t.states.sort(), ["BILLRAG", "PROCUREMENT"]);
});

test("a visitor's question cannot inject HTML into the panel", async () => {
  const evil = `<script>alert(1)</script><img src=x onerror=alert(2)>`;
  const t = setup({ logs: { [logKey("100000")]: ask({ question: evil, answer: evil, warnings: [evil], evidence: [{ bill: evil, section: evil, score: 1 }] }) } });
  for (const path of [`/admin/questions?s=bills&day=${DAY}`, `/admin/q/${logKey("100000")}`]) {
    const html = await text(await t.call(path));
    assert.ok(html.includes("&lt;script&gt;alert(1)"), path);
    assert.ok(!html.includes(evil), path);
  }
  assert.equal(esc(`"'&`), "&quot;&#39;&amp;");
});

test("rollups hold counts and keys, never the words", async () => {
  const t = setup({ logs: { [logKey("100000")]: ask({ question: "PRIVATE WORDS", error: "boom", answer: "PRIVATE ANSWER" }) } });
  await loadDay(t.env, "bills", DAY, { now: NOW });
  const saved = t.LOGS.m.get(`admin-rollups/bills/${DAY}.json`);
  assert.ok(saved);
  assert.ok(!saved.includes("PRIVATE"));
});

test("question pages refuse keys outside the logs", async () => {
  assert.equal(validKey("chatlogs/2026-11-07/100000000-a-1.json"), true);
  for (const k of ["bills/index.sqlite", "admin-rollups/bills/2026-11-07.json", "chatlogs/../bills/x.json", "chatlogs/2026-11-07/a/../../x.json"]) assert.equal(validKey(k), false, k);
  const t = setup();
  assert.equal((await t.call("/admin/q/bills%2Findex.sqlite")).status, 404);
  assert.equal((await t.call("/admin/q/%E0%A4%A")).status, 404);
});

// --- counting ---
test("problemsOf: what counts as what", () => {
  assert.deepEqual(problemsOf(ask()), []);
  assert.deepEqual(problemsOf(ask({ error: "x" })), ["error"]);
  assert.deepEqual(problemsOf(ask({ evidence: [] })), ["nothing"]);
  assert.deepEqual(problemsOf(ask({ evidence: [], queries: [] })), [], "a greeting searched nothing, so it did not find nothing");
  assert.deepEqual(problemsOf(ask({ enough: false })), ["insufficient"]);
  assert.deepEqual(problemsOf(ask({ warnings: ["w"], fallback: { calls: 1 }, seconds: 61 })), ["warned", "backup", "slow"]);
  assert.deepEqual(problemsOf({ type: "ask", question: "q" }), [], "an old record without the fields is not a problem");
  // Procurement's records: sources + ms + status
  assert.deepEqual(problemsOf({ ms: 70000, sources: [], answer: "none found", warnings: [], status: 200 }), ["nothing", "slow"]);
  assert.deepEqual(problemsOf({ ms: 10, sources: [], answer: null, status: 502, error: "down" }), ["error"]);
});

test("addRecord: searches apart, cost only from priced calls, milliseconds become seconds", () => {
  const s = emptyDay(DAY);
  addRecord(s, "a", { type: "search" });
  addRecord(s, "b", ask({ lane: "public", cost: { usd: 0.5, priced_calls: 2 }, build: "abc", usage: { peak_rss_mb: 300 } }));
  addRecord(s, "c", ask({ cost: { usd: 9, priced_calls: 0 } }));
  addRecord(s, "d", { ms: 1500, sources: ["x"], answer: "y" });
  assert.deepEqual([s.records, s.searches, s.asks, s.public, s.usd, s.priced, s.peakRss, s.build], [4, 1, 3, 1, 0.5, 1, 300, "abc"]);
  assert.deepEqual(s.secs, [10, 10, 1.5]);
});

test("loadDay reads each log once, and only what is new", async () => {
  const t = setup({ logs: { [logKey("100000")]: ask(), [logKey("100100")]: ask() } });
  assert.equal((await loadDay(t.env, "bills", DAY, { now: NOW })).asks, 2);
  const gets = t.LOGS.gets;
  assert.equal((await loadDay(t.env, "bills", DAY, { now: NOW })).asks, 2);
  assert.equal(t.LOGS.gets - gets, 1, "only the rollup itself");
  t.LOGS.m.set(logKey("100200"), ask());
  assert.equal((await loadDay(t.env, "bills", DAY, { now: NOW })).asks, 3);
});

test("the last minute is left for later: a log written just after its key's time is not skipped for good", async () => {
  const t = setup({ logs: { [logKey("115959")]: ask() } });
  const fresh = await loadDay(t.env, "bills", DAY, { now: NOW });
  assert.equal(fresh.asks, 0);
  assert.equal((await loadDay(t.env, "bills", DAY, { now: NOW + 120_000 })).asks, 1);
});

test("the budget bounds a load; the rest is counted later and the day says it is behind", async () => {
  const logs = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [logKey(`10${String(i).padStart(2, "0")}00`), ask()]));
  const t = setup({ logs });
  const budget = { left: 11 };
  const first = await loadDay(t.env, "bills", DAY, { now: NOW, budget });
  assert.equal(first.asks, 10);
  assert.equal(first.behind, true);
  assert.equal(budget.left, 0);
  const rest = await loadDay(t.env, "bills", DAY, { now: NOW });
  assert.equal(rest.asks, 30);
  assert.equal(rest.behind, false);
});

test("a day that is over is closed and costs one read from then on; today and yesterday stay open", async () => {
  const old = "2026-11-01";
  const t = setup({ logs: { [logKey("100000", old)]: ask() } });
  const s = await loadDay(t.env, "bills", old, { now: NOW });
  assert.equal(s.done, true);
  const lists = t.LOGS.lists;
  await loadDay(t.env, "bills", old, { now: NOW });
  assert.equal(t.LOGS.lists, lists);
  assert.equal((await loadDay(t.env, "bills", DAY, { now: NOW })).done, false);
  assert.equal((await loadDay(t.env, "bills", utcDay(NOW, 1), { now: NOW })).done, false);
});

test("an unreadable log is counted as bad, and does not stop the rest", async () => {
  const t = setup({ logs: { [logKey("100000")]: "not json", [logKey("100100")]: ask() } });
  const s = await loadDay(t.env, "bills", DAY, { now: NOW });
  assert.deepEqual([s.bad, s.asks], [1, 1]);
});

test("listQuestions: newest first, paged, filtered from the rollup", async () => {
  const logs = Object.fromEntries(Array.from({ length: 30 }, (_, i) => [logKey(`10${String(i).padStart(2, "0")}00`), ask({ question: `q${i}`, error: i === 3 ? "x" : null })]));
  const t = setup({ logs });
  const a = await listQuestions(t.env, "bills", DAY, { now: NOW });
  assert.equal(a.rows.length, 25);
  assert.equal(a.rows[0].rec.question, "q29");
  const b = await listQuestions(t.env, "bills", DAY, { before: a.next, now: NOW });
  assert.equal(b.rows.length, 5);
  assert.equal(b.next, null);
  const only = await listQuestions(t.env, "bills", DAY, { only: "error", now: NOW });
  assert.deepEqual(only.rows.map((r) => r.rec.question), ["q3"]);
});

test("quantile and the 24 h window", () => {
  assert.equal(quantile([], 0.5), null);
  assert.equal(quantile([3, 1, 2], 0.5), 2);
  const p = lastDayProblems([{ problems: [{ k: "a", t: new Date(NOW - 3600e3).toISOString(), w: ["error"] }, { k: "b", t: new Date(NOW - 25 * 3600e3).toISOString(), w: ["error"] }] }], NOW);
  assert.deepEqual(p.error.map((x) => x.k), ["a"]);
});

// --- To do ---
const titles = (items) => items.map((x) => `${x.tier}: ${x.title}`);
const balanceOk = { ...settings(), "admin:balance": JSON.stringify({ state: "ok", balance: 12, currency: "USD", limit: 5, at: new Date(NOW - 1800e3).toISOString() }) };
const items = async (opts) => { const t = setup(opts); return todoItems(await snapshot(t.env, { now: NOW, containerState: async () => ({ status: "stopped", lastChange: NOW }) })); };

test("all well: nothing to do", async () => {
  assert.deepEqual(titles(await items({ kvInit: balanceOk })), []);
});

test("failures, warnings and the backup model are listed, worst first, with a button to read them", async () => {
  const list = await items({ kvInit: balanceOk, logs: {
    [logKey("100000")]: ask({ fallback: { calls: 1 } }), [logKey("100100")]: ask({ warnings: ["w"] }), [logKey("100200")]: ask({ error: "x" }), [logKey("100300")]: ask({ error: "y" }) } });
  assert.deepEqual(titles(list), [
    "people: 2 questions failed on Bill Assistant, last 24 h",
    "you: 1 answer with a warning on Bill Assistant, last 24 h",
    "watch: 1 question answered by the backup model on Bill Assistant, last 24 h"]);
  assert.match(list[0].actions[0].href, /only=error/);
});

test("credit: low waits for you with Top up; empty affects people; a failing check; never checked; stale", async () => {
  const at = new Date(NOW).toISOString();
  const with_ = (o) => ({ kvInit: { ...settings(), "admin:balance": JSON.stringify({ at, ...o }) } });
  const low = await items(with_({ state: "low", balance: 4.2, currency: "USD", limit: 5 }));
  assert.deepEqual(titles(low), ["you: DeepSeek credit is low: 4.2 USD"]);
  assert.equal(low[0].actions[0].label, "Top up");
  assert.match(titles(await items(with_({ state: "low", balance: 0, currency: "USD", limit: 5 })))[0], /^people: DeepSeek credit is used up/);
  assert.match(titles(await items(with_({ state: "check_failed", failures: 1, error: "e" })))[0], /^watch: The credit check failed 1 time/);
  assert.match(titles(await items(with_({ state: "check_failed", failures: 3, error: "e" })))[0], /^you: The credit check failed 3 times/);
  assert.match(titles(await items({}))[0], /has not been checked/);
  assert.match(titles(await items({ kvInit: { ...settings(), "admin:balance": JSON.stringify({ state: "ok", balance: 9, at: new Date(NOW - 4 * 3600e3).toISOString() }) } }))[0], /last checked 4 h ago/);
});

test("public questions: near the cap, at the cap, paused, broken config", async () => {
  assert.deepEqual(titles(await items({ kvInit: balanceOk, used: 400 })), ["watch: 400 of 500 free questions used today"]);
  assert.match(titles(await items({ kvInit: balanceOk, used: 500 }))[0], /^people: All 500 free questions/);
  const paused = await items({ kvInit: { ...balanceOk, "admin:public-paused": JSON.stringify({ at: new Date(NOW).toISOString(), by: "me" }) }, used: 500 });
  assert.deepEqual(titles(paused), ["you: Public questions are paused"]);
  assert.equal(paused[0].actions[0].post, "/admin/public-resume");
  assert.match(titles(await items({ kvInit: { ...balanceOk, ...settings({ publicWorks: false }) } }))[0], /^people: Public questions are on, but/);
  assert.deepEqual(titles(await items({ kvInit: { ...balanceOk, ...settings({ publicWanted: false, publicWorks: false }) }, used: 500 })), []);
});

test("index: missing affects people, two weeks old is watched", async () => {
  const t = setup({ kvInit: balanceOk });
  t.LOGS.m.delete("bills/vectors.npz");
  const s = await snapshot(t.env, { now: NOW, containerState: async () => ({ status: "stopped", lastChange: NOW }) });
  assert.match(titles(todoItems(s))[0], /^people: The index files are missing/);
  const old = await snapshot(setup({ kvInit: balanceOk }).env, { now: NOW + 13 * 864e5, containerState: async () => ({ status: "stopped", lastChange: NOW }) });
  assert.ok(titles(todoItems(old)).some((x) => /^watch: The bill index was last published 15 days ago/.test(x)));
});

test("containers: awake 6 h is watched, a bad exit is watched, unreadable is said", async () => {
  const run = async (state) => titles(todoItems(await snapshot(setup({ kvInit: balanceOk }).env, { now: NOW, containerState: state })));
  assert.deepEqual(await run(async () => ({ status: "healthy", lastChange: NOW - 3600e3 })), []);
  assert.equal((await run(async (_e, b) => (b === "BILLRAG" ? { status: "healthy", lastChange: NOW - 7 * 3600e3 } : { status: "stopped", lastChange: NOW })))[0], "watch: Bill Assistant container has been awake for 7 h");
  assert.match((await run(async (_e, b) => (b === "BILLRAG" ? { status: "stopped_with_code", exitCode: 137, lastChange: NOW } : { status: "stopped", lastChange: NOW })))[0], /exit code 137/);
  assert.match((await run(async () => { throw new Error("nope"); }))[0], /Cannot read the Bill Assistant container/);
});

test("a part that cannot be read says so instead of looking fine", async () => {
  const t = setup({ kvInit: balanceOk });
  t.LOGS.list = async () => { throw new Error("R2 down"); };
  const s = await snapshot(t.env, { now: NOW, containerState: async () => ({ status: "stopped", lastChange: NOW }) });
  assert.ok(titles(todoItems(s)).includes("people: Cannot read the Bill Assistant logs"));
  assert.ok(titles(todoItems(await snapshot({ ...t.env, BILLS_KV: kv() }, { now: NOW }))).includes("watch: Bill Assistant has not reported its settings yet"));
  const noLogs = await snapshot({ ...t.env, LOGS: undefined }, { now: NOW });
  assert.ok(titles(todoItems(noLogs)).includes("people: Cannot read the Procurement Rules logs"));
});

// --- buttons ---
test("pause: a visitor is shut out, a member is not, the audit log says who; resume undoes it", async () => {
  const t = setup({ kvInit: balanceOk });
  const r = await t.call("/admin/public-pause", { method: "POST", headers: { Referer: "https://bills.test/admin/source/public" } });
  assert.equal(r.status, 303);
  assert.match(r.headers.get("Location"), /^\/admin\/source\/public\?done=/);
  assert.match(await text(await t.call("/admin/audit")), /me@example.com[\s\S]*Paused public questions/);

  const handler = createHandler({ upstream: async () => new Response("container"), now: () => NOW });
  // Bill Assistant's own view of the same KV the panel wrote to
  const env = { ...t.env, AUTH: t.env.BILLS_KV, PROXY_SECRET: "s".repeat(16), PUBLIC_CHAT: "on", TURNSTILE_SECRET: "t", TURNSTILE_SITEKEY: "k", OTP_PEPPER: "p", BILLRAG: {},
    ALLOWED_EMAILS: "me@example.com", ASSETS: { fetch: async () => new Response("page") } };
  const me = () => handler(new Request("https://bills.test/public/me"), env);
  assert.equal((await (await me()).json()).enabled, false);
  assert.equal((await handler(new Request("https://bills.test/", { headers: { Accept: "text/html" } }), env)).status, 302);

  await t.call("/admin/public-resume", { method: "POST" });
  assert.equal((await (await me()).json()).enabled, true);
});

test("a Referer elsewhere does not steer the redirect; an unknown action is 404", async () => {
  const t = setup();
  const r = await t.call("/admin/public-pause", { method: "POST", headers: { Referer: "https://evil.test/admin" } });
  assert.match(r.headers.get("Location"), /^\/admin\?done=/);
  assert.equal((await t.call("/admin/constructor", { method: "POST" })).status, 404);
});

test("the credit check records its result for the panel, without the mail fields, and remembers since when it has been low", async () => {
  const env = { AUTH: kv(), DEEPSEEK_API_KEY: "k", ALERT_EMAIL: "a@b.c", FROM_ADDRESS: "a@b.c", BALANCE_ALERT_USD: "5" };
  const reply = (n) => ({ fetchFn: async () => new Response(JSON.stringify({ balance_infos: [{ currency: "USD", total_balance: n }] })), send: async () => {} });
  await checkBalance(env, { ...reply("4"), nowIso: () => "2026-11-07T01:00:00.000Z" });
  await checkBalance(env, { ...reply("3"), nowIso: () => "2026-11-07T02:00:00.000Z" });
  const low = JSON.parse(env.AUTH.m.get("admin:balance"));
  assert.deepEqual([low.state, low.balance, low.lowSince, low.at, low.mailed], ["low", 3, "2026-11-07T01:00:00.000Z", "2026-11-07T02:00:00.000Z", undefined]);
  await checkBalance(env, { ...reply("9"), nowIso: () => "2026-11-07T03:00:00.000Z" });
  assert.equal(JSON.parse(env.AUTH.m.get("admin:balance")).lowSince, null);
});

test("settings: not reported, stale, or PROXY_SECRET missing", async () => {
  const of = async (o) => titles(await items({ kvInit: { ...balanceOk, ...o } }));
  assert.deepEqual(await of(settings({ at: new Date(NOW - 4 * 3600e3).toISOString() })), ["watch: Bill Assistant last reported its settings 4 h ago"]);
  assert.deepEqual(await of(settings({ proxySecret: false })), ["people: PROXY_SECRET is not set"]);
});

test("every page renders, also before Bill Assistant has reported its settings", async () => {
  const t = setup({ kvInit: balanceOk, logs: { [logKey("100000")]: ask({ calls: [{ kind: "chat", step: "answer", model: "m", s: 1, in: 2, out: 3 }], cost: { usd: 0.01, priced_calls: 1 } }) } });
  for (const p of ["/admin", "/admin/", "/admin/source/bills", "/admin/source/procurement", "/admin/source/money", "/admin/source/public", "/admin/source/containers", "/admin/source/index", "/admin/questions", "/admin/questions?s=procurement", "/admin/audit", `/admin/q/${logKey("100000")}`])
    assert.equal((await t.call(p)).status, 200, p);
  const fresh = setup({ kvInit: {} });
  assert.equal((await fresh.call("/admin/source/public")).status, 200);
  assert.equal((await fresh.call("/admin")).status, 200);
  assert.equal((await t.call("/admin/source/nope")).status, 404);
  assert.equal((await t.call("/admin/nope")).status, 404);
});
