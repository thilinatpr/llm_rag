import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createProcurementHandler, logAsk, RERANK_MODEL } from "../workers/procurement/handler.js";

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
