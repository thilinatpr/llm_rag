import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { VERSION as CONSENT_VERSION, required } from "../src/consent.js";
import { privacyPage } from "../src/privacy.js";
import { siteOf, GO } from "../src/events.js";
import { createBudgetHandler, FALLBACK_MODEL, logAsk } from "../workers/budget/handler.js";

const SECRET = "s".repeat(32);
const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
function setup(extra = {}) {
  const forwarded = [], aiCalls = [];
  const m = new Map();
  const AUTH = { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k) };
  const env = { PROXY_SECRET: SECRET, BILLRAG: {}, AUTH, ALLOWED_EMAILS: "me@example.com", OTP_PEPPER: "p", ASSETS: { fetch: async () => new Response("page") },
    AI: { run: async (model, input) => { aiCalls.push({ model, input }); return { choices: [{ message: { content: "ok" }, finish_reason: "stop" }], usage: { prompt_tokens: 9 } }; } }, ...extra };
  const handle = createBudgetHandler(() => ({ fetch: async (r) => { forwarded.push(r); return new Response("{}"); } }));
  return { env, handle, forwarded, aiCalls, m };
}
const signIn = (t, consent = true) => {
  t.m.set("session:" + "a".repeat(64), "me@example.com");
  if (consent) t.m.set("consent:me@example.com", JSON.stringify({ general: { v: CONSENT_VERSION }, budget: { v: CONSENT_VERSION } }));
  return { Cookie: "clk_session=" + "a".repeat(64) };
};
const ask = (headers = {}, body = { question: "q" }) => new Request("https://budget.citizenlk.com/api/ask", { method: "POST", headers, body: JSON.stringify(body) });

test("the API stays behind the login and is not forwarded without a session", async () => {
  const t = setup();
  assert.equal((await t.handle(ask(), t.env)).status, 401);
  assert.equal(t.forwarded.length, 0);
});

test("a signed-in account that has not accepted the Budget statements gets the consent step, not the container", async () => {
  const t = setup();
  const r = await t.handle(ask(signIn(t, false)), t.env);
  assert.equal(r.status, 403);
  assert.equal(t.forwarded.length, 0);
  const ids = required("budget").map((i) => i.id);
  assert.ok(ids.includes("budget-official") && ids.includes("budget-decision") && ids.includes("accuracy"));
});

test("a consented session reaches the container with the proxy secret and without its cookie", async () => {
  const t = setup();
  const r = await t.handle(ask(signIn(t)), t.env);
  assert.equal(r.status, 200, await r.clone().text());
  assert.equal(t.forwarded.length, 1);
  assert.equal(t.forwarded[0].headers.get("X-Proxy-Secret"), SECRET);
  assert.equal(t.forwarded[0].headers.get("Cookie"), null);
});

test("backup chat needs the proxy secret, runs on the AI binding, and caps what it accepts", async () => {
  const t = setup();
  const req = (headers, body) => new Request("https://budget.citizenlk.com/internal/chat", { method: "POST", headers, body: JSON.stringify(body) });
  const good = { system: "s", messages: [{ role: "user", content: "q" }], max_tokens: 99999, effort: "low" };
  assert.equal((await t.handle(req({}, good), t.env)).status, 403);
  assert.equal((await t.handle(req({ "X-Proxy-Secret": "wrong" }, good), t.env)).status, 403);
  assert.equal(t.aiCalls.length, 0);
  const headers = { "X-Proxy-Secret": SECRET };
  assert.equal((await t.handle(req(headers, { system: "s" }), t.env)).status, 400);
  assert.equal((await t.handle(req(headers, { ...good, system: "x".repeat(200_000) }), t.env)).status, 400);
  const ok = await t.handle(req(headers, good), t.env);
  assert.deepEqual(await ok.json(), { text: "ok", stop: "stop", usage: { prompt_tokens: 9 } });
  assert.equal(t.aiCalls[0].model, FALLBACK_MODEL);
  assert.equal(t.aiCalls[0].input.max_tokens, 4000);
  assert.equal(t.aiCalls[0].input.reasoning_effort, "low");
});

test("there is no reranker, law or other internal route: only /internal/chat", async () => {
  const t = setup();
  for (const p of ["/internal/rerank", "/internal/law", "/internal/evidence"]) {
    const r = await t.handle(new Request("https://budget.citizenlk.com" + p, { method: "POST", headers: { "X-Proxy-Secret": SECRET }, body: "{}" }), t.env);
    assert.notEqual(r.status, 200, p);
    assert.equal(t.forwarded.length, 0, p);
  }
});

test("each answered question is logged under budget-logs/ with the build and the evidence, and the trace never reaches the browser", async () => {
  const puts = [], waits = [];
  const trace = { build: "ab12cd34", seconds: 2.1, model: "deepseek-flash", evidence: ["[Bill Head 118]"], calls: [{ kind: "chat", in: 100, out: 20 }], cost: { usd: 0.0005 } };
  const res = new Response(JSON.stringify({ answer: "a [Bill Head 118]", sources: ["[Bill Head 118] x"], warnings: [] }), { headers: { "X-Ask-Trace": JSON.stringify(trace) } });
  const t = setup({ LOGS: { put: async (k, v) => void puts.push({ k, rec: JSON.parse(v) }) } });
  const handle = createBudgetHandler(() => ({ fetch: async () => res }));
  const r = await handle(ask(signIn(t), { question: "head 118?" }), t.env, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  assert.equal(r.status, 200);
  assert.equal(r.headers.get("X-Ask-Trace"), null);
  assert.match(puts[0].k, /^budget-logs\/\d{4}-\d\d-\d\d\/\d{9}-[0-9a-f]{8}\.json$/);
  const rec = puts[0].rec;
  assert.deepEqual([rec.type, rec.service, rec.question, rec.build, rec.evidence], ["ask", "budget", "head 118?", "ab12cd34", ["[Bill Head 118]"]]);
  assert.equal(rec.cost.usd, 0.0005);
});

test("logAsk never throws: no binding, a failing put, a non-JSON reply", async () => {
  await logAsk({}, null, { question: "q", response: new Response("x"), ms: 1, status: 200 });
  await logAsk({ LOGS: { put: async () => { throw new Error("r2 down"); } } }, null, { question: "q", response: new Response("not json", { status: 502 }), ms: 1, status: 502 });
});

test("config: staff only at first, own domain, own container class, no open sign-up", () => {
  const toml = read("../workers/budget/wrangler.toml");
  assert.ok(toml.includes('ALLOWED_EMAILS = "thilinatpr@gmail.com"'));
  assert.ok(toml.includes('pattern = "budget.citizenlk.com"'));
  assert.ok(toml.includes("workers_dev = false") && toml.includes("preview_urls = false"));
  assert.ok(!/^OPEN_TO\s*=/m.test(toml), "OPEN_TO must stay unset until the owner opens it");
  assert.ok(toml.includes('OPEN_SIGNUP = "off"'));
  assert.ok(toml.includes('class_name = "BudgetContainer"') && toml.includes('new_sqlite_classes = ["BudgetContainer"]'));
  assert.ok(toml.includes('id = "a44e7e8cc8f0444ba865ea162820193d"'), "shares the account KV with the other services");
  assert.ok(!toml.includes("BILLS") && !toml.includes("LAW_"), "no link to Bill Assistant's law route");
  const worker = read("../workers/budget/worker.js");
  assert.ok(worker.includes("export class BudgetContainer") && worker.includes("BUDGET_PROXY_SECRET") && worker.includes("BUDGET_FALLBACK_URL"));
  const docker = read("../workers/budget/Dockerfile");
  assert.ok(docker.includes("data/budget/index.sqlite") && docker.includes("budget.serve"));
  for (const f of ["ask.py", "index.py", "parse.py", "speech.py", "serve.py"]) assert.ok(docker.includes("budget/" + f), f);  // every module the server imports is in the image
  assert.ok(!docker.includes("evaluate"));
});

test("deploy.yml has a budget target that fetches its index from R2", () => {
  const y = read("../.github/workflows/deploy.yml");
  assert.match(y, /options: \[bills, admin, procurement, budget\]/);
  assert.ok(y.includes("scripts/budget_index.py fetch") && y.includes("workers/budget/wrangler.toml"));
});

test("the Budget page compiles, has its own words, and cites only Bill and Speech labels", () => {
  const html = read("../workers/budget/public/index.html");
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.ok(scripts.length >= 1);
  for (const code of scripts) assert.doesNotThrow(() => new vm.Script(code, { filename: "budget.html" }));
  assert.ok(html.includes("<title>Budget 2027 - citizenlk</title>") && html.includes('<a class="mark" href="https://citizenlk.com">'));
  assert.ok(html.includes("can be incomplete or wrong"), "DESIGN.md principle 4");
  assert.doesNotMatch(html, /Procurement|PG2024|tender|standstill|history: turns|standalone/);
  assert.doesNotMatch(html, /Sinhala, Tamil or English/);
  assert.ok(html.includes("(?:Bill|Speech)"));
});

test("consent and privacy: Budget has its own statements and page, and visitor counts know its host", async () => {
  assert.match(required("budget").map((i) => i.text).join(" "), /not the Treasury[\s\S]*not official figures[\s\S]*as presented to Parliament/);
  const p = privacyPage("budget");
  assert.match(p, /Budget 2027/);
  assert.ok(p.includes('<a href="/">Back to Budget 2027</a>') && p.includes('<a class="mark" href="https://citizenlk.com">'));
  assert.match(p, /every question is answered on its own/);
  assert.equal(siteOf("budget.citizenlk.com"), "budget");
  assert.ok(GO.includes("budget"));
  const t = setup();
  const r = await t.handle(new Request("https://budget.citizenlk.com/privacy"), t.env);
  assert.equal(r.status, 200);
  assert.match(await r.text(), /Budget 2027/);
  assert.equal(t.forwarded.length, 0);
});

// "Use the auth system, env vars and bindings the same way as Bill Assistant and Procurement Rules" (owner, 2026-11-17): the settings that make
// one citizenlk account work across services must not drift between the Budget and Procurement configs.
test("config: account, mail, model and storage settings equal Procurement's", () => {
  const strip = (t) => t.replace(/^\s*#.*$/gm, "");
  const val = (t, k) => (t.match(new RegExp(`^${k}\\s*=\\s*"([^"]*)"`, "m")) || [])[1];
  const bud = strip(read("../workers/budget/wrangler.toml")), proc = strip(read("../workers/procurement/wrangler.toml"));
  for (const k of ["COOKIE_DOMAIN", "ALLOWED_EMAILS", "FROM_ADDRESS", "CF_ACCOUNT_ID", "CHAT_MODEL", "CHAT_BASE_URL", "OPEN_SIGNUP", "MEMBER_MONTHLY", "compatibility_date"])
    { assert.ok(val(bud, k), `${k} missing in Budget`); assert.equal(val(bud, k), val(proc, k), k); }
  const block = (t, header) => (t.match(new RegExp(`\\[\\[${header.replace(/\./g, "\\.")}\\]\\]([\\s\\S]*?)(?=\\n\\[|$)`)) || [])[1]?.replace(/\s+/g, " ").trim();
  assert.equal(block(bud, "kv_namespaces"), block(proc, "kv_namespaces"));                         // the shared account KV, binding AUTH
  assert.equal(block(bud, "r2_buckets"), block(proc, "r2_buckets"));                               // LOGS -> gov-rag
  assert.equal(block(bud, "analytics_engine_datasets"), block(proc, "analytics_engine_datasets"));
  assert.ok(bud.includes('script_name = "gov-rag-llm"') && bud.includes('class_name = "QuotaCounter"'));
  assert.ok(bud.includes("[ai]") && /binding = "AI"/.test(bud));
  // the secrets the Worker reads, as on Procurement (LAW_SECRET only serves its law link): documented in the header of each config
  for (const s of ["OTP_PEPPER", "PROXY_SECRET", "CF_EMAIL_TOKEN", "DEEPSEEK_API_KEY"]) assert.ok(read("../workers/budget/wrangler.toml").includes(s), s);
});

// The first Budget deploy (2026-11-17) failed at `docker build`: .dockerignore starts with `data/`, and only Procurement's index had an
// exception, so `COPY data/budget/index.sqlite` found nothing. No other test builds an image, so check the rule itself, for every Worker image.
test(".dockerignore lets every baked-in data file into the build context", () => {
  const ignore = read("../.dockerignore").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
  assert.ok(ignore.includes("data/"), "the rule this test guards: data/ is excluded by default");
  let checked = 0;
  for (const w of ["budget", "procurement"]) {
    for (const m of read(`../workers/${w}/Dockerfile`).matchAll(/^COPY\s+(data\/\S+)\s/gm)) {
      assert.ok(ignore.includes("!" + m[1]), `${w}: ${m[1]} needs "!${m[1]}" in .dockerignore`);
      checked++;
    }
  }
  assert.equal(checked, 2);
});
