// Procurement Rules Worker: the shared login gate in front of the Python container, plus the reranker the container calls back.
// The container has no Cloudflare token: /internal/rerank runs the model on the Worker's own Workers AI binding.
import { createHandler } from "../../src/auth.js";

export const RERANK_MODEL = "@cf/baai/bge-reranker-base";
const MAX_CONTEXTS = 40;
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });

async function secretMatches(got, want) {
  if (!got || !want) return false;
  const digest = async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([digest(got), digest(want)]);
  return a.reduce((diff, byte, i) => diff | (byte ^ b[i]), 0) === 0;
}

async function rerank(request, env) {
  if (request.method !== "POST" || !(await secretMatches(request.headers.get("X-Proxy-Secret"), env.PROXY_SECRET)))
    return json({ error: "forbidden" }, 403);
  const body = await request.json().catch(() => null);
  if (!body || typeof body.query !== "string" || !Array.isArray(body.contexts) || body.contexts.length > MAX_CONTEXTS)
    return json({ error: "bad request" }, 400);
  const result = await env.AI.run(RERANK_MODEL, { query: body.query, contexts: body.contexts });
  return json({ response: result.response });
}

// One R2 object per answered question: procurement-logs/YYYY-MM-DD/<HHMMSSmmm>-<id>.json. Written by the Worker through its
// LOGS binding, so the container needs no storage credentials. Best-effort and after the reply (waitUntil): logging never
// breaks or delays an answer. Records hold users' questions: private.
export async function logAsk(env, ctx, { question, response, ms, status }) {
  if (!env.LOGS) return;
  const work = (async () => {
    let body = null;
    try { body = await response.json(); } catch { /* not JSON: log the status only */ }
    const now = new Date();
    const rec = { ts: now.toISOString(), type: "ask", question, standalone: body?.standalone || null, status, ms, answer: body?.answer ?? null, sources: body?.sources ?? [],
                  warnings: body?.warnings ?? [], error: body?.error ?? null };
    const key = `procurement-logs/${now.toISOString().slice(0, 10)}/${now.toISOString().slice(11, 23).replace(/[:.]/g, "")}-${crypto.randomUUID().slice(0, 8)}.json`;
    await env.LOGS.put(key, JSON.stringify(rec), { httpMetadata: { contentType: "application/json" } });
  })().catch((e) => console.error(JSON.stringify({ event: "log_failed", error: String((e && e.message) || e) })));
  ctx?.waitUntil ? ctx.waitUntil(work) : await work;
}

// startContainer(env) returns an object with fetch(request): the container's stub.
export function createProcurementHandler(getStub) {
  const upstream = async (request, env, ctx) => {
    const headers = new Headers(request.headers);
    headers.delete("Cookie");
    headers.set("X-Proxy-Secret", env.PROXY_SECRET);
    const started = Date.now();
    const path = new URL(request.url).pathname;
    const isAsk = request.method === "POST" && path === "/api/ask";
    let question = "";
    if (isAsk) question = String((await request.clone().json().catch(() => ({}))).question ?? "").slice(0, 1000);
    let res;
    try {
      res = await getStub(env).fetch(new Request(request, { headers }));
    } catch (e) {
      console.error(JSON.stringify({ event: "upstream_failed", path, ms: Date.now() - started, error: String((e && e.message) || e) }));
      res = json({ error: "The app server is not responding. If it was asleep, it is starting; try again in a minute." }, 502);
    }
    if (isAsk) await logAsk(env, ctx, { question, response: res.clone(), ms: Date.now() - started, status: res.status });
    return res;
  };
  return async (request, env, ctx) => {
    if (new URL(request.url).pathname === "/internal/rerank") return rerank(request, env);
    return createHandler({ upstream: (req, e) => upstream(req, e, ctx) })(request, env);
  };
}
