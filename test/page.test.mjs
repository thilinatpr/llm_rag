import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

// The chat page is one static file with an inline script. A syntax error there breaks the whole page and no other test would notice
// (a stray `//` comment once swallowed the rest of a line). Compile every inline script; nothing is run.
test("public/index.html: every inline script compiles", () => {
  const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.ok(scripts.length >= 1);
  for (const code of scripts) assert.doesNotThrow(() => new vm.Script(code, { filename: "index.html" }));
});

test("public/index.html: no public text names the languages the assistant answers in", () => {
  const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
  assert.doesNotMatch(html, /Ask in Sinhala|in Sinhala, Tamil or English|Sinhala, Tamil or English/);
});
