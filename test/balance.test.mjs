import test from "node:test";
import assert from "node:assert/strict";
import { checkBalance } from "../src/balance.js";

function setup(extra = {}) {
  const m = new Map();
  const AUTH = { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v), delete: async (k) => void m.delete(k) };
  const mails = [];
  const env = { AUTH, DEEPSEEK_API_KEY: "k", ALERT_EMAIL: "me@example.com", FROM_ADDRESS: "a@b.c", BALANCE_ALERT_USD: "5", ...extra };
  const run = (reply) => checkBalance(env, { fetchFn: async (url, init) => { assert.equal(init.headers.Authorization, "Bearer k"); return reply(); },
    send: async (_e, msg) => void mails.push(msg) });
  return { run, mails, m };
}
const balance = (total, is_available = true) => () => new Response(JSON.stringify({ is_available, balance_infos: [{ currency: "USD", total_balance: total }] }));

test("credit above the limit: no mail", async () => {
  const t = setup();
  assert.equal((await t.run(balance("5.00"))).state, "ok");
  assert.equal(t.mails.length, 0);
});

test("credit below the limit: one mail a day, to the alert address, saying how much is left", async () => {
  const t = setup();
  assert.deepEqual(await t.run(balance("4.86")), { state: "low", balance: 4.86, currency: "USD", limit: 5, mailed: true });
  assert.equal(t.mails[0].to, "me@example.com");
  assert.match(t.mails[0].subject, /4\.86 USD/);
  assert.equal((await t.run(balance("4.80"))).mailed, false);
  assert.equal(t.mails.length, 1);
});

test("DeepSeek saying the account cannot be used counts as low, whatever the number", async () => {
  const t = setup();
  assert.equal((await t.run(balance("9", false))).state, "low");
});

test("failed checks mail only after 3 in a row, and a good check resets the count", async () => {
  const t = setup();
  const down = () => new Response("no", { status: 401 });
  await t.run(down); await t.run(down);
  await t.run(balance("9"));
  await t.run(down); await t.run(down);
  assert.equal(t.mails.length, 0);
  const third = await t.run(down);
  assert.equal(third.failures, 3);
  assert.match(third.error, /401/);
  assert.equal(t.mails.length, 1);
  assert.equal((await t.run(down)).mailed, false);
});

test("not configured: does nothing", async () => {
  assert.equal((await setup({ DEEPSEEK_API_KEY: undefined }).run(balance("1"))).state, "off");
  assert.equal((await setup({ ALERT_EMAIL: undefined }).run(balance("1"))).state, "off");
});
