// Worker entrypoint: the login gate (auth.js) in front of the Python UI container.
// Kept to a default export plus the Durable Object class: workerd rejects any other non-handler named export.
import { getContainer } from "@cloudflare/containers";
import { createHandler } from "./auth.js";

export { BillragContainer } from "./container.js";

// Signed-in requests are forwarded to the container with the shared secret; the browser's session cookie is not.
async function upstream(request, env) {
  const headers = new Headers(request.headers);
  headers.delete("Cookie");
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

export default { fetch: createHandler({ upstream }) };
