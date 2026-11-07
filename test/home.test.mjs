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

test("homepage has the SEO basics: description, canonical, Open Graph, valid JSON-LD", () => {
  const html = readFileSync(new URL("../site/index.html", import.meta.url), "utf8");
  assert.match(html, /<meta name="description" content="[^"]{50,}"/);
  assert.ok(html.includes('<link rel="canonical" href="https://citizenlk.com/">'));
  assert.ok(html.includes('property="og:title"'));
  const ld = html.match(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/);
  assert.ok(ld, "JSON-LD present");
  const graph = JSON.parse(ld[1])["@graph"];
  assert.deepEqual(graph.map((n) => n["@type"]), ["Organization", "WebSite", "WebApplication", "WebApplication"]);
  assert.equal((html.match(/<h1[ >]/g) || []).length, 1);
});

test("robots.txt and sitemap.xml point at the bare domain", () => {
  const robots = readFileSync(new URL("../site/robots.txt", import.meta.url), "utf8");
  const sitemap = readFileSync(new URL("../site/sitemap.xml", import.meta.url), "utf8");
  assert.ok(robots.includes("Sitemap: https://citizenlk.com/sitemap.xml"));
  assert.ok(!sitemap.includes("www.citizenlk.com") && sitemap.includes("<loc>https://citizenlk.com/</loc>"));
});
