import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createAdmin, snapshot, todoItems } from "../src/admin.js";
import { CHECK_CRON, MANIFEST_KEY, SKIPPED_KEY, STATUS_KEY, STAGING_PREFIX, STATE_KEY, detect, discard, listingKeys, parseListing, phaseOf, startRun } from "../src/refresh.js";
import adminWorker from "../workers/admin/worker.js";

const NOW = Date.parse("2026-11-08T03:00:00Z");
const iso = (ms) => new Date(ms).toISOString();

function kv(init = {}) {
  const m = new Map(Object.entries(init));
  return { m, get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k) };
}
function bucket(objects = {}) {
  const m = new Map(Object.entries(objects).map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]));
  return {
    m,
    async get(k) { return m.has(k) ? { json: async () => JSON.parse(m.get(k)) } : null; },
    async put(k, v) { m.set(k, v); },
    async head(k) { return m.has(k) ? { size: 1, uploaded: new Date(NOW - 864e5) } : null; },
    async delete(keys) { for (const k of [].concat(keys)) m.delete(k); },
    async list({ prefix }) { return { objects: [...m.keys()].filter((k) => k.startsWith(prefix)).map((key) => ({ key })), truncated: false }; },
  };
}

// documents.gov.lk: a Next.js server action answering "0:..\n1:{json}". Two pages of bills, one of Acts.
const bill = (n, year = 2026, d = `${year}-10-01T00:00:00.000Z`) => ({ billNo: n, billSubNo: year, date: d, descriptionEnglish: `  Bill   number ${n}  ` });
const act = (n, year = 2026) => ({ actNo: n, actSubNo: year, descriptionEnglish: `Act ${n}` });
function site({ bills = [bill(1), bill(2), bill(3)], acts = [act(5)], status = 200, body = null, perPage = 2 } = {}) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, init });
    if (status !== 200) return new Response("no", { status });
    if (body !== null) return new Response(body);
    const isActs = url.endsWith("/web/acts"), rows = isActs ? acts : bills;
    const { page } = JSON.parse(init.body)[0];
    const data = rows.slice((page - 1) * perPage, page * perPage);
    return new Response(`0:["$@1"]\n1:${JSON.stringify({ data, pagination: { total: rows.length, totalPages: Math.ceil(rows.length / perPage) } })}\n`);
  };
  return { fetchFn, calls };
}
const manifest = (keys) => ({ [MANIFEST_KEY]: { at: iso(NOW), keys } });

test("parseListing reads the server action's reply and says what a stale action id looks like", () => {
  assert.deepEqual(parseListing('0:["x"]\n1:{"data":[1]}\n'), { data: [1] });
  assert.throws(() => parseListing("<html>Not found</html>"), /action id may be stale/);
});

test("listingKeys uses the index's key format and leaves out old years and unnumbered Acts", () => {
  const k = listingKeys("bills", [bill(7), bill(9, 2020), { ...bill(8), billSubNo: 0 }], { now: NOW });
  assert.deepEqual(Object.keys(k), ["007/2026"]);
  assert.equal(k["007/2026"].title, "Bill number 7");
  assert.deepEqual(Object.keys(listingKeys("acts", [act(25), act(0), act(3, 2019)], { now: NOW })), ["Act 25/2026"]);
});

test("detect: pending = listed, but not indexed, not skipped by the last build, not ignored; every page is read", async () => {
  const env = { AUTH: kv({ [STATE_KEY]: JSON.stringify({ seen: { "003/2026": "x" } }) }), LOGS: bucket({ ...manifest(["001/2026"]), [SKIPPED_KEY]: { keys: ["Act 5/2026"] } }) };
  const s = site({ bills: [bill(1), bill(2), bill(3), bill(4)] });
  const check = await detect(env, { now: NOW, fetchFn: s.fetchFn });
  assert.deepEqual(Object.keys(check.pending), ["002/2026", "004/2026"]);
  assert.equal(check.listed, 5);
  assert.equal(s.calls.length, 3, "two pages of bills, one of Acts");
  assert.equal(s.calls[0].init.headers["Next-Action"].length > 10, true);
  assert.deepEqual(JSON.parse(await env.AUTH.get(STATE_KEY)).check.pending["002/2026"], { title: "Bill number 2", kind: "bill" });
});

test("detect: no manifest is said, a site failure is recorded and the last list is kept", async () => {
  const env = { AUTH: kv(), LOGS: bucket({}) };
  assert.equal((await detect(env, { now: NOW, fetchFn: site().fetchFn })).noManifest, true);
  env.LOGS = bucket(manifest([]));
  const good = await detect(env, { now: NOW, fetchFn: site().fetchFn });
  assert.equal(Object.keys(good.pending).length, 4);
  const bad = await detect(env, { now: NOW + 1000, fetchFn: site({ status: 500 }).fetchFn });
  assert.match(bad.error, /answered 500/);
  assert.equal(Object.keys(bad.pending).length, 4, "the last good list is kept");
  const stale = await detect(env, { now: NOW + 2000, fetchFn: site({ body: "<html>" }).fetchFn });
  assert.match(stale.error, /action id may be stale/);
});

test("phaseOf", () => {
  const run = { at: iso(NOW - 60e3) };
  assert.equal(phaseOf({}, null, NOW), "idle");
  assert.equal(phaseOf({ run }, { state: "built", at: iso(NOW - 864e5) }, NOW), "starting", "the old report is older than the request");
  assert.equal(phaseOf({ run: { at: iso(NOW - 40 * 60e3) } }, null, NOW), "stuck");
  assert.equal(phaseOf({ run }, { state: "running", at: iso(NOW - 30e3) }, NOW), "running");
  assert.equal(phaseOf({ run: { at: iso(NOW - 5 * 3600e3) } }, { state: "running", at: iso(NOW - 4 * 3600e3) }, NOW), "stuck");
  assert.equal(phaseOf({ run }, { state: "built", at: iso(NOW) }, NOW), "built");
  assert.equal(phaseOf({ run }, { state: "failed", at: iso(NOW) }, NOW), "failed");
  assert.equal(phaseOf({ run }, { state: "published", at: iso(NOW) }, NOW), "idle");
});

const REPO = { GITHUB_REPO: "o/r", GITHUB_DISPATCH_TOKEN: "tok", GITHUB_REF: "main" };
const gh = (status = 204, text = "") => { const calls = []; return { calls, fetchFn: async (u, i) => { calls.push({ u, i }); return new Response(status === 204 ? null : text, { status }); } }; };

test("Build dispatches the workflow with the mode; no token says what to set; a run in progress is refused", async () => {
  const env = { AUTH: kv(), LOGS: bucket({}), ...REPO };
  const g = gh();
  assert.deepEqual(await startRun(env, "build", { by: "me", now: NOW, fetchFn: g.fetchFn }), { ok: true });
  assert.equal(g.calls[0].u, "https://api.github.com/repos/o/r/actions/workflows/refresh-index.yml/dispatches");
  assert.deepEqual(JSON.parse(g.calls[0].i.body), { ref: "main", inputs: { mode: "build" } });
  assert.equal(g.calls[0].i.headers.Authorization, "Bearer tok");
  assert.equal(JSON.parse(await env.AUTH.get(STATE_KEY)).run.by, "me");
  assert.match((await startRun(env, "build", { by: "me", now: NOW + 1000, fetchFn: g.fetchFn })).error, /already in progress/);
  assert.equal(g.calls.length, 1);
  assert.match((await startRun({ AUTH: kv(), LOGS: bucket({}) }, "build", { now: NOW, fetchFn: g.fetchFn })).error, /GITHUB_DISPATCH_TOKEN/);
  const bad = await startRun({ AUTH: kv(), LOGS: bucket({}), ...REPO }, "build", { now: NOW, fetchFn: gh(404, '{"message":"Not Found"}').fetchFn });
  assert.match(bad.error, /404.*Not Found/);
});

test("Publish needs a built index; Discard deletes the staged pair and says so", async () => {
  const env = { AUTH: kv(), LOGS: bucket({}), ...REPO };
  assert.match((await startRun(env, "publish", { now: NOW, fetchFn: gh().fetchFn })).error, /no built index/);
  assert.match((await discard(env, { now: NOW })).error, /no built index/);
  env.LOGS = bucket({ [STATUS_KEY]: { state: "built", at: iso(NOW - 3600e3), new_bills: { "009/2026": "T" } }, [`${STAGING_PREFIX}index.sqlite`]: "x", [`${STAGING_PREFIX}vectors.npz`]: "x", "bills/index.sqlite": "live" });
  const g = gh();
  assert.deepEqual(await startRun(env, "publish", { by: "me", now: NOW, fetchFn: g.fetchFn }), { ok: true });
  assert.equal(JSON.parse(g.calls[0].i.body).inputs.mode, "publish");
  const env2 = { AUTH: kv(), LOGS: env.LOGS };
  assert.deepEqual(await discard(env2, { by: "me", now: NOW }), { ok: true });
  assert.deepEqual([...env.LOGS.m.keys()].filter((k) => k.startsWith(STAGING_PREFIX)), []);
  assert.ok(env.LOGS.m.has("bills/index.sqlite"), "the live index is not touched");
  assert.equal(JSON.parse(env.LOGS.m.get(STATUS_KEY)).state, "discarded");
});

// ---- the panel ----
const titles = (s) => todoItems(s).filter((x) => x.source === "index").map((x) => x.title);
async function panel({ state = {}, status = null, now = NOW, extra = {} } = {}) {
  const env = { AUTH: kv({ [STATE_KEY]: JSON.stringify({ check: { at: iso(now - 3600e3), listed: 9, pending: {} }, ...state }) }), BILLS_KV: kv(), LOGS: bucket({ "bills/index.sqlite": "x", "bills/vectors.npz": "x", ...(status ? { [STATUS_KEY]: status } : {}) }), ...REPO, ...extra };
  return { env, snap: await snapshot(env, { now }) };
}
const pendingState = { check: { at: iso(NOW - 3600e3), listed: 9, pending: { "010/2026": { title: "Mining Bill", kind: "bill" }, "Act 7/2026": { title: "Some Act", kind: "Act" } } } };

test("To do: new documents wait for you with Build and Ignore; nothing pending, nothing shown", async () => {
  assert.deepEqual(titles((await panel()).snap), []);
  const { snap } = await panel({ state: pendingState });
  const row = todoItems(snap).find((x) => x.source === "index");
  assert.equal(row.tier, "you");
  assert.match(row.title, /2 new documents on documents.gov.lk are not in the index/);
  assert.match(row.detail, /010\/2026 Mining Bill/);
  assert.deepEqual(row.actions.map((a) => a.post), ["/admin/refresh-build", "/admin/refresh-ignore"]);
});

test("To do: a build in progress, a built index (Publish / Discard), a failed build, a job that never started", async () => {
  const run = { at: iso(NOW - 120e3) };
  assert.match((await titles((await panel({ state: { ...pendingState, run } })).snap))[0], /requested; the job has not started/);
  const running = await panel({ state: { ...pendingState, run }, status: { state: "running", mode: "build", at: iso(NOW - 60e3) } });
  assert.deepEqual(titles(running.snap), ["Index build is running"], "no Build button beside a running build");
  const built = await panel({ state: { ...pendingState, run }, status: { state: "built", at: iso(NOW - 30e3), new_bills: { "010/2026": "Mining Bill" }, mrr: 0.878, min_mrr: 0.8 } });
  const row = todoItems(built.snap).find((x) => x.source === "index");
  assert.match(row.title, /built and checked: 1 document, ready to publish/);
  assert.match(row.detail, /0\.878/);
  assert.deepEqual(row.actions.filter((a) => a.post).map((a) => a.post), ["/admin/refresh-publish", "/admin/refresh-discard"]);
  assert.ok(row.actions[0].confirm);
  const failed = await panel({ state: { ...pendingState, run }, status: { state: "failed", at: iso(NOW - 30e3), error: "checks failed: retrieval MRR 0.700 is below 0.8" } });
  const f = todoItems(failed.snap).find((x) => x.source === "index");
  assert.equal(f.level, "red");
  assert.match(f.detail, /MRR 0\.700/);
  assert.match((await titles((await panel({ state: { ...pendingState, run: { at: iso(NOW - 3600e3) } } })).snap))[0], /has not reported for too long/);
});

test("To do: base Acts nobody could fetch get their own red row, whatever the build state", async () => {
  const missing = { "15/1979": { name: "Code of Criminal Procedure Act", cited_by: ["455/2024"], why: "not on documents.gov.lk" } };
  const { snap } = await panel({ state: pendingState, status: { state: "failed", at: iso(NOW - 30e3), error: "checks failed", base_acts_missing: missing } });
  const row = todoItems(snap).find((x) => /not in the index/.test(x.title));
  assert.ok(row, "a row for the missing Act");
  assert.equal(row.level, "red");
  assert.match(row.title, /^1 Act that bills amend is not in the index/);
  assert.match(row.detail, /Code of Criminal Procedure Act \(15\/1979\)/);
  const none = await panel({ state: pendingState, status: { state: "failed", at: iso(NOW - 30e3), error: "x" } });
  assert.ok(!todoItems(none.snap).some((x) => /not in the index/.test(x.title)));
});

test("To do: after a publish the stored list is out of date, so it asks for a new check instead of offering Build", async () => {
  const { snap } = await panel({ state: pendingState, status: { state: "published", at: iso(NOW - 60e3) } });
  assert.deepEqual(titles(snap), ["A new index was published: check that nothing is still missing"]);
});

test("To do: check failures, a missing list, never checked, a stale check", async () => {
  const err = await panel({ state: { check: { failedAt: iso(NOW), error: "documents.gov.lk answered 500", pending: {} } } });
  assert.match(titles(err.snap)[0], /Cannot check documents.gov.lk/);
  assert.match(todoItems(err.snap).find((x) => x.source === "index").detail, /DOCS_ACTION_ID/);
  assert.match((await titles((await panel({ state: { check: { at: iso(NOW), noManifest: true, pending: {} } } })).snap))[0], /no list of the documents/);
  assert.match((await titles((await panel({ state: { check: null } })).snap))[0], /not been checked/);
  assert.match((await titles((await panel({ state: { check: { at: iso(NOW - 40 * 3600e3), pending: {} } } })).snap))[0], /last checked 40 h ago/);
});

function call(env, path, { method = "POST", fetchFn = gh().fetchFn } = {}) {
  const admin = createAdmin({ now: () => NOW, fetchFn, containerState: async () => ({ status: "stopped", lastChange: NOW }) });
  return admin(new Request(`https://admin.test${path}`, { method, headers: method === "POST" ? { Origin: "https://admin.test" } : {} }), env, "me@example.com");
}
const flash = (r) => decodeURIComponent(new URL(r.headers.get("Location"), "https://admin.test").searchParams.get("done"));

test("the buttons work through the panel and are written to the audit log", async () => {
  const { env } = await panel({ state: pendingState });
  const g = gh();
  assert.match(flash(await call(env, "/admin/refresh-build", { fetchFn: g.fetchFn })), /Build started on GitHub/);
  assert.equal(g.calls.length, 1);
  assert.match(flash(await call(env, "/admin/refresh-build", { fetchFn: g.fetchFn })), /already in progress/);
  const ign = await panel({ state: pendingState });
  assert.match(flash(await call(ign.env, "/admin/refresh-ignore")), /2 documents will not be reported again/);
  assert.deepEqual(JSON.parse(await ign.env.AUTH.get(STATE_KEY)).check.pending, {});
  assert.deepEqual(titles(await snapshot(ign.env, { now: NOW })), []);
  const audit = JSON.parse(await env.AUTH.get("admin:audit")).map((x) => x.what);
  assert.ok(audit.includes("Started an index build") && audit.some((w) => w.startsWith("Index build not started")));
});

test("Check now runs the check and says what it found", async () => {
  const { env } = await panel({ extra: { LOGS: bucket({ ...manifest(["001/2026", "002/2026", "003/2026", "Act 5/2026"]), "bills/index.sqlite": "x", "bills/vectors.npz": "x" }) } });
  assert.match(flash(await call(env, "/admin/refresh-check", { fetchFn: site().fetchFn })), /The index has every listed document/);
  assert.match(flash(await call(env, "/admin/refresh-check", { fetchFn: site({ bills: [bill(8), bill(9)] }).fetchFn })), /2 documents not in the index/);
});

test("the Bill index page shows the state and its buttons, and escapes what the site sends", async () => {
  const { env } = await panel({ state: { check: { at: iso(NOW - 3600e3), listed: 3, pending: { "010/2026": { title: "<script>alert(1)</script>", kind: "bill" } } } } });
  const html = await (await call(env, "/admin/source/index", { method: "GET" })).text();
  assert.match(html, /Not in the index/);
  assert.ok(!html.includes("<script>alert(1)"), "titles from documents.gov.lk are untrusted");
  assert.match(html, /refresh-check/);
  assert.match(html, /refresh-build/);
});

test("the daily cron runs the check; the hourly one does not touch documents.gov.lk", async () => {
  const wrangler = readFileSync(new URL("../workers/admin/wrangler.toml", import.meta.url), "utf8");
  assert.ok(wrangler.includes(`"${CHECK_CRON}"`), "wrangler.toml lists the daily cron");
  assert.ok(wrangler.includes('"23 * * * *"'));
  const real = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (u) => { seen.push(String(u)); return new Response("0:x\n1:" + JSON.stringify({ data: [], pagination: { totalPages: 1 } })); };
  try {
    const env = { AUTH: kv(), LOGS: bucket(manifest([])) };
    const waits = [];
    adminWorker.scheduled({ cron: CHECK_CRON }, env, { waitUntil: (p) => waits.push(p) });
    await Promise.all(waits);
    assert.ok(seen.some((u) => u.startsWith("https://documents.gov.lk")));
    assert.ok(JSON.parse(await env.AUTH.get(STATE_KEY)).check.at, "the result is stored");
    seen.length = 0;
    adminWorker.scheduled({ cron: "23 * * * *" }, { AUTH: kv(), LOGS: bucket({}) }, { waitUntil: (p) => waits.push(p.catch(() => {})) });
    await Promise.all(waits);
    assert.deepEqual(seen, []);
  } finally {
    globalThis.fetch = real;
  }
});

test("the workflow file and the Worker agree on its name and modes", () => {
  const wf = readFileSync(new URL("../.github/workflows/refresh-index.yml", import.meta.url), "utf8");
  assert.match(wf, /options: \[build, publish\]/);
  assert.match(wf, /refresh_bills\.py --report report\.json/);
  assert.ok(!/refresh_bills\.py[^\n]*--publish/.test(wf.replace(/#[^\n]*/g, "")), "the build never publishes by itself");
});

test("new documents are emailed once; a failed send is retried; no address means no mail", async () => {
  const sent = [];
  const send = async (_env, m) => void sent.push(m);
  const env = { AUTH: kv(), LOGS: bucket(manifest(["001/2026"])), NEW_BILLS_EMAIL: "me@example.com", FROM_ADDRESS: "login@x" };
  await detect(env, { now: NOW, fetchFn: site().fetchFn, send });
  assert.equal(sent.length, 1);
  assert.equal(sent[0].to, "me@example.com");
  assert.match(sent[0].subject, /3 new bills or Acts/);
  assert.match(sent[0].text, /002\/2026 {2}Bill number 2/);
  assert.match(sent[0].text, /admin\.citizenlk\.com/);
  await detect(env, { now: NOW + 864e5, fetchFn: site().fetchFn, send });
  assert.equal(sent.length, 1, "told once");
  await detect(env, { now: NOW + 2 * 864e5, fetchFn: site({ bills: [bill(1), bill(2), bill(3), bill(4)] }).fetchFn, send });
  assert.equal(sent.length, 2);
  assert.match(sent[1].subject, /^1 new bill or Act/, "only the new one");

  const env2 = { AUTH: kv(), LOGS: bucket(manifest([])), NEW_BILLS_EMAIL: "me@example.com" };
  const failing = async () => { throw new Error("mail down"); };
  assert.match((await detect(env2, { now: NOW, fetchFn: site().fetchFn, send: failing })).mailError, /mail down/);
  assert.equal(titlesOf(await snapshot({ ...env2, BILLS_KV: kv(), AUTH: env2.AUTH }, { now: NOW })).some((t) => /email could not be sent/.test(t)), true);
  await detect(env2, { now: NOW + 1000, fetchFn: site().fetchFn, send });
  assert.equal(sent.length, 3, "retried after the failure");

  const env3 = { AUTH: kv(), LOGS: bucket(manifest([])) };
  await detect(env3, { now: NOW, fetchFn: site().fetchFn, send });
  assert.equal(sent.length, 3, "no address set");
});
const titlesOf = (s) => todoItems(s).map((x) => x.title);
