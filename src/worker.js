// Worker entrypoint: the login gate (auth.js) in front of the Python UI container.
// Kept to a default export plus the Durable Object class: workerd rejects any other non-handler named export.
import { getContainer } from "@cloudflare/containers";
import { createHandler } from "./auth.js";
import { checkBalance } from "./balance.js";
import { reportSettings } from "./public.js";
import { noindex } from "./seo.js";
import { lawEvidence } from "./lawlink.js";

export { BillragContainer } from "./container.js";
export { QuotaCounter } from "./quota-do.js";

// Signed-in requests are forwarded to the container with the shared secret; the browser's session cookie is not.
// opts.lane = "public" marks an anonymous visitor (public.js). A client-sent X-Lane is always dropped: only this Worker sets it.
async function upstream(request, env, opts = {}) {
  const headers = new Headers(request.headers);
  headers.delete("Cookie");
  headers.delete("X-Lane");
  if (opts.lane) headers.set("X-Lane", opts.lane);
  headers.set("X-Proxy-Secret", env.PROXY_SECRET);
  const started = Date.now();
  try {
    return await getContainer(env.BILLRAG).fetch(new Request(request, { headers }));
  } catch (e) {
    // Answer with a real status the page can show, instead of letting the connection drop.
    const path = new URL(request.url).pathname;
    console.error(JSON.stringify({ event: "upstream_failed", path, ms: Date.now() - started, error: String((e && e.message) || e) }));
    return new Response(JSON.stringify({ error: "The app server is not responding. If it was asleep, it is starting; try again in a minute." }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}

const site = noindex(createHandler({ upstream, privacy: "bills", cookie: "clk_session", consent: { service: "bills", name: "Bill Assistant" } }));

export default {
  // /internal/evidence: law passages for Procurement Rules (src/lawlink.js); everything else goes through the login gate
  fetch: (request, env, ctx) => (new URL(request.url).pathname === "/internal/evidence" ? lawEvidence(request, env, upstream) : site(request, env, ctx)),
  scheduled: (_event, env, ctx) => ctx.waitUntil(Promise.all([checkBalance(env), reportSettings(env)])), // hourly: see [triggers] in wrangler.toml
};
