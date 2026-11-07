// 30-day chat history with delete: src/history.js, routed in auth.js behind sign-in and consent.
import test from "node:test";
import assert from "node:assert/strict";
import { createHandler } from "../src/auth.js";
import { required } from "../src/consent.js";

// A KV with list, metadata and expiry, like the real one.
function kv(pageSize = 1000) {
  const m = new Map();
  return { m,
    get: async (k) => m.get(k)?.v ?? null,
    put: async (k, v, o = {}) => void m.set(k, { v, ttl: o.expirationTtl, meta: o.metadata }),
    delete: async (k) => void m.delete(k),
    list: async ({ prefix = "", limit = 1000, cursor } = {}) => { // the cursor is the last key returned, as in the real store
      let all = [...m].filter(([k]) => k.startsWith(prefix)).map(([name, e]) => ({ name, metadata: e.meta })).sort((x, y) => (x.name < y.name ? -1 : 1));
      if (cursor) all = all.filter((k) => k.name > cursor);
      const keys = all.slice(0, Math.min(limit, pageSize)), done = keys.length === all.length;
      return { keys, list_complete: done, cursor: done ? undefined : keys.at(-1).name };
    } };
}
function world(emails = "a@example.com,b@example.com", pageSize = 1000) {
  const AUTH = kv(pageSize), sent = [], clock = { t: Date.parse("2026-11-09T06:00:00Z") };
  const env = { AUTH, OTP_PEPPER: "p", ALLOWED_EMAILS: emails, FROM_ADDRESS: "x@y.z", ASSETS: { fetch: async () => new Response("page") } };
  const mk = (service) => { const h = createHandler({ cookie: "clk_session", privacy: service, consent: { service, name: service }, now: () => clock.t, sendMail: async (_e, to, code) => void sent.push({ to, code }) });
    return (path, { method = "GET", body, cookie, origin } = {}) => h(new Request("https://bills.test" + path, { method, body: body && JSON.stringify(body), headers: { ...(cookie && { Cookie: cookie }), ...(origin && { Origin: origin }) } }), env); };
  const bills = mk("bills"), proc = mk("procurement");
  const signIn = async (email, consent = true) => {
    await bills("/login/request", { method: "POST", body: { email } });
    const r = await bills("/login/verify", { method: "POST", body: { email, code: sent.filter((s) => s.to === email).at(-1).code } });
    const cookie = r.headers.get("Set-Cookie").split(";")[0];
    if (consent) { await bills("/consent", { method: "POST", cookie, body: { ids: required("bills").map((i) => i.id) } }); await proc("/consent", { method: "POST", cookie, body: { ids: required("procurement").map((i) => i.id) } }); }
    return cookie;
  };
  return { AUTH, clock, bills, proc, signIn };
}
const chat = (n = 1) => ({ turns: Array.from({ length: n }, (_, i) => ({ q: `What is clause ${i}?`, a: `It says ${i}.` })) });
const put = (w, cookie, id, body = chat()) => w.bills(`/history/${id}`, { method: "PUT", cookie, body });

test("a saved chat comes back, is listed newest first with a title, and expires after 30 days", async () => {
  const w = world();
  const c = await w.signIn("a@example.com");
  assert.equal((await put(w, c, "chat-one", chat(2))).status, 200);
  w.clock.t += 3600_000;
  await put(w, c, "chat-two");
  const list = await (await w.bills("/history", { cookie: c })).json();
  assert.equal(list.days, 30);
  assert.deepEqual(list.chats.map((x) => x.id), ["chat-two", "chat-one"]);
  assert.equal(list.chats[1].title, "What is clause 0?");
  assert.equal(list.chats[1].n, 2);
  assert.deepEqual((await (await w.bills("/history/chat-one", { cookie: c })).json()).turns[1], { q: "What is clause 1?", a: "It says 1." });
  for (const [k, e] of w.AUTH.m) if (k.startsWith("hist:")) assert.equal(e.ttl, 30 * 24 * 3600);
});

test("delete one chat, then delete everything (across list pages); the account's other data stays", async () => {
  const w = world("a@example.com", 3); // small pages: delete-all has to follow the cursor
  const c = await w.signIn("a@example.com");
  for (let i = 0; i < 7; i++) await put(w, c, `chat-${i}x`);
  assert.equal((await w.bills("/history/chat-0x", { method: "DELETE", cookie: c })).status, 200);
  assert.equal((await w.bills("/history/chat-0x", { cookie: c })).status, 404);
  const gone = await (await w.bills("/history", { method: "DELETE", cookie: c })).json();
  assert.equal(gone.deleted, 6);
  assert.equal((await (await w.bills("/history", { cookie: c })).json()).chats.length, 0);
  assert.ok(w.AUTH.m.has("consent:a@example.com"));
});

test("nobody can read, overwrite or delete another account's chats, and the two services keep separate histories", async () => {
  const w = world();
  const a = await w.signIn("a@example.com"), b = await w.signIn("b@example.com");
  await put(w, a, "chat-aaa", chat());
  assert.equal((await w.bills("/history/chat-aaa", { cookie: b })).status, 404);
  assert.equal((await put(w, b, "chat-aaa", { turns: [{ q: "mine", a: "mine" }] })).status, 200);       // same id, b's own chat
  assert.equal((await (await w.bills("/history/chat-aaa", { cookie: a })).json()).turns[0].q, "What is clause 0?");
  await w.bills("/history/chat-aaa", { method: "DELETE", cookie: b });
  assert.equal((await w.bills("/history/chat-aaa", { cookie: a })).status, 200);
  assert.equal((await w.proc("/history/chat-aaa", { cookie: a })).status, 404);                         // Procurement Rules history is its own
  assert.equal((await w.bills("/history", { cookie: "" })).status, 401);
  for (const k of w.AUTH.m.keys()) assert.ok(!k.includes("example.com") || k.startsWith("consent:") || k.startsWith("session:") === false, k); // keys hold a hash, never the address
  assert.ok(![...w.AUTH.m.keys()].some((k) => k.startsWith("hist:") && k.includes("@")));
});

test("a chat needs consent first, a write from another site is refused, and bad input is not stored", async () => {
  const w = world();
  const noConsent = await w.signIn("a@example.com", false);
  assert.equal((await put(w, noConsent, "chat-aaa")).status, 403);
  const c = await w.signIn("b@example.com");
  assert.equal((await w.bills("/history/chat-aaa", { method: "PUT", cookie: c, origin: "https://evil.test", body: chat() })).status, 403);
  assert.equal((await w.bills("/history/chat-aaa", { method: "DELETE", cookie: c, origin: "https://evil.test" })).status, 403);
  for (const bad of [{}, { turns: [] }, { turns: [{ q: 1, a: "x" }] }, { turns: [{ q: " ", a: "x" }] }, chat(61), { turns: Array.from({ length: 60 }, () => ({ q: "q", a: "x".repeat(20000) })) }])
    assert.equal((await put(w, c, "chat-aaa", bad)).status, 400);
  assert.equal((await put(w, c, "a/../b")).status, 404);
  assert.equal((await put(w, c, "short")).status, 404);
  assert.equal([...w.AUTH.m.keys()].filter((k) => k.startsWith("hist:")).length, 0);
  assert.equal((await put(w, c, "chat-aaa", { turns: [{ q: "q".repeat(5000), a: "a" }] })).status, 200);
  assert.equal((await (await w.bills("/history/chat-aaa", { cookie: c })).json()).turns[0].q.length, 1000);
});

test("50 chats at most: a 51st is refused with a clear reason, but an existing chat can still be added to", async () => {
  const w = world();
  const c = await w.signIn("a@example.com");
  for (let i = 0; i < 50; i++) assert.equal((await put(w, c, `chat-${String(i).padStart(3, "0")}`)).status, 200);
  const full = await put(w, c, "chat-new1");
  assert.equal(full.status, 409);
  assert.equal((await full.json()).reason, "full");
  assert.equal((await put(w, c, "chat-007", chat(3))).status, 200);
});
