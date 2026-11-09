// Law passages for Procurement Rules (billrag/lawlink.py behind POST /api/evidence). Procurement's Worker calls this one through a
// service binding (wrangler [[services]] BILLS) with the shared secret LAW_SECRET, set on both Workers. Fails closed: without the
// secret on this Worker, or with a wrong one, the answer is 403 and nothing reaches the container. A sleeping container is started
// (1-3 min); the caller waits (owner, 2026-11-09: wait for cold starts, do not keep Bill Assistant warm).

const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
const MAX_BODY = 4000;

export async function secretMatches(got, want) {
  if (!got || !want) return false;
  const digest = async (s) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)));
  const [a, b] = await Promise.all([digest(got), digest(want)]);
  return a.reduce((diff, byte, i) => diff | (byte ^ b[i]), 0) === 0;
}

// upstream(request, env, {lane}) forwards to the container with the proxy secret (src/worker.js).
export async function lawEvidence(request, env, upstream) {
  if (request.method !== "POST" || !(await secretMatches(request.headers.get("X-Law-Secret"), env.LAW_SECRET)))
    return json({ error: "forbidden" }, 403);
  const raw = await request.text();
  let body = null;
  try { body = raw.length <= MAX_BODY ? JSON.parse(raw) : null; } catch { body = null; }
  if (!body || typeof body.query !== "string" || !body.query.trim()) return json({ error: "bad request" }, 400);
  const k = Math.max(1, Math.min(Number(body.k) || 4, 6));
  const url = new URL(request.url);
  url.pathname = "/api/evidence";
  const forward = new Request(url.toString(), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: body.query.slice(0, 1000), k }),
  });
  return upstream(forward, env, { lane: "procurement" });
}
