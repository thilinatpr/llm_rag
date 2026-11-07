import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createProcurementHandler, FALLBACK_MODEL, logAsk, RERANK_MODEL } from "../workers/procurement/handler.js";

const SECRET = "s".repeat(32);
function setup() {
  const forwarded = [], aiCalls = [];
  const env = { PROXY_SECRET: SECRET, BILLRAG: {}, AUTH: { get: async () => null, put: async () => {}, delete: async () => {} },
    ALLOWED_EMAILS: "me@example.com", ASSETS: { fetch: async () => new Response("page") },
    AI: { run: async (model, input) => { aiCalls.push({ model, input }); return { response: [{ id: 0, score: 0.5 }] }; } } };
  const handle = createProcurementHandler(() => ({ fetch: async (r) => { forwarded.push(r); return new Response("{}"); } }));
  return { env, handle, forwarded, aiCalls };
}
const rerank = (headers, body = { query: "q", contexts: [{ text: "x" }] }) =>
  new Request("https://procurement.citizenlk.com/internal/rerank", { method: "POST", headers, body: JSON.stringify(body) });

test("rerank needs the proxy secret and runs on the AI binding", async () => {
  const t = setup();
  assert.equal((await t.handle(rerank({}), t.env)).status, 403);
  assert.equal((await t.handle(rerank({ "X-Proxy-Secret": "wrong" }), t.env)).status, 403);
  assert.equal(t.aiCalls.length, 0);
  const ok = await t.handle(rerank({ "X-Proxy-Secret": SECRET }), t.env);
  assert.deepEqual(await ok.json(), { response: [{ id: 0, score: 0.5 }] });
  assert.equal(t.aiCalls[0].model, RERANK_MODEL);
});

test("rerank refuses malformed and oversized requests", async () => {
  const t = setup(), headers = { "X-Proxy-Secret": SECRET };
  assert.equal((await t.handle(rerank(headers, { query: "q" }), t.env)).status, 400);
  assert.equal((await t.handle(rerank(headers, { query: "q", contexts: Array(41).fill({ text: "x" }) }), t.env)).status, 400);
});

test("the API stays behind the login and is not forwarded without a session", async () => {
  const t = setup();
  const r = await t.handle(new Request("https://procurement.citizenlk.com/api/ask", { method: "POST", body: "{}" }), t.env);
  assert.equal(r.status, 401);
  assert.equal(t.forwarded.length, 0);
});

test("only the owner is allowed at launch, on the procurement domain", () => {
  const toml = readFileSync(new URL("../workers/procurement/wrangler.toml", import.meta.url), "utf8");
  assert.ok(toml.includes('ALLOWED_EMAILS = "thilinatpr@gmail.com"'));
  assert.ok(toml.includes('pattern = "procurement.citizenlk.com"'));
  assert.ok(toml.includes("workers_dev = false"));
});

test("logAsk writes one R2 object per answered question, after the reply", async () => {
  const puts = [], waits = [];
  const env = { LOGS: { put: async (key, body) => puts.push({ key, rec: JSON.parse(body) }) } };
  const ctx = { waitUntil: (p) => waits.push(p) };
  const response = new Response(JSON.stringify({ answer: "A [x]", sources: ["s1"], warnings: [], standalone: "full q?" }));
  await logAsk(env, ctx, { question: "q?", response, ms: 12, status: 200 });
  await Promise.all(waits);
  assert.equal(puts.length, 1);
  assert.equal(puts[0].rec.type, "ask"); // read_chatlogs.py counts anything else as a search
  assert.equal(puts[0].rec.standalone, "full q?");
  assert.match(puts[0].key, /^procurement-logs\/\d{4}-\d\d-\d\d\/\d{9}-[0-9a-f]{8}\.json$/);
  assert.deepEqual([puts[0].rec.question, puts[0].rec.answer, puts[0].rec.sources, puts[0].rec.status], ["q?", "A [x]", ["s1"], 200]);
});

test("logAsk never throws: no binding, a failing put, a non-JSON reply", async () => {
  await logAsk({}, null, { question: "q", response: new Response("x"), ms: 1, status: 200 });
  const env = { LOGS: { put: async () => { throw new Error("r2 down"); } } };
  await logAsk(env, null, { question: "q", response: new Response("not json", { status: 502 }), ms: 1, status: 502 });
});

test("backup chat needs the proxy secret, runs on the AI binding, and caps what it accepts", async () => {
  const t = setup();
  t.env.AI.run = async (model, input) => { t.aiCalls.push({ model, input }); return { choices: [{ message: { content: "Yes [PG2024 §1.2]." }, finish_reason: "stop" }], usage: { prompt_tokens: 9, completion_tokens: 3 } }; };
  const req = (headers, body) => new Request("https://procurement.citizenlk.com/internal/chat", { method: "POST", headers, body: JSON.stringify(body) });
  const good = { system: "s", messages: [{ role: "user", content: "q" }], max_tokens: 99999, effort: "low" };
  assert.equal((await t.handle(req({}, good), t.env)).status, 403);
  assert.equal(t.aiCalls.length, 0);
  const headers = { "X-Proxy-Secret": SECRET };
  assert.equal((await t.handle(req(headers, { system: "s" }), t.env)).status, 400);
  assert.equal((await t.handle(req(headers, { ...good, system: "x".repeat(200_000) }), t.env)).status, 400);
  const ok = await t.handle(req(headers, good), t.env);
  assert.deepEqual(await ok.json(), { text: "Yes [PG2024 §1.2].", stop: "stop", usage: { prompt_tokens: 9, completion_tokens: 3 } });
  const { model, input } = t.aiCalls[0];
  assert.equal(model, FALLBACK_MODEL);
  assert.equal(input.max_tokens, 4000);
  assert.equal(input.reasoning_effort, "low");
  assert.deepEqual(input.messages[0], { role: "system", content: "s" });
});

test("tokens and cost go into the log, not to the browser", async () => {
  const puts = [];
  const trace = { seconds: 4.2, model: "deepseek-flash", calls: [{ kind: "chat", in: 100, out: 20, usd: 0.0001 }], cost: { usd: 0.0001 } };
  const res = new Response(JSON.stringify({ answer: "a", sources: [], warnings: [] }), { headers: { "X-Ask-Trace": JSON.stringify(trace) } });
  const handle = createProcurementHandler(() => ({ fetch: async () => res }));
  const m = new Map();
  const AUTH = { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async () => {} };
  const env = { PROXY_SECRET: SECRET, BILLRAG: {}, AUTH, ALLOWED_EMAILS: "me@example.com", ASSETS: { fetch: async () => new Response("page") }, OTP_PEPPER: "p",
    LOGS: { put: async (k, v) => void puts.push(JSON.parse(v)) } };
  m.set("session:" + "a".repeat(64), "me@example.com");
  const r = await handle(new Request("https://procurement.citizenlk.com/api/ask", { method: "POST", headers: { Cookie: "session=" + "a".repeat(64) }, body: JSON.stringify({ question: "q" }) }), env);
  assert.equal(r.status, 200, await r.clone().text());
  assert.equal(r.headers.get("X-Ask-Trace"), null);
  assert.equal(puts[0].seconds, 4.2);
  assert.equal(puts[0].calls[0].in, 100);
  assert.equal(puts[0].cost.usd, 0.0001);
});
