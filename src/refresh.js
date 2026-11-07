// New bills: a daily check and the buttons that act on it, for the admin panel (src/admin.js, workers/admin/worker.js).
//
// A Worker cannot run scripts/refresh_bills.py (Python, PyMuPDF, ~700 MB of PDFs, an embedding run), so the work is split:
//   1. This file, daily (cron): replay documents.gov.lk's listing and compare it with bills/indexed-keys.json in R2 (the keys of the index
//      that is live, written by scripts/upload_r2.py when it publishes). What is listed and neither indexed, skipped by the last build nor
//      ignored by hand is "pending": the panel's To do list says so.
//   2. Build (button): starts .github/workflows/refresh-index.yml on GitHub Actions. That runs refresh_bills.py WITHOUT publishing, stages the
//      built pair in R2 under refresh/staging/ and writes refresh/status.json (scripts/refresh_r2.py).
//   3. Publish (second button): the same workflow promotes the staged pair with upload_r2.py's checked swap. Discard deletes the staged pair.
// The job reports through R2 (the panel already has the bucket), so there is no inbound endpoint to protect.
// State in KV (this Worker's own AUTH namespace): refresh:state = {check, seen, run}.

export const STATE_KEY = "refresh:state";
export const MANIFEST_KEY = "bills/indexed-keys.json"; // {at, keys[]}: documents in the live index
export const STATUS_KEY = "refresh/status.json"; //     the job's own report (see phaseOf)
export const SKIPPED_KEY = "refresh/skipped.json"; //   {at, keys[]}: listed, but the last build could not index them (no English PDF, unparsable)
export const STAGING_PREFIX = "refresh/staging/";
export const WORKFLOW = "refresh-index.yml";
export const CHECK_CRON = "40 2 * * *"; // 08:10 in Sri Lanka; the hourly cron does not run it
const SITE = "https://documents.gov.lk";
// The Next.js server-action id of the listing pages, the same one scripts/scrape_bills.py uses (DEFAULT_ACTION_ID). When the site redeploys
// it changes: the check then says so, and the new id goes in the DOCS_ACTION_ID var.
export const DEFAULT_ACTION_ID = "40dd03742cae2465a031936880def365fdd014c22b";
const SOURCES = {
  bills: { page: "/web/bills", api: "http://gvp-api:4500/website-data/bill/get-all" },
  acts: { page: "/web/acts", api: "http://gvp-api:4500/website-data/act/get-all" },
};
const ACTS_SINCE = 2024; // billrag/acts.py ACT_SINCE: older Acts are not indexed
const MAX_PAGES = 80; // ~52 pages of 100 today
const MAX_PENDING = 200;
const STARTING_MS = 30 * 60e3; // dispatched, but the job has not written "running"
const RUNNING_MS = 3 * 3600e3; //  a build normally takes 20-40 min
const CHECK_STALE_MS = 36 * 3600e3;
const squeeze = (s) => String(s ?? "").split(/\s+/).filter(Boolean).join(" ");

export const readJson = async (obj) => (obj ? obj.json() : null);

// ---------- the listing ----------
export function parseListing(text) {
  for (const line of String(text).split("\n")) if (line.startsWith("1:")) return JSON.parse(line.slice(2));
  throw new Error(`Unexpected response from documents.gov.lk (the action id may be stale): ${squeeze(text).slice(0, 120)}`);
}

async function fetchPage(source, page, { actionId, fetchFn }) {
  const s = SOURCES[source];
  const res = await fetchFn(`${SITE}${s.page}`, {
    method: "POST",
    headers: { "Next-Action": actionId, Accept: "text/x-component", "Content-Type": "text/plain;charset=UTF-8", "User-Agent": "citizenlk-admin/1.0 (+https://citizenlk.com)" },
    body: JSON.stringify([{ apiEndpoint: s.api, page, limit: 100, q: "", search: "", forwarded: {} }]),
  });
  if (!res.ok) throw new Error(`documents.gov.lk answered ${res.status} for the ${source} listing`);
  return parseListing(await res.text());
}

export async function fetchListing(source, { actionId = DEFAULT_ACTION_ID, fetchFn = fetch } = {}) {
  const first = await fetchPage(source, 1, { actionId, fetchFn });
  const rows = [...first.data];
  const pages = Math.min(Number(first.pagination?.totalPages) || 1, MAX_PAGES);
  for (let p = 2; p <= pages; p++) rows.push(...(await fetchPage(source, p, { actionId, fetchFn })).data);
  return rows;
}

// {key: {title, kind}} for what the index would hold: bills from last year on (refresh_bills.py --since-year), Acts from 2024.
// Keys are the index's own: "NNN/YYYY" for a bill (billrag/store.py bill_key), "Act N/YYYY" for an Act (billrag/acts.py act_key).
export function listingKeys(source, rows, { now }) {
  const out = {};
  const since = new Date(now).getUTCFullYear() - 1;
  for (const r of rows) {
    if (source === "bills") {
      if (!Number.isInteger(Number(r.billNo)) || !Number(r.billSubNo) || Number((r.date || "").slice(0, 4)) < since) continue;
      out[`${String(Number(r.billNo)).padStart(3, "0")}/${Number(r.billSubNo)}`] = { title: squeeze(r.descriptionEnglish), kind: "bill" };
    } else {
      if (!Number(r.actNo) || Number(r.actSubNo) < Math.max(ACTS_SINCE, since)) continue;
      out[`Act ${Number(r.actNo)}/${Number(r.actSubNo)}`] = { title: squeeze(r.descriptionEnglish), kind: "Act" };
    }
  }
  return out;
}

// ---------- state ----------
export async function readState(env) {
  try {
    return JSON.parse((await env.AUTH.get(STATE_KEY)) || "null") || {};
  } catch {
    return {};
  }
}
const saveState = (env, state) => env.AUTH.put(STATE_KEY, JSON.stringify(state));

// The daily check. Never throws: a failure is recorded (and shown on the panel) and the last good list of pending documents is kept.
export async function detect(env, { now = Date.now(), fetchFn = fetch } = {}) {
  const state = await readState(env);
  const at = new Date(now).toISOString();
  const keep = state.check?.pending || {};
  try {
    if (!env.LOGS) throw new Error("no LOGS binding");
    const manifest = await readJson(await env.LOGS.get(MANIFEST_KEY));
    if (!manifest?.keys) {
      state.check = { at, noManifest: true, pending: keep };
      return await saveState(env, state), state.check;
    }
    const skipped = await readJson(await env.LOGS.get(SKIPPED_KEY));
    const done = new Set([...manifest.keys, ...(skipped?.keys || []), ...Object.keys(state.seen || {})]);
    const actionId = env.DOCS_ACTION_ID || DEFAULT_ACTION_ID;
    const listed = {};
    for (const source of Object.keys(SOURCES)) Object.assign(listed, listingKeys(source, await fetchListing(source, { actionId, fetchFn }), { now }));
    const pending = {};
    for (const [key, v] of Object.entries(listed)) if (!done.has(key) && Object.keys(pending).length < MAX_PENDING) pending[key] = v;
    state.check = { at, listed: Object.keys(listed).length, pending };
  } catch (e) {
    state.check = { at: state.check?.at, failedAt: at, error: String((e && e.message) || e), pending: keep };
  }
  await saveState(env, state);
  return state.check;
}

export async function readStatus(env) {
  try {
    return env.LOGS ? await readJson(await env.LOGS.get(STATUS_KEY)) : null;
  } catch (e) {
    return { failed: String((e && e.message) || e) };
  }
}

// What the panel should say. state = KV refresh:state, status = the job's report (R2), both may be empty.
//   starting  dispatched, the job has not reported yet        running  the job is working
//   stuck     starting or running for too long                  built    finished, checks passed, waiting for Publish / Discard
//   failed    the job stopped; nothing was published           idle     nothing in progress (status may be published / nothing_new / discarded)
export function phaseOf(state, status, now) {
  const run = state?.run;
  const t = (x) => Date.parse(x) || 0;
  if (status?.failed) return "unknown";
  if (run && t(run.at) > t(status?.at)) return now - t(run.at) > STARTING_MS ? "stuck" : "starting";
  if (status?.state === "running") return now - t(status.at) > RUNNING_MS ? "stuck" : "running";
  if (status?.state === "built") return "built";
  if (status?.state === "failed") return "failed";
  return "idle";
}

// ---------- the buttons ----------
export async function dispatch(env, mode, { fetchFn = fetch } = {}) {
  const repo = env.GITHUB_REPO, token = env.GITHUB_DISPATCH_TOKEN;
  if (!repo || !token) return { ok: false, error: "Not set up: add the GITHUB_DISPATCH_TOKEN secret (fine-grained, this repository only, Actions: read and write) and the GITHUB_REPO variable." };
  const res = await fetchFn(`https://api.github.com/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "citizenlk-admin", "Content-Type": "application/json" },
    body: JSON.stringify({ ref: env.GITHUB_REF || "main", inputs: { mode } }),
  });
  if (res.status === 204) return { ok: true };
  const body = squeeze(await res.text().catch(() => "")).slice(0, 160);
  return { ok: false, error: `GitHub answered ${res.status}${body ? `: ${body}` : ""}` };
}

export const actionsUrl = (env) => (env.GITHUB_REPO ? `https://github.com/${env.GITHUB_REPO}/actions/workflows/${WORKFLOW}` : null);

// Records that a run was asked for; the job's own report replaces "starting" with "running".
export async function startRun(env, mode, { by, now = Date.now(), fetchFn = fetch } = {}) {
  const state = await readState(env);
  const status = await readStatus(env);
  const phase = phaseOf(state, status, now);
  if (phase === "starting" || phase === "running") return { ok: false, error: "A run is already in progress." };
  if (mode === "publish" && status?.state !== "built") return { ok: false, error: "There is no built index waiting to be published." };
  const r = await dispatch(env, mode, { fetchFn });
  if (!r.ok) return r;
  state.run = { mode, at: new Date(now).toISOString(), by };
  await saveState(env, state);
  return { ok: true };
}

// Deleting the staged pair needs no GitHub run: the panel's bucket binding can do it.
export async function discard(env, { by, now = Date.now() } = {}) {
  const status = await readStatus(env);
  if (status?.state !== "built") return { ok: false, error: "There is no built index to discard." };
  let cursor;
  do {
    const page = await env.LOGS.list({ prefix: STAGING_PREFIX, cursor });
    if (page.objects.length) await env.LOGS.delete(page.objects.map((o) => o.key));
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  await env.LOGS.put(STATUS_KEY, JSON.stringify({ state: "discarded", at: new Date(now).toISOString(), by, new_bills: status.new_bills, new_acts: status.new_acts }));
  const state = await readState(env);
  delete state.run;
  await saveState(env, state);
  return { ok: true };
}

// "Ignore these": the listed documents stop being reported (the build found nothing to index, or they are not wanted).
export async function ignorePending(env, { now = Date.now() } = {}) {
  const state = await readState(env);
  const keys = Object.keys(state.check?.pending || {});
  state.seen = { ...(state.seen || {}) };
  for (const k of keys) state.seen[k] = new Date(now).toISOString();
  state.check = { ...state.check, pending: {} };
  await saveState(env, state);
  return keys.length;
}

// What snapshot() reads: two small reads, no listing fetch.
export async function refreshView(env) {
  const [state, status] = await Promise.all([readState(env), readStatus(env)]);
  return { state, status, actionsUrl: actionsUrl(env) };
}

export { CHECK_STALE_MS };
