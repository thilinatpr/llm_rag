// One account across services: a domain-wide session cookie, one shared KV, and a consent step. See src/consent.js and auth.js.
import test from "node:test";
import assert from "node:assert/strict";
import { createHandler } from "../src/auth.js";
import { VERSION, hasConsent, required } from "../src/consent.js";

const kv = () => { const m = new Map(); return { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k), m }; };
const BILLS = { service: "bills", name: "Bill Assistant" }, PROC = { service: "procurement", name: "Procurement Rules" };

// Two Workers (two handlers, two hosts) over ONE KV, like bills.citizenlk.com and procurement.citizenlk.com.
function world() {
  const AUTH = kv(), sent = [];
  const env = { AUTH, OTP_PEPPER: "p", ALLOWED_EMAILS: "me@example.com", FROM_ADDRESS: "a@b.c", COOKIE_DOMAIN: "citizenlk.com", ASSETS: { fetch: async () => new Response("page") } };
  const mk = (host, consent) => {
    const h = createHandler({ cookie: "clk_session", consent, privacy: consent.service, sendMail: async (_e, to, code) => void sent.push(code) });
    return (path, { method = "GET", body, cookie, accept } = {}) => h(new Request(`https://${host}${path}`, { method, body: body && JSON.stringify(body),
      headers: { ...(cookie && { Cookie: cookie }), ...(accept && { Accept: accept }) } }), env);
  };
  return { AUTH, sent, bills: mk("bills.citizenlk.com", BILLS), proc: mk("procurement.citizenlk.com", PROC) };
}
const signIn = async (w) => {
  await w.bills("/login/request", { method: "POST", body: { email: "me@example.com" } });
  const r = await w.bills("/login/verify", { method: "POST", body: { email: "me@example.com", code: w.sent[0] } });
  return { cookie: r.headers.get("Set-Cookie").split(";")[0], header: r.headers.get("Set-Cookie") };
};
const accept = (call, service, cookie) => call("/consent", { method: "POST", cookie, body: { ids: required(service).map((i) => i.id) } });

test("the session cookie is valid on every subdomain, and the same cookie works on both services", async () => {
  const w = world();
  const { cookie, header } = await signIn(w);
  assert.match(header, /Domain=citizenlk\.com/);
  assert.match(header, /HttpOnly/);
  assert.match(cookie, /^clk_session=[0-9a-f]{64}$/);
  assert.equal((await accept(w.bills, "bills", cookie)).status, 200);
  assert.equal((await w.bills("/", { cookie })).status, 200);
  // Procurement accepts the same cookie (no second sign-in) but asks for its own extra statements once.
  const redirected = await w.proc("/", { cookie, accept: "text/html" });
  assert.equal(redirected.status, 302);
  assert.match(redirected.headers.get("Location"), /\/consent$/);
  assert.equal((await accept(w.proc, "procurement", cookie)).status, 200);
  assert.equal((await w.proc("/", { cookie })).status, 200);
});

test("nothing is answered before consent: pages redirect, the API gets a 403 the page can read", async () => {
  const w = world();
  const { cookie } = await signIn(w);
  const page = await w.bills("/", { cookie, accept: "text/html" });
  assert.equal(page.status, 302);
  const api = await w.bills("/api/ask", { method: "POST", cookie });
  assert.equal(api.status, 403);
  assert.equal((await api.json()).consent_required, true);
  assert.equal((await w.bills("/privacy", { cookie })).status, 200);
  assert.equal((await w.bills("/consent", { cookie })).status, 200);
  assert.equal((await w.bills("/consent", { accept: "text/html" })).status, 302); // signed out: to the sign-in page
});

test("every statement is required, nothing partial is stored, and the record carries the version and date", async () => {
  const w = world();
  const { cookie } = await signIn(w);
  const ids = required("bills").map((i) => i.id);
  assert.equal((await w.bills("/consent", { method: "POST", cookie, body: { ids: ids.slice(1) } })).status, 400);
  assert.equal((await w.bills("/consent", { method: "POST", cookie, body: {} })).status, 400);
  assert.equal(w.AUTH.m.has("consent:me@example.com"), false);
  assert.equal((await w.bills("/consent", { method: "POST", cookie, body: { ids } })).status, 200);
  const rec = JSON.parse(w.AUTH.m.get("consent:me@example.com"));
  assert.equal(rec.general.v, VERSION);
  assert.ok(!Number.isNaN(Date.parse(rec.general.at)));
  assert.equal(rec.procurement, undefined);                       // Bill Assistant's consent does not cover Procurement Rules' extras
  assert.equal(await hasConsent({ AUTH: w.AUTH }, "me@example.com", "procurement"), false);
  assert.equal(await hasConsent({ AUTH: w.AUTH }, "me@example.com", "bills"), true);
});

test("changed wording asks again; an old version does not count", async () => {
  const w = world();
  const { cookie } = await signIn(w);
  await accept(w.bills, "bills", cookie);
  w.AUTH.m.set("consent:me@example.com", JSON.stringify({ general: { v: "2020-01-01" } }));
  assert.equal((await w.bills("/api/ask", { method: "POST", cookie })).status, 403);
});

test("logout ends the session on both services and clears the domain cookie", async () => {
  const w = world();
  const { cookie } = await signIn(w);
  await accept(w.bills, "bills", cookie); await accept(w.proc, "procurement", cookie);
  const out = await w.proc("/logout", { method: "POST", cookie });
  assert.match(out.headers.get("Set-Cookie"), /Max-Age=0.*Domain=citizenlk\.com/);
  assert.equal((await w.bills("/", { cookie })).status, 401);
});

test("a handler with no consent option (the admin panel) and no domain keeps the old host-only `session` cookie", async () => {
  const env = { AUTH: kv(), OTP_PEPPER: "p", ALLOWED_EMAILS: "me@example.com", FROM_ADDRESS: "a@b.c" }, sent = [];
  const h = createHandler({ sendMail: async (_e, _t, c) => void sent.push(c), app: async (_r, _e, user) => new Response(user || "none") });
  const call = (p, o = {}) => h(new Request("https://admin.test" + p, o), env);
  await call("/login/request", { method: "POST", body: JSON.stringify({ email: "me@example.com" }) });
  const r = await call("/login/verify", { method: "POST", body: JSON.stringify({ email: "me@example.com", code: sent[0] }) });
  assert.match(r.headers.get("Set-Cookie"), /^session=[0-9a-f]{64}; Path=\/; HttpOnly/);
  assert.doesNotMatch(r.headers.get("Set-Cookie"), /Domain=/);
  const cookie = r.headers.get("Set-Cookie").split(";")[0];
  assert.equal(await (await call("/", { headers: { Cookie: cookie } })).text(), "me@example.com");
});

test("the consent page names the service, lists every statement, and Procurement Rules asks for more than Bill Assistant", async () => {
  const w = world();
  const { cookie } = await signIn(w);
  const b = await (await w.bills("/consent", { cookie })).text(), p = await (await w.proc("/consent", { cookie })).text();
  assert.ok(b.includes("Bill Assistant") && p.includes("Procurement Rules"));
  assert.equal((b.match(/name="c"/g) || []).length, required("bills").length);
  assert.ok((p.match(/name="c"/g) || []).length > (b.match(/name="c"/g) || []).length);
  for (const word of ["not legal advice", "incomplete, out of date or wrong"]) assert.ok(b.includes(word), word);
  assert.ok(p.includes("confidential procurement") && p.includes("circulars"));
  assert.ok(!/[඀-෿஀-௿]/.test(b + p));
});
