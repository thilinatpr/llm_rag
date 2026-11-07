import test from "node:test";
import assert from "node:assert/strict";
import { cleanEvent, countEvents, counted, EV_SCRIPT, readBatch, signPage, siteOf } from "../src/events.js";
import { createAdmin } from "../src/admin.js";
import { summarize, visitors } from "../src/admin-visitors.js";
import home from "../workers/home/worker.js";

const KEY = "k-secret-key-123456";
const NOW = Date.parse("2026-11-10T08:00:00Z");
const points = [];
const env = (extra = {}) => ({ EV_KEY: KEY, EVENTS: { writeDataPoint: (p) => points.push(p) }, ASSETS: { fetch: async () => new Response("<html><head></head><body>hi</body></html>", { headers: { "Content-Type": "text/html" } }) }, ...extra });
const post = (body, host = "citizenlk.com", headers = {}) => new Request(`https://${host}/e`, { method: "POST", body: typeof body === "string" ? body : JSON.stringify(body), headers: { "user-agent": "Mozilla/5.0 (iPhone)", ...headers } });

test("the script is plain text that cannot end its own tag and parses", () => {
  assert.ok(!EV_SCRIPT.includes("</script"));
  new Function(EV_SCRIPT);
});

test("a served page carries a token for its own host and path only", async () => {
  const e = env();
  const r = await counted(await e.ASSETS.fetch(), new Request("https://citizenlk.com/"), e);
  const html = await r.text();
  const token = /name="clk-ev" content="([0-9a-f]{32})"/.exec(html)[1];
  assert.equal(token, await signPage(KEY, "home", "/"));
  assert.notEqual(token, await signPage(KEY, "bills", "/"));
  assert.ok(html.includes("<script>(function(){"));
  for (const [url, why] of [["https://citizenlk.com/other", "path"], ["https://example.com/", "host"]]) {
    assert.equal(await (await counted(await e.ASSETS.fetch(), new Request(url), e)).text(), "<html><head></head><body>hi</body></html>", why);
  }
  const noKey = env({ EV_KEY: undefined });
  assert.ok(!(await (await counted(await noKey.ASSETS.fetch(), new Request("https://citizenlk.com/"), noKey)).text()).includes("clk-ev"));
  const json = await counted(new Response("{}", { headers: { "Content-Type": "application/json" } }), new Request("https://citizenlk.com/"), e);
  assert.equal(await json.text(), "{}");
});

test("events outside the fixed lists are dropped, free text only where allowed", () => {
  assert.deepEqual(cleanEvent({ ev: "nav", a: "new", b: "out" }), { ev: "nav", a: "new", b: "out" });
  assert.equal(cleanEvent({ ev: "nav", a: "new", b: "elsewhere" }), null);
  assert.equal(cleanEvent({ ev: "question", a: "what is the penalty", b: "" }), null);
  assert.equal(cleanEvent({ ev: "constructor", a: "", b: "" }), null);
  assert.equal(cleanEvent({ ev: "from", a: "someone@example.com", b: "" }), null);
  assert.equal(cleanEvent({ ev: "from", a: "chatgpt", b: "chatgpt.com" }).b, "chatgpt.com");
  assert.equal(cleanEvent({ ev: "from", a: "", b: "evil.com/path?x=1" }), null);
});

test("a valid batch is written with the site from the host and no IP or user agent", async () => {
  points.length = 0;
  const token = await signPage(KEY, "bills", "/");
  const r = await countEvents(post({ p: "/", t: token, pv: "abc12345xy", e: [{ ev: "view" }, { ev: "nav", a: "new", b: "out" }, { ev: "bogus" }] }, "bills.citizenlk.com", { "cf-connecting-ip": "1.2.3.4" }), env());
  assert.equal(r.status, 204);
  assert.equal(points.length, 2);
  assert.deepEqual(points[0].blobs.slice(0, 3), ["view", "bills", "main"]);
  const flat = JSON.stringify(points);
  assert.ok(!flat.includes("1.2.3.4") && !flat.includes("iPhone"));
});

test("bad tokens, other hosts, bots, big bodies and Global Privacy Control count nothing real", async () => {
  const good = { p: "/", t: await signPage(KEY, "home", "/"), pv: "abc12345xy", e: [{ ev: "view" }] };
  const reasons = async (req) => { points.length = 0; await countEvents(req, env()); return points.map((p) => `${p.indexes[0]}:${p.blobs[4]}`); };
  assert.deepEqual(await reasons(post({ ...good, t: "0".repeat(32) })), ["drop:token"]);
  assert.deepEqual(await reasons(post(good, "bills.citizenlk.com")), ["drop:token"]); // a home token on another host
  assert.deepEqual(await reasons(post(good, "citizenlk.com", { "user-agent": "curl/8" })), ["drop:ua"]);
  assert.deepEqual(await reasons(post({ ...good, p: "/admin" })), ["drop:path"]);
  assert.deepEqual(await reasons(post("x".repeat(5000))), ["drop:size"]);
  assert.deepEqual(await reasons(post(good, "citizenlk.com", { "sec-gpc": "1" })), []);
  assert.deepEqual(await reasons(post(good, "evil.example")), []);
  assert.equal((await countEvents(new Request("https://citizenlk.com/e"), env())).status, 405);
  assert.equal(await readBatch(KEY, "home", JSON.stringify({ ...good, pv: "bad pv!" })).then((r) => r.why), "pv");
  assert.equal(siteOf("bills.citizenlk.com"), "bills");
});

test("the homepage Worker counts and serves the counter; the old behaviour stays", async () => {
  points.length = 0;
  const e = env();
  const page = await home.fetch(new Request("https://citizenlk.com/"), e);
  assert.ok((await page.text()).includes("clk-ev"));
  assert.equal(page.headers.get("X-Content-Type-Options"), "nosniff");
  const token = await signPage(KEY, "home", "/");
  assert.equal((await home.fetch(post({ p: "/", t: token, pv: "abc12345xy", e: [{ ev: "go", a: "bills" }] }), e)).status, 204);
  assert.equal(points.length, 1);
});

const rows = {
  daily: [{ hour: "2026-11-09 20:00:00", site: "home", n: "4" }, { hour: "2026-11-10 08:00:00", site: "bills", n: "3" }],
  dailyVisits: [{ hour: "2026-11-10 08:00:00", site: "home", n: "2" }],
  events: [
    { ev: "view", site: "home", a: "", b: "", n: "4" }, { ev: "view", site: "bills", a: "", b: "", n: "3" },
    { ev: "nav", site: "home", a: "new", b: "out", n: "2" }, { ev: "nav", site: "home", a: "reload", b: "in", n: "1" },
    { ev: "from", site: "home", a: "", b: "chatgpt.com", n: "2" }, { ev: "go", site: "home", a: "bills", b: "", n: "2" },
    { ev: "scroll", site: "home", a: "50", b: "", n: "1" }, { ev: "drop", site: "home", a: "ua", b: "", n: "5" }, { ev: "skip", site: "home", a: "noint", b: "", n: "2" },
  ],
  countries: [{ site: "home", country: "LK", n: "6" }, { site: "bills", country: "XX", n: "1" }],
  prev: [{ n: "5" }],
};

test("summarize folds hours into Sri Lanka days and adds up the sites", () => {
  const s = summarize(rows, 30);
  assert.equal(s.views, 7);
  assert.equal(s.visits, 2);
  // 20:00 UTC on the 9th is 01:30 on the 10th in Sri Lanka, so both rows are one day.
  assert.deepEqual(s.daily.map((d) => [d.day, d.total]), [["2026-11-10", 7]]);
  assert.deepEqual(s.bySite, { home: { views: 4, visits: 2 }, bills: { views: 3, visits: 0 } });
  assert.deepEqual(s.referrers, { "chatgpt.com": 2 });
  assert.equal(s.droppedTotal, 7);
  assert.equal(s.prev, 5);
});

const adminEnv = (extra = {}) => {
  const m = new Map();
  return { AUTH: { get: async (k) => (m.has(k) ? JSON.parse(m.get(k)) : null), put: async (k, v) => void m.set(k, v), delete: async () => {} }, BILLS_KV: { get: async () => null, put: async () => {} }, LOGS: { get: async () => null, put: async () => {}, head: async () => null, list: async () => ({ objects: [], truncated: false }) }, QUOTA: null, ...extra };
};
const fakeFetch = (log = []) => async (url, init) => {
  log.push(init.body);
  const q = init.body;
  const key = q.includes("blob1 = 'nav'") ? "dailyVisits" : q.includes("GROUP BY ev") ? "events" : q.includes("blob4 AS country") ? "countries" : q.includes("<= NOW()") ? "prev" : "daily";
  return new Response(JSON.stringify({ data: rows[key] }), { status: 200 });
};

test("the Visitors page: setup hint without a token, numbers with one, cached for a short time", async () => {
  const bare = adminEnv();
  const page = createAdmin({ now: () => NOW });
  const r1 = await page(new Request("https://admin.citizenlk.com/admin/source/visitors"), bare, "me@example.com");
  assert.equal(r1.status, 200);
  assert.ok((await r1.text()).includes("CF_API_TOKEN"));

  const log = [];
  const e = adminEnv({ CF_API_TOKEN: "t", CF_ACCOUNT_ID: "acc" });
  const admin = createAdmin({ now: () => NOW, fetchFn: fakeFetch(log) });
  const html = await (await admin(new Request("https://admin.citizenlk.com/admin/source/visitors?days=7"), e, "me@example.com")).text();
  for (const want of ["chatgpt.com", "Bill Assistant", "citizenlk.com", "<b>7</b> page views", "Went on to Bill Assistant", "No cookie"]) assert.ok(html.toLowerCase().includes(want.toLowerCase()), want);
  assert.ok(log.every((q) => q.includes("INTERVAL '7' DAY") || q.includes("INTERVAL '14' DAY")));
  const n = log.length;
  await admin(new Request("https://admin.citizenlk.com/admin/source/visitors?days=7"), e, "me@example.com");
  assert.equal(log.length, n, "second view comes from the cache");
});

test("a hostile referrer host cannot break out of the page, and a failing API is shown, not hidden", async () => {
  const evil = { ...rows, events: [...rows.events, { ev: "from", site: "home", a: "", b: "<script>alert(1)</script>", n: "9" }] };
  const e = adminEnv({ CF_API_TOKEN: "t", CF_ACCOUNT_ID: "acc" });
  const admin = createAdmin({ now: () => NOW, fetchFn: async (u, i) => new Response(JSON.stringify({ data: (evil[i.body.includes("GROUP BY ev") ? "events" : "daily"]) }), { status: 200 }) });
  const html = await (await admin(new Request("https://admin.citizenlk.com/admin/source/visitors"), e, "me@example.com")).text();
  assert.ok(!html.includes("<script>alert(1)"));
  const down = createAdmin({ now: () => NOW, fetchFn: async () => new Response("denied", { status: 403 }) });
  const v = await visitors(adminEnv({ CF_API_TOKEN: "t", CF_ACCOUNT_ID: "acc" }), 30, { fetchFn: async () => new Response("denied", { status: 403 }) });
  assert.match(v.error, /403/);
  assert.ok((await (await down(new Request("https://admin.citizenlk.com/admin/source/visitors"), adminEnv({ CF_API_TOKEN: "t", CF_ACCOUNT_ID: "acc" }), "me@example.com")).text()).includes("Could not read the counts"));
});

test("the services' handler takes the beacon without a sign-in and leaves other routes gated", async () => {
  const { createHandler } = await import("../src/auth.js");
  points.length = 0;
  const handler = createHandler({});
  const e = env({ OTP_PEPPER: KEY, EV_KEY: undefined, AUTH: { get: async () => null } });
  const token = await signPage(KEY, "bills", "/");
  const r = await handler(post({ p: "/", t: token, pv: "abc12345xy", e: [{ ev: "view" }] }, "bills.citizenlk.com"), e);
  assert.equal(r.status, 204);
  assert.equal(points.length, 1);
  const gated = await handler(new Request("https://bills.citizenlk.com/", { headers: { Accept: "text/html" } }), e);
  assert.equal(gated.status, 302);
});
