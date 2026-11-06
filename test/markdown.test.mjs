import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The renderer lives inline in public/index.html (a single static page); pull it out between the markers.
const html = readFileSync(new URL("../public/index.html", import.meta.url), "utf8");
const code = html.slice(html.indexOf("// --- md:start ---"), html.indexOf("// --- md:end ---"));
const renderMarkdown = new Function(code + "\nreturn renderMarkdown;")();

test("bold, bullets and citations render without * marks", () => {
  const out = renderMarkdown("The bill **allows** detention [070/2026 §3].\n\n* **Time limit:** 72 hours [070/2026 §5]\n* *Penalty*: a fine");
  assert.equal(out, '<p>The bill <strong>allows</strong> detention <span class="cite">[070/2026 §3]</span>.</p>' +
    '<ul><li><strong>Time limit:</strong> 72 hours <span class="cite">[070/2026 §5]</span></li><li><em>Penalty</em>: a fine</li></ul>');
  assert.ok(!out.includes("*"));
});

test("HTML in the answer is escaped", () => {
  const out = renderMarkdown('<img src=x onerror="alert(1)"> **b**');
  assert.ok(!out.includes("<img"));
  assert.match(out, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt; <strong>b<\/strong>/);
});

test("numbered lists, headings, tables, stray stars, code", () => {
  assert.equal(renderMarkdown("1. one\n2. two"), "<ol><li>one</li><li>two</li></ol>");
  assert.equal(renderMarkdown("### Caveats\ntext"), "<h4>Caveats</h4><p>text</p>");
  assert.equal(renderMarkdown("| a | b |\n|---|---|\n| 1 | **2** |"),
    "<table><tr><th>a</th><th>b</th></tr><tr><td>1</td><td><strong>2</strong></td></tr></table>");
  assert.equal(renderMarkdown("odd ** marks * here"), "<p>odd  marks  here</p>");
  assert.equal(renderMarkdown("use `a*b` ok"), "<p>use <code>a*b</code> ok</p>");
  assert.equal(renderMarkdown("line one\nline two"), "<p>line one<br>line two</p>");
});

test("Sinhala text and a list continuation line", () => {
  assert.equal(renderMarkdown("- **භීෂණය** [070/2026 §3]\n  more"), '<ul><li><strong>භීෂණය</strong> <span class="cite">[070/2026 §3]</span> more</li></ul>');
});

test("citations are wrapped for styling, full-width brackets too, and other brackets are left alone", () => {
  assert.equal(renderMarkdown("see 【078/2026 §13(1)】"), '<p>see <span class="cite">【078/2026 §13(1)】</span></p>');
  assert.equal(renderMarkdown("a [note] and [1]"), "<p>a [note] and [1]</p>");
});
