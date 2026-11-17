// Budget Worker: the shared login gate in front of the Python container, plus the backup chat model the container calls back.
// The container has no Cloudflare token: /internal/chat runs the backup model on the Worker's own Workers AI binding.
import { createHandler } from "../../src/auth.js";

export const FALLBACK_MODEL = "@cf/openai/gpt-oss-120b";
const MAX_CHAT_CHARS = 150_000;
const MAX_CHAT_TOKENS = 4000;
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });

async function secretMatches(got, want) {
  if (!got || !want) return false;
  const digest = async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([digest(got), digest(want)]);
  return a.reduce((diff, byte, i) => diff | (byte ^ b[i]), 0) === 0;
}

// The container's backup chat model for when DeepSeek fails (budget/serve.py WorkerChat).
async function chat(request, env) {
  if (request.method !== "POST" || !(await secretMatches(request.headers.get("X-Proxy-Secret"), env.PROXY_SECRET)))
    return json({ error: "forbidden" }, 403);
  const raw = await request.text();
  const body = raw.length <= MAX_CHAT_CHARS ? JSON.parse(raw || "null") : null;
  if (!body || typeof body.system !== "string" || !Array.isArray(body.messages)) return json({ error: "bad request" }, 400);
  const input = { messages: [{ role: "system", content: body.system }, ...body.messages], max_tokens: Math.min(Number(body.max_tokens) || 2048, MAX_CHAT_TOKENS) };
  if (["low", "medium", "high"].includes(body.effort)) input.reasoning_effort = body.effort;
  const r = await env.AI.run(env.FALLBACK_MODEL || FALLBACK_MODEL, input);
  const choice = r.choices?.[0];
  return json({ text: choice?.message?.content ?? r.response ?? "", stop: choice?.finish_reason ?? "", usage: r.usage ?? null });
}

// One R2 object per answered question: budget-logs/YYYY-MM-DD/<HHMMSSmmm>-<id>.json, written through the LOGS binding (the container has no
// storage credentials), best-effort and after the reply. `trace` (X-Ask-Trace) carries build, evidence labels, seconds, model, calls, cost.
// Records hold users' questions: private.
export async function logAsk(env, ctx, { question, response, ms, status, trace }) {
  if (!env.LOGS) return;
  const work = (async () => {
    let body = null;
    try { body = await response.json(); } catch { /* not JSON: log the status only */ }
    const now = new Date();
    const rec = { ts: now.toISOString(), type: "ask", service: "budget", question, status, ms, answer: body?.answer ?? null, sources: body?.sources ?? [],
                  warnings: body?.warnings ?? [], error: body?.error ?? null, ...trace };
    const key = `budget-logs/${now.toISOString().slice(0, 10)}/${now.toISOString().slice(11, 23).replace(/[:.]/g, "")}-${crypto.randomUUID().slice(0, 8)}.json`;
    await env.LOGS.put(key, JSON.stringify(rec), { httpMetadata: { contentType: "application/json" } });
  })().catch((e) => console.error(JSON.stringify({ event: "log_failed", error: String((e && e.message) || e) })));
  ctx?.waitUntil ? ctx.waitUntil(work) : await work;
}

export function createBudgetHandler(getStub) {
  const upstream = async (request, env, ctx) => {
    const headers = new Headers(request.headers);
    headers.delete("Cookie");
    headers.set("X-Proxy-Secret", env.PROXY_SECRET);
    const started = Date.now();
    const path = new URL(request.url).pathname;
    const isAsk = request.method === "POST" && path === "/api/ask";
    let question = "";
    if (isAsk) question = String((await request.clone().json().catch(() => ({}))).question ?? "").slice(0, 600);
    let res;
    try {
      res = await getStub(env).fetch(new Request(request, { headers }));
    } catch (e) {
      console.error(JSON.stringify({ event: "upstream_failed", path, ms: Date.now() - started, error: String((e && e.message) || e) }));
      res = json({ error: "The app server is not responding. If it was asleep, it is starting; try again in a minute." }, 502);
    }
    if (!isAsk) return res;
    let trace = {};
    try { trace = JSON.parse(res.headers.get("X-Ask-Trace") || "{}"); } catch { /* log without it */ }
    await logAsk(env, ctx, { question, response: res.clone(), ms: Date.now() - started, status: res.status, trace });
    const out = new Response(res.body, res);
    out.headers.delete("X-Ask-Trace");
    return out;
  };
  return async (request, env, ctx) => {
    if (new URL(request.url).pathname === "/internal/chat") return chat(request, env);
    return createHandler({ privacy: "budget", cookie: "clk_session", consent: { service: "budget", name: "Budget 2027" }, upstream: (req, e) => upstream(req, e, ctx) })(request, env);
  };
}
