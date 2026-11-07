// What a person must accept once before using a citizenlk service, stored per account (email) in the shared accounts KV, not per Worker:
// accepting "general" on Bill Assistant also covers Procurement Rules; Procurement Rules asks for its own extra items once.
// Bump VERSION whenever the wording changes in substance: everyone is asked again. Not reviewed by a lawyer (TODO_LAUNCH.md, Phase 2).
// Every statement here must stay true of the code: what is logged and who sees it is in privacy.js.
export const VERSION = "2026-11-09";

const GENERAL = [
  { id: "accuracy", text: "Answers are written by AI from official documents. They can be incomplete, out of date or wrong. I will check the cited document before I rely on an answer." },
  { id: "not-advice", text: "This is not legal advice and not a government service. An answer is an explanation of a text, not an official interpretation, and nobody here is my lawyer." },
  { id: "data", text: 'My questions and the answers are saved and sent to third-party service providers outside Sri Lanka (see the <a href="/privacy" target="_blank" rel="noopener">privacy page</a>). I will not put in names, ID numbers or private details.' },
];

const EXTRA = {
  procurement: [
    { id: "procurement-official", text: "citizenlk.com is an independent service, not the National Procurement Commission or the Treasury, and its answers are not official guidance. For a real procurement, my entity's own guidelines, circulars, bidding documents and procurement committee decide. Circulars expire and rules change, so I will check that the text is still in force." },
    { id: "procurement-confidential", text: "I will not enter confidential procurement information: bids, evaluation reports, bidder names, or tender documents that are not public." },
    { id: "procurement-decision", text: "An answer will not be the only basis for awarding, rejecting or cancelling a bid, for approving a payment, or for an appeal. I stay responsible for my decisions." },
  ],
};

// What this service needs from an account before it answers: the general items plus its own.
export const required = (service) => [...GENERAL, ...(EXTRA[service] || [])];
export const needs = (service) => [{ key: "general", items: GENERAL }, ...(EXTRA[service] ? [{ key: service, items: EXTRA[service] }] : [])];

export const consentKey = (email) => `consent:${email}`;

export async function readConsent(env, email) {
  try { return JSON.parse((await env.AUTH.get(consentKey(email))) || "{}"); } catch { return {}; }
}

// True when the account has accepted the current version of every part this service needs.
export async function hasConsent(env, email, service) {
  const rec = await readConsent(env, email);
  return needs(service).every((n) => rec[n.key]?.v === VERSION);
}

// Accepting needs every item of every part the service needs; a partial list is refused. Parts already accepted at the current version keep their date.
export async function saveConsent(env, email, service, ids, nowMs) {
  const have = new Set(Array.isArray(ids) ? ids : []);
  if (!required(service).every((i) => have.has(i.id))) return false;
  const rec = await readConsent(env, email);
  for (const n of needs(service)) if (rec[n.key]?.v !== VERSION) rec[n.key] = { v: VERSION, at: new Date(nowMs).toISOString() };
  await env.AUTH.put(consentKey(email), JSON.stringify(rec)); // no expiry: it is the record that the person agreed
  return true;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

export function consentPage(service, serviceName, nowMs = Date.now()) {
  const boxes = required(service).map((i) => `<label><input type="checkbox" name="c" value="${i.id}"><span>${i.text}</span></label>`).join("\n    ");
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Before you start - citizenlk</title>
<style>
  :root { color-scheme: light dark; --lime:#F4F2EE; --ink:#1C1511; --muted:#6E6157; --line:#DAD3CA; --box:#FFFFFF; --sindoor:#A3241B; --ochre:#E0A93A; --err:#B3261E; }
  @media (prefers-color-scheme: dark) { :root { --lime:#15100D; --ink:#F1E6D6; --muted:#B3A391; --line:#3A2D24; --box:#211913; --sindoor:#E0675A; --ochre:#E8B550; --err:#FF8A80; } }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--lime); color: var(--ink); font: 400 1rem/1.6 Yaldevi, "Segoe UI", system-ui, sans-serif; }
  main { max-width: 640px; margin: 0 auto; padding: 32px max(16px, env(safe-area-inset-right, 0px)) 48px max(16px, env(safe-area-inset-left, 0px)); overflow-wrap: anywhere; }
  .mark { font: 800 1.5rem/1 "Abhaya Libre", Georgia, serif; color: var(--ink); text-decoration: none; } .mark b { color: var(--sindoor); }
  h1 { font: 800 1.8rem/1.2 "Abhaya Libre", Georgia, serif; margin: 20px 0 6px; }
  label { display: flex; gap: 12px; align-items: flex-start; padding: 12px 14px; margin: 10px 0; border: 1px solid var(--line); border-radius: 12px; background: var(--box); cursor: pointer; }
  input { margin-top: 5px; width: 18px; height: 18px; flex: none; accent-color: var(--sindoor); }
  button { margin-top: 12px; border: 0; border-radius: 999px; padding: 10px 22px; background: var(--ink); color: var(--lime); font: inherit; font-weight: 500; cursor: pointer; }
  button:disabled { opacity: .45; cursor: not-allowed; } button:not(:disabled):hover { background: var(--sindoor); color: #fff; }
  a { color: inherit; } :focus-visible { outline: 2px solid var(--ochre); outline-offset: 3px; }
  .muted { color: var(--muted); font-size: .9rem; } #msg { min-height: 1.5em; color: var(--err); font-size: .92rem; }
</style></head><body><main>
<a class="mark" href="https://citizenlk.com">citizen<b>lk</b></a>
<h1>Before you start</h1>
<p class="muted">${esc(serviceName)}. One account works on every citizenlk service. You are asked once; tick each line to continue.</p>
<form id="f">
    ${boxes}
  <button id="go" disabled>I agree, continue</button>
  <p id="msg" aria-live="polite"></p>
</form>
<p class="muted">Version ${VERSION}. <a href="/privacy">Privacy</a> · <a href="#" id="out">Sign out</a></p>
<script>
const boxes = [...document.querySelectorAll('input[name=c]')], go = document.getElementById('go');
boxes.forEach((b) => b.onchange = () => { go.disabled = !boxes.every((x) => x.checked); });
document.getElementById('f').onsubmit = async (e) => { e.preventDefault();
  const r = await fetch('/consent', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids: boxes.map((b) => b.value) }) });
  if (r.ok) location.href = '/'; else document.getElementById('msg').textContent = 'Could not save that. Please try again.'; };
document.getElementById('out').onclick = async (e) => { e.preventDefault(); try { await fetch('/logout', { method: 'POST' }); } catch (x) {} location.href = '/login'; };
</script></main></body></html>`;
}
