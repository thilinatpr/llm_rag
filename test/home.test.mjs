import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import worker from "../workers/home/worker.js";

const env = { ASSETS: { fetch: async () => new Response("home page", { headers: { "Content-Type": "text/html" } }) } };

test("homepage is served with security headers and no login", async () => {
  const r = await worker.fetch(new Request("https://citizenlk.com/"), env);
  assert.equal(r.status, 200);
  assert.equal(await r.text(), "home page");
  assert.equal(r.headers.get("X-Content-Type-Options"), "nosniff");
  assert.equal(r.headers.get("Content-Type"), "text/html");
});

test("www redirects to the bare domain and keeps the path", async () => {
  const r = await worker.fetch(new Request("https://www.citizenlk.com/about?x=1"), env);
  assert.equal(r.status, 301);
  assert.equal(r.headers.get("Location"), "https://citizenlk.com/about?x=1");
});

test("homepage links to the service domain, not workers.dev", () => {
  const html = readFileSync(new URL("../site/index.html", import.meta.url), "utf8");
  assert.ok(html.includes("https://bills.citizenlk.com"));
  assert.ok(!html.includes("workers.dev"));
});
