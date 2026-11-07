// citizenlk-admin: the admin panel at admin.citizenlk.com, its own Worker so nothing here can touch Bill Assistant's traffic or deploys.
// Exports only the default handler (see the runbook in AGENTS.md). No container of its own, and it never sends a request to one:
// the bindings to the other Workers' Durable Objects are used only for stored state and the public question counter.
import { createAdmin } from "../../src/admin.js";
import { refreshStats } from "../../src/admin-stats.js";
import { createHandler } from "../../src/auth.js";

// The instance name @cloudflare/containers uses for getContainer(binding). getState() reads stored state: it does not start a sleeping
// container or reset its idle timer.
const SINGLETON = "cf-singleton-container";
const containerState = (env, binding) => env[binding].get(env[binding].idFromName(SINGLETON)).getState();

export default {
  fetch: createHandler({ app: createAdmin({ containerState }), loginTitle: "Sign in to the admin panel" }),
  scheduled: (_event, env, ctx) => ctx.waitUntil(refreshStats(env)), // keeps the counts moving when nobody opens the panel
};
