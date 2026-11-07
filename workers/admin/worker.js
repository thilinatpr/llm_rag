// citizenlk-admin: the admin panel at admin.citizenlk.com, its own Worker so nothing here can touch Bill Assistant's traffic or deploys.
// Exports only the default handler (see the runbook in AGENTS.md). No container of its own, and it never sends a request to one:
// the bindings to the other Workers' Durable Objects are used only for stored state and the public question counter.
import { createAdmin } from "../../src/admin.js";
import { refreshStats } from "../../src/admin-stats.js";
import { createHandler } from "../../src/auth.js";
import { CHECK_CRON, detect } from "../../src/refresh.js";
import { noindex } from "../../src/seo.js";

// The instance name @cloudflare/containers uses for getContainer(binding). getState() reads stored state: it does not start a sleeping
// container or reset its idle timer.
const SINGLETON = "cf-singleton-container";
const containerState = (env, binding) => env[binding].get(env[binding].idFromName(SINGLETON)).getState();

export default {
  fetch: noindex(createHandler({ app: createAdmin({ containerState }), loginTitle: "Sign in to the admin panel" }), { disallowAll: true }),
  // Hourly: keeps the counts moving when nobody opens the panel. Daily (CHECK_CRON): looks for new bills on documents.gov.lk (src/refresh.js).
  scheduled: (event, env, ctx) => ctx.waitUntil(event.cron === CHECK_CRON ? detect(env) : refreshStats(env)),
};
