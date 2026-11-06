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

// startContainer(env) returns an object with fetch(request): the container's stub.
export function createProcurementHandler(getStub) {
  const upstream = async (request, env) => {
    const headers = new Headers(request.headers);
    headers.delete("Cookie");
    headers.set("X-Proxy-Secret", env.PROXY_SECRET);
    const started = Date.now();
    try {
      return await getStub(env).fetch(new Request(request, { headers }));
    } catch (e) {
      const path = new URL(request.url).pathname;
      console.error(JSON.stringify({ event: "upstream_failed", path, ms: Date.now() - started, error: String((e && e.message) || e) }));
      return json({ error: "The app server is not responding. If it was asleep, it is starting; try again in a minute." }, 502);
    }
  };
  const gated = createHandler({ upstream });
  return async (request, env) => (new URL(request.url).pathname === "/internal/rerank" ? rerank(request, env) : gated(request, env));
}
