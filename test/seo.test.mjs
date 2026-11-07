import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { noindex, NOINDEX } from "../src/seo.js";

const ok = async () => new Response("page", { headers: { "Content-Type": "text/html" } });

test("every response carries noindex, including redirects (immutable headers) and errors", async () => {
  const h = noindex(async (req) => (new URL(req.url).pathname === "/r" ? Response.redirect("https://x.test/", 302) : new Response("no", { status: 502 })));
  for (const path of ["/r", "/anything"]) {
    const r = await h(new Request("https://bills.citizenlk.com" + path));
    assert.equal(r.headers.get("X-Robots-Tag"), NOINDEX);
  }
  assert.equal((await h(new Request("https://bills.citizenlk.com/r"))).status, 302);
});

test("the body and other headers pass through unchanged, and extra arguments reach the handler", async () => {
  let seen;
  const h = noindex(async (req, env, ctx) => { seen = [env, ctx]; return ok(); });
  const r = await h(new Request("https://x.test/"), "env", "ctx");
  assert.equal(await r.text(), "page");
  assert.equal(r.headers.get("Content-Type"), "text/html");
  assert.deepEqual(seen, ["env", "ctx"]);
});

test("robots.txt: chat services stay fetchable (so noindex is seen), the admin panel disallows everything", async () => {
  const chat = await noindex(ok)(new Request("https://bills.citizenlk.com/robots.txt"));
  assert.equal(await chat.text(), "User-agent: *\nAllow: /\n");
  const admin = await noindex(ok, { disallowAll: true })(new Request("https://admin.citizenlk.com/robots.txt"));
  assert.equal(await admin.text(), "User-agent: *\nDisallow: /\n");
  assert.equal(admin.headers.get("X-Robots-Tag"), NOINDEX);
  const post = await noindex(ok)(new Request("https://bills.citizenlk.com/robots.txt", { method: "POST" }));
  assert.equal(await post.text(), "page"); // only GET and HEAD are answered here
});

test("all three Worker entrypoints are wrapped", () => {
  for (const f of ["../src/worker.js", "../workers/admin/worker.js", "../workers/procurement/worker.js"])
    assert.match(readFileSync(new URL(f, import.meta.url), "utf8"), /noindex\(/, f);
});
