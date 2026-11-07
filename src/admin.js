// The admin panel at /admin: the same idea as LankaWatch's control panel. Home is a To do list, most urgent first, each row with the
// button that deals with it; then one tile per source; each source has its own page. Server-rendered, no build step, works on a phone.
// Runs as its own Worker (workers/admin/worker.js): whoever can sign in there is an admin. It never calls a container, so opening it costs
// nothing and wakes nothing: numbers come from R2 rollups (admin-stats.js), KV, the quota Durable Object and the containers' stored state.
// env.AUTH is this Worker's own KV (sessions, audit). env.BILLS_KV is Bill Assistant's: only the admin:* keys below are touched there.
// Log records hold text anyone can type, so everything is escaped and the CSP allows only this response's own script.
import { lastDayProblems, listQuestions, loadDays, PROBLEMS, quantile, readLog, seconds, SERVICES, utcDay, validKey } from "./admin-stats.js";
import { visitors } from "./admin-visitors.js";
import { checkBalance } from "./balance.js";
import { publicUsedToday } from "./public.js";
import { CHECK_STALE_MS, detect, discard, ignorePending, phaseOf, refreshView, startRun } from "./refresh.js";
import { hex, json } from "./util.js";

export const PAUSE_KEY = "admin:public-paused";
export const BALANCE_KEY = "admin:balance";
export const SETTINGS_KEY = "admin:settings"; // written hourly by Bill Assistant (public.js reportSettings)
const SETTINGS_STALE_MS = 3 * 3600e3;
const AUDIT_KEY = "admin:audit";
const AUDIT_MAX = 200;
const DAY = 864e5;
const INDEX_OLD_DAYS = 14;
const AWAKE_WATCH_MS = 6 * 3600e3;
const CREDIT_STALE_MS = 3 * 3600e3;
const TABLE_DAYS = 14;
const R2_FILES = { sqlite: "bills/index.sqlite", vectors: "bills/vectors.npz" };
export const CONTAINERS = [
  { id: "bills", name: "Bill Assistant", binding: "BILLRAG", usdPerHour: 0.009 },
  { id: "procurement", name: "Procurement Rules", binding: "PROCUREMENT", usdPerHour: 0.002 },
];

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const url = encodeURIComponent;
export const ago = (ms) => {
  const m = Math.round(ms / 60000);
  if (m < 1) return "under a minute";
  if (m < 90) return `${m} min`;
  const h = Math.round(m / 60);
  return h < 48 ? `${h} h` : `${Math.round(h / 24)} days`;
};
const when = (t) => (t ? new Date(t).toLocaleString("en-GB", { timeZone: "Asia/Colombo", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");
const usd = (n) => (n >= 1 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`);
const mb = (n) => `${Math.round(n / 1e6)} MB`;
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

// ---------- what the panel knows ----------
const attempt = async (fn) => {
  try {
    return await fn();
  } catch (e) {
    return { failed: String((e && e.message) || e) };
  }
};

async function indexFiles(env) {
  const [sqlite, vectors] = await Promise.all(Object.values(R2_FILES).map((k) => env.LOGS.head(k)));
  const info = (o) => (o ? { size: o.size, uploaded: new Date(o.uploaded).getTime() } : null);
  return { sqlite: info(sqlite), vectors: info(vectors) };
}

// Everything home and the source pages need. A part that cannot be read becomes {failed}, shown as such rather than as "all good".
export async function snapshot(env, { now = Date.now(), containerState = null } = {}) {
  const stats = {};
  await Promise.all(Object.keys(SERVICES).map(async (id) => {
    stats[id] = await attempt(async () => {
      if (!env.LOGS) throw new Error("no LOGS binding");
      return { days: await loadDays(env, id, 2, { now }) };
    });
  }));
  const settings = await attempt(async () => JSON.parse((await env.BILLS_KV.get(SETTINGS_KEY)) || "null"));
  const limit = settings?.limits?.global ?? null;
  const [balance, paused, used, index, containers, refresh] = await Promise.all([
    attempt(async () => JSON.parse((await env.BILLS_KV.get(BALANCE_KEY)) || "null")),
    attempt(async () => JSON.parse((await env.BILLS_KV.get(PAUSE_KEY)) || "null")),
    attempt(() => (env.QUOTA && limit !== null ? publicUsedToday(env, limit, now) : null)),
    attempt(() => (env.LOGS ? indexFiles(env) : { failed: "no LOGS binding" })),
    Promise.all(CONTAINERS.map(async (c) => ({ ...c, state: await attempt(() => (containerState ? containerState(env, c.binding) : { failed: "not connected" })) }))),
    attempt(() => refreshView(env)),
  ]);
  const known = settings && !settings.failed;
  return { now, stats, balance, paused, used, index, containers, refresh, limit, settings, limits: known ? settings.limits : null,
    publicWanted: !!known && settings.publicWanted, publicWorks: !!known && settings.publicWorks };
}

// ---------- To do ----------
export const TIERS = ["people", "you", "watch"];
const TIER_NAMES = { people: "People affected", you: "Waiting for you", watch: "Watching" };
const TOP_UP = "https://platform.deepseek.com/top_up";
const questionsHref = (service, only) => `/admin/questions?s=${service}${only ? `&only=${only}` : ""}`;

// Pure: snapshot -> rows, most urgent first, oldest first within a tier. Each row says what to do; a row disappears when the cause does.
export function todoItems(s) {
  const out = [];
  const add = (x) => out.push({ level: "amber", actions: [], ...x });
  const credit = { label: "Check now", post: "/admin/check-credit" };
  const topUp = { label: "Top up", href: TOP_UP, primary: true };

  const cfg = s.settings;
  if (cfg?.failed) add({ tier: "watch", source: "containers", title: "Cannot read Bill Assistant's settings", detail: cfg.failed });
  else if (!cfg) add({ tier: "watch", source: "containers", title: "Bill Assistant has not reported its settings yet", detail: "Its hourly job does, at 17 minutes past. Until then the public question limit and its checks are unknown." });
  else if (s.now - Date.parse(cfg.at) > SETTINGS_STALE_MS) add({ tier: "watch", source: "containers", title: `Bill Assistant last reported its settings ${ago(s.now - Date.parse(cfg.at))} ago`, detail: "It should every hour: look at that Worker's cron." });
  if (cfg && !cfg.failed && !cfg.proxySecret) add({ tier: "people", level: "red", source: "containers", title: "PROXY_SECRET is not set", detail: "Every question fails until it is: npx wrangler secret put PROXY_SECRET" });

  for (const [id, svc] of Object.entries(SERVICES)) {
    const st = s.stats[id];
    if (st?.failed) { add({ tier: "people", level: "red", source: id, title: `Cannot read the ${svc.name} logs`, detail: st.failed }); continue; }
    const p = lastDayProblems(st.days, s.now);
    const oldest = (list) => list.map((x) => x.t).sort()[0];
    if (p.error) add({ tier: "people", level: "red", source: id, since: oldest(p.error), title: `${plural(p.error.length, "question")} failed on ${svc.name}, last 24 h`,
      actions: [{ label: "Read them", href: questionsHref(id, "error"), primary: true }] });
    if (p.warned) add({ tier: "you", source: id, since: oldest(p.warned), title: `${plural(p.warned.length, "answer")} with a warning on ${svc.name}, last 24 h`,
      detail: "For example a citation not found in the evidence.", actions: [{ label: "Read them", href: questionsHref(id, "warned"), primary: true }] });
    if (p.backup) add({ tier: "watch", source: id, since: oldest(p.backup), title: `${plural(p.backup.length, "question")} answered by the backup model on ${svc.name}, last 24 h`,
      detail: "DeepSeek failed for these; the backup scored lower in the eval.", actions: [{ label: "Read them", href: questionsHref(id, "backup") }] });
    if (st.days.some((d) => d.behind)) add({ tier: "watch", source: id, title: `${svc.name} logs are not all counted yet`, detail: "The numbers catch up on each view and every hour." });
    const bad = st.days[0].bad;
    if (bad) add({ tier: "watch", source: id, title: `${plural(bad, "log record")} could not be read today (${svc.name})` });
  }

  const b = s.balance;
  if (b?.failed) add({ tier: "watch", source: "money", title: "Cannot read the last credit check", detail: b.failed, actions: [credit] });
  else if (!b) add({ tier: "watch", source: "money", title: "The DeepSeek credit has not been checked yet", detail: "The hourly check records it here.", actions: [credit] });
  else if (b.state === "low") {
    const empty = b.balance <= 0;
    add({ tier: empty ? "people" : "you", level: empty ? "red" : "amber", source: "money", since: b.lowSince,
      title: empty ? "DeepSeek credit is used up: answers are on the backup model" : `DeepSeek credit is low: ${b.balance} ${b.currency}`,
      detail: empty ? "The backup scored lower in the eval." : `Below the alert level of ${b.limit}. Answers go to the backup model when it runs out.`, actions: [topUp, credit] });
  } else if (b.state === "check_failed")
    add({ tier: b.failures >= 3 ? "you" : "watch", source: "money", title: `The credit check failed ${plural(b.failures, "time")} in a row`, detail: b.error, actions: [credit] });
  if (b && !b.failed && s.now - Date.parse(b.at) > CREDIT_STALE_MS)
    add({ tier: "watch", source: "money", title: `The credit was last checked ${ago(s.now - Date.parse(b.at))} ago`, detail: "It should run every hour: look at the Worker's cron.", actions: [credit] });

  const pausedAt = !s.paused?.failed && s.paused?.at;
  if (s.publicWanted && !s.publicWorks)
    add({ tier: "people", level: "red", source: "public", title: "Public questions are on, but something they need is missing", detail: "PUBLIC_CHAT = on with a missing secret or binding means login only. See AGENTS.md \"Public lane\"." });
  if (pausedAt)
    add({ tier: "you", source: "public", since: pausedAt, title: "Public questions are paused", detail: "Visitors must sign in. Resume when ready.",
      actions: [{ label: "Resume", post: "/admin/public-resume", primary: true }] });
  if (s.publicWorks && !pausedAt && Number.isFinite(s.used)) {
    if (s.used >= s.limit) add({ tier: "people", level: "red", source: "public", title: `All ${s.limit} free questions are used today: visitors are being refused`, detail: "They come back at midnight in Sri Lanka." });
    else if (s.used >= s.limit * 0.8) add({ tier: "watch", source: "public", title: `${s.used} of ${s.limit} free questions used today` });
  }
  if (s.used?.failed) add({ tier: "watch", source: "public", title: "Cannot read the public question counter", detail: s.used.failed });

  for (const c of s.containers) {
    const st = c.state;
    if (st?.failed) add({ tier: "watch", source: "containers", title: `Cannot read the ${c.name} container`, detail: st.failed });
    else if (st.status === "stopped_with_code" && st.exitCode) add({ tier: "watch", source: "containers", since: new Date(st.lastChange).toISOString(), title: `${c.name} container stopped with exit code ${st.exitCode}`, detail: "The next question starts it again. If this repeats, read its logs." });
    else if ((st.status === "healthy" || st.status === "running") && s.now - st.lastChange > AWAKE_WATCH_MS)
      add({ tier: "watch", source: "containers", since: new Date(st.lastChange).toISOString(), title: `${c.name} container has been awake for ${ago(s.now - st.lastChange)}`, detail: "Fine if people are using it; it should sleep after 15 quiet minutes." });
  }

  if (s.index?.failed) add({ tier: "watch", source: "index", title: "Cannot read the index files", detail: s.index.failed });
  else if (!s.index?.sqlite || !s.index?.vectors) add({ tier: "people", level: "red", source: "index", title: "The index files are missing from R2", detail: "A container that starts now cannot answer. See AGENTS.md \"Retrieval config\"." });
  else if (s.now - s.index.sqlite.uploaded > INDEX_OLD_DAYS * DAY)
    add({ tier: "watch", source: "index", title: `The bill index was last published ${ago(s.now - s.index.sqlite.uploaded)} ago`, detail: "New bills are not in it until scripts/refresh_bills.py --publish is run." });

  refreshItems(s, add);

  const rank = (x) => TIERS.indexOf(x.tier);
  return out.sort((a, b2) => rank(a) - rank(b2) || String(a.since || "9").localeCompare(String(b2.since || "9")));
}

// New bills on documents.gov.lk (src/refresh.js): found by the daily check, built and published on GitHub Actions at the owner's button presses.
const listTitles = (pending) => {
  const items = Object.entries(pending);
  const shown = items.slice(0, 4).map(([k, v]) => `${k} ${v.title}`.slice(0, 90));
  return shown.join("; ") + (items.length > shown.length ? `; and ${items.length - shown.length} more` : "");
};
const countWords = (o) => { const n = Object.keys(o || {}).length; return n ? plural(n, "document") : "no new documents"; };

function refreshItems(s, add) {
  const r = s.refresh;
  if (!r) return;
  const src = { source: "index" };
  if (r.failed) return add({ tier: "watch", ...src, title: "Cannot read the index build state", detail: r.failed });
  const { state, status } = r;
  const phase = phaseOf(state, status, s.now);
  const run = r.actionsUrl ? { label: "Open the run", href: r.actionsUrl } : null;
  const check = state.check;
  const pending = check?.pending || {};
  const nPending = Object.keys(pending).length;
  if (phase === "unknown") add({ tier: "watch", ...src, title: "Cannot read the index build report", detail: status.failed });
  else if (phase === "starting" || phase === "running")
    add({ tier: "watch", ...src, since: state.run?.at || status.at, title: phase === "starting" ? "Index build requested; the job has not started yet" : `Index ${status.mode === "publish" ? "publish" : "build"} is running`,
      detail: "A build takes 20-40 minutes. It publishes nothing: you press Publish when it is done.", actions: run ? [run] : [] });
  else if (phase === "stuck")
    add({ tier: "you", level: "red", ...src, since: state.run?.at || status.at, title: "The index job has not reported for too long", detail: "It may have been cancelled or failed to start. Look at the run, then build again.", actions: [run, { label: "Build again", post: "/admin/refresh-build" }].filter(Boolean) });
  else if (phase === "built")
    add({ tier: "you", ...src, since: status.at, title: `A new index is built and checked: ${countWords({ ...status.new_bills, ...status.new_acts })}, ready to publish`,
      detail: `${listTitles({ ...Object.fromEntries(Object.entries(status.new_bills || {}).map(([k, t]) => [k, { title: t }])), ...Object.fromEntries(Object.entries(status.new_acts || {}).map(([k, t]) => [k, { title: t }])) })}. Checks passed: nothing lost, new documents found by title, retrieval ${status.mrr ?? "?"} (floor ${status.min_mrr ?? 0.8}). The live index changes only when you publish.`,
      actions: [{ label: "Publish", post: "/admin/refresh-publish", primary: true, confirm: "Replace the live bill index with the built one? The old pair is backed up in R2; a running container picks the new one up when it next starts." },
        { label: "Discard", post: "/admin/refresh-discard", confirm: "Throw the built index away?" }, ...(run ? [run] : [])] });
  else if (phase === "failed")
    add({ tier: "you", level: "red", ...src, since: status.at, title: "The last index build stopped; nothing was published", detail: squeezeText(status.error || (status.problems || []).join("; ")) || "See the run for the output.",
      actions: [{ label: "Build again", post: "/admin/refresh-build", primary: true }, ...(run ? [run] : [])] });
  // A publish (or a build that found nothing to index) changes what "pending" means: the stored list is from before it.
  const outdated = phase === "idle" && ["published", "nothing_new"].includes(status?.state) && Date.parse(status.at) > Date.parse(check?.at || 0);
  if (outdated)
    add({ tier: "watch", ...src, since: status.at, title: status.state === "published" ? "A new index was published: check that nothing is still missing" : "The build found nothing to index: check again",
      detail: "The list of missing documents is from before it.", actions: [{ label: "Check now", post: "/admin/refresh-check", primary: true }] });
  if (phase === "idle" && nPending && !outdated)
    add({ tier: "you", ...src, since: check.at, title: `${plural(nPending, "new document")} on documents.gov.lk ${nPending === 1 ? "is" : "are"} not in the index`, detail: listTitles(pending),
      actions: [{ label: "Build the index", post: "/admin/refresh-build", primary: true, confirm: "Start a build on GitHub Actions? It uses a few cents of model calls and publishes nothing: you press Publish afterwards." },
        { label: "Ignore these", post: "/admin/refresh-ignore", confirm: "Stop reporting these documents?" }] });
  if (check?.mailError) add({ tier: "watch", ...src, since: check.at, title: "The new-bills email could not be sent", detail: `${check.mailError} It is tried again at the next check.` });
  if (check?.error) add({ tier: "watch", ...src, since: check.failedAt, title: "Cannot check documents.gov.lk for new bills", detail: `${check.error} If the site was redeployed, set DOCS_ACTION_ID (see AGENTS.md "Refreshing the index").`, actions: [{ label: "Check now", post: "/admin/refresh-check" }] });
  else if (check?.noManifest) add({ tier: "watch", ...src, title: "R2 has no list of the documents in the live index yet", detail: "Run once from the main checkout: python scripts/upload_r2.py --manifest-only", actions: [{ label: "Check now", post: "/admin/refresh-check" }] });
  else if (!check) add({ tier: "watch", ...src, title: "New bills have not been checked for yet", detail: "The daily check runs at 08:10 Sri Lanka time.", actions: [{ label: "Check now", post: "/admin/refresh-check" }] });
  else if (s.now - Date.parse(check.at) > CHECK_STALE_MS) add({ tier: "watch", ...src, since: check.at, title: `New bills were last checked ${ago(s.now - Date.parse(check.at))} ago`, detail: "It should run daily: look at this Worker's cron.", actions: [{ label: "Check now", post: "/admin/refresh-check" }] });
}
const squeezeText = (t) => String(t ?? "").replace(/\s+/g, " ").trim().slice(0, 300);

// ---------- page parts ----------
const CSS = `
:root { color-scheme: light dark; --lime:#F4F2EE; --ink:#1C1511; --muted:#6E6157; --line:#DAD3CA; --box:#FFFFFF; --sindoor:#A3241B; --ochre:#E0A93A; --err:#B3261E; --ok:#2F6B3A;
  --tint:color-mix(in srgb, var(--ink) 6%, var(--lime));
  --f-display:"Abhaya Libre","Noto Serif Sinhala",Georgia,serif; --f-body:"Yaldevi","Noto Sans Sinhala","Segoe UI",system-ui,sans-serif; }
@media (prefers-color-scheme: dark) { :root { --lime:#15100D; --ink:#F1E6D6; --muted:#B3A391; --line:#3A2D24; --box:#211913; --sindoor:#E0675A; --ochre:#E8B550; --err:#FF8A80; --ok:#86C795; } }
* { box-sizing:border-box; } body { margin:0; background:var(--lime); color:var(--ink); font:400 1rem/1.5 var(--f-body); }
:focus-visible { outline:2px solid var(--ochre); outline-offset:3px; border-radius:4px; } a { color:inherit; }
.petals { height:14px; background:radial-gradient(circle at 50% 0, var(--sindoor) 0 7px, transparent 7.5px) 0 0 / 20px 14px repeat-x; }
.wrap { max-width:1120px; margin:0 auto; padding:12px max(16px, env(safe-area-inset-right, 0px)) 40px max(16px, env(safe-area-inset-left, 0px)); }
nav { display:flex; flex-wrap:wrap; gap:4px 6px; align-items:center; padding-bottom:10px; border-bottom:1px solid var(--line); }
nav .brand { font:800 1.5rem/1 var(--f-display); text-decoration:none; margin-right:12px; } nav .brand b { color:var(--sindoor); }
nav a.l { text-decoration:none; padding:5px 10px; border-radius:6px; font-weight:500; } nav a.l.on, nav a.l:hover { background:var(--tint); }
nav .me { margin-left:auto; color:var(--muted); font-size:.85rem; overflow-wrap:anywhere; }
h1 { font:800 2rem/1.15 var(--f-display); margin:0; } h2 { font:500 1.05rem/1.3 var(--f-body); margin:26px 0 8px; }
.top { display:flex; flex-wrap:wrap; gap:2px 12px; align-items:baseline; margin:18px 0 12px; }
.dim { color:var(--muted); } .small { font-size:.88rem; } .red { color:var(--err); font-weight:500; } .good { color:var(--ok); font-weight:500; }
.dot { display:inline-block; flex:none; width:10px; height:10px; border-radius:50%; background:var(--ok); } .dot.amber { background:var(--ochre); } .dot.red { background:var(--err); }
.msg { padding:9px 14px; background:var(--tint); border-radius:6px; margin:0 0 12px; }
.todo-list { background:var(--box); border:1px solid var(--line); border-radius:6px; padding:0 16px; margin-bottom:18px; }
.todo { display:grid; grid-template-columns:136px minmax(0,1fr) 120px auto; gap:4px 14px; align-items:center; padding:11px 0; border-top:1px solid var(--line); }
.todo:first-child { border-top:0; }
.todo .tier { display:flex; gap:7px; align-items:center; font-size:.76rem; font-weight:500; letter-spacing:.04em; text-transform:uppercase; color:var(--muted); }
.todo.people .tier { color:var(--err); } .todo .what b { font-weight:500; overflow-wrap:anywhere; } .todo .when { color:var(--muted); font-size:.88rem; }
.todo .acts { display:flex; flex-wrap:wrap; gap:6px; justify-content:flex-end; }
@media (max-width:760px) { .todo { grid-template-columns:minmax(0,1fr) auto; } .todo .tier, .todo .what { grid-column:1 / -1; } }
.btn { display:inline-block; font:inherit; font-weight:500; white-space:nowrap; text-decoration:none; padding:6px 13px; border:0; border-radius:6px; background:var(--tint); color:var(--ink); cursor:pointer; }
.btn:hover { background:color-mix(in srgb, var(--ink) 12%, var(--lime)); } .btn.primary { background:var(--ink); color:var(--lime); } .btn.primary:hover { background:var(--sindoor); color:#fff; }
button[disabled] { opacity:.6; cursor:progress; } form.inline { display:inline; }
.groups { display:grid; gap:18px; grid-template-columns:minmax(0,1fr); } @media (min-width:760px) { .groups { grid-template-columns:1fr 2fr; } }
.grp h3 { margin:0 0 8px; font-size:.76rem; font-weight:500; letter-spacing:.05em; text-transform:uppercase; color:var(--muted); }
.tiles { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); }
.tile { display:block; text-decoration:none; background:var(--box); border:1px solid var(--line); border-radius:6px; padding:11px 13px; } .tile:hover { border-color:var(--ochre); }
.tile.amber { border-color:var(--ochre); } .tile.red { border-color:var(--err); } .tn { display:flex; gap:8px; align-items:center; font-weight:500; } .tf { color:var(--muted); font-size:.9rem; margin-top:2px; }
.tabs { display:flex; gap:2px; overflow-x:auto; border-bottom:1px solid var(--line); margin:12px 0 4px; } .tabs a { display:inline-flex; gap:7px; align-items:center; white-space:nowrap; text-decoration:none; padding:8px 12px; border-bottom:2px solid transparent; margin-bottom:-1px; } .tabs a.on { border-bottom-color:var(--sindoor); font-weight:500; }
.scroll { overflow-x:auto; } table { border-collapse:collapse; width:100%; font-size:.93rem; } th, td { text-align:left; padding:6px 12px 6px 0; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; } th { font-size:.8rem; font-weight:500; color:var(--muted); }
td.wrapped { white-space:normal; overflow-wrap:anywhere; min-width:220px; } td.n, th.n { text-align:right; }
dl { display:grid; grid-template-columns:max-content minmax(0,1fr); gap:4px 16px; margin:0; } dt { color:var(--muted); } dd { margin:0; overflow-wrap:anywhere; }
.text { white-space:pre-wrap; overflow-wrap:anywhere; background:var(--box); border:1px solid var(--line); border-radius:6px; padding:10px 13px; margin:0; font:inherit; }
pre.raw { white-space:pre-wrap; overflow-wrap:anywhere; font-size:.8rem; margin:8px 0 0; } .bar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:12px 0; }
`;
const FONTS = "https://fonts.googleapis.com/css2?family=Abhaya+Libre:wght@800&family=Yaldevi:wght@400;500&display=swap";

// Buttons ask first and show they are working.
const SCRIPT = `(function(){
document.addEventListener('submit',function(e){var c=e.target.getAttribute('data-confirm');if(c&&!confirm(c)){e.preventDefault();return}var b=e.submitter;if(b){b.disabled=true;b.textContent='Working…'}});
window.addEventListener('pageshow',function(e){if(e.persisted)location.reload()});
if(location.search.indexOf('done=')>=0){var u=new URL(location.href);u.searchParams.delete('done');history.replaceState(null,'',u)}
})()`;

function respond(body, { status = 200, nonce }) {
  return new Response(body, { status, headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,
  } });
}
const NAV = [["/admin", "Home", "home"], ["/admin/source/bills", "Questions", "questions"], ["/admin/source/visitors", "Visitors", "visitors"], ["/admin/source/money", "Running", "running"], ["/admin/audit", "Audit", "audit"]];
function page(title, body, c, { active = "", status = 200 } = {}) {
  const nonce = hex(crypto.getRandomValues(new Uint8Array(16)));
  return respond(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>${esc(title)} - citizenlk admin</title><link rel="stylesheet" href="${FONTS}"><style nonce="${nonce}">${CSS}</style></head><body>
<div class="petals" aria-hidden="true"></div><div class="wrap">
<nav><a class="brand" href="/admin">citizen<b>lk</b></a>${NAV.map(([h, n, id]) => `<a class="l${active === id ? " on" : ""}" href="${h}">${n}</a>`).join("")}<span class="me">${esc(c.user)}</span></nav>
${doneLine(c.url)}<main>${body}</main>
<p class="dim small">Times are Sri Lanka time; daily rows are UTC days, as the log folders are. ${c.env.CF_VERSION_METADATA ? `Worker version ${esc(c.env.CF_VERSION_METADATA.id.slice(0, 8))}.` : ""}</p>
</div><script nonce="${nonce}">${SCRIPT}</script></body></html>`, { status, nonce });
}
const doneLine = (u) => (u.searchParams.get("done") ? `<div class="msg">${esc(u.searchParams.get("done"))}</div>` : "");
const dot = (level) => `<i class="dot ${level || ""}" aria-label="${{ red: "Problem", amber: "Needs attention" }[level] || "All good"}"></i>`;
const button = (a, cls = "") => (a.href
  ? `<a class="btn ${a.primary ? "primary" : ""} ${cls}" href="${esc(a.href)}">${esc(a.label)}</a>`
  : `<form class="inline" method="post" action="${esc(a.post)}"${a.confirm ? ` data-confirm="${esc(a.confirm)}"` : ""}><button class="btn ${a.primary ? "primary" : ""} ${cls}">${esc(a.label)}</button></form>`);

function todoRow(x, now) {
  return `<div class="todo ${x.tier}"><span class="tier">${dot(x.level)}${TIER_NAMES[x.tier]}</span>
<div class="what"><b>${esc(x.title)}</b>${x.detail ? `<div class="dim small">${esc(x.detail)}</div>` : ""}</div>
<span class="when">${x.since ? `since ${ago(now - Date.parse(x.since))} ago` : ""}</span><div class="acts">${x.actions.map((a) => button(a)).join("")}</div></div>`;
}
const todoHtml = (items, now) => (items.length ? `<section class="todo-list">${items.map((x) => todoRow(x, now)).join("")}</section>` : "");

const levelOf = (items, source) => {
  const mine = items.filter((x) => x.source === source);
  return mine.some((x) => x.level === "red") ? "red" : mine.length ? "amber" : "";
};
const failedLine = (x) => `<p class="red">Could not read this: ${esc(x.failed)}</p>`;
const containerWord = (c, now) => {
  const st = c.state;
  if (st?.failed) return "unknown";
  if (st.status === "healthy" || st.status === "running") return `awake for ${ago(now - st.lastChange)}`;
  if (st.status === "stopping") return "going to sleep";
  return "asleep";
};

// ---------- sources ----------
const serviceBrief = (id) => (s) => {
  const st = s.stats[id];
  if (st.failed) return "Cannot read the logs";
  const d = st.days[0], med = quantile(d.secs, 0.5);
  return `Today ${plural(d.asks, "question")}${d.error ? ` · ${d.error} failed` : ""}${med !== null ? ` · median ${Math.round(med)} s` : ""}`;
};

function serviceTable(service, days) {
  const cell = (d, k) => (d[k] ? `<a href="${questionsHref(service, k)}&day=${d.day}">${d[k]}</a>` : `<span class="dim">0</span>`);
  return `<div class="scroll"><table><tr><th>Day (UTC)</th><th class="n">Questions</th>${Object.values(PROBLEMS).map((n) => `<th class="n">${esc(n)}</th>`).join("")}<th class="n">Median s</th><th class="n">p90 s</th><th class="n">Peak MB</th><th class="n">Model cost</th></tr>
${days.map((d) => `<tr><td><a href="${questionsHref(service)}&day=${d.day}">${d.day}</a></td><td class="n">${d.asks}${d.behind ? "+" : ""}</td>${Object.keys(PROBLEMS).map((k) => `<td class="n">${cell(d, k)}</td>`).join("")}
<td class="n">${quantile(d.secs, 0.5) ?? "—"}</td><td class="n">${quantile(d.secs, 0.9) ?? "—"}</td><td class="n">${d.peakRss ? Math.round(d.peakRss) : "—"}</td><td class="n">${d.priced ? usd(d.usd) : "—"}</td></tr>`).join("")}</table></div>`;
}

const serviceSource = (id) => ({
  title: SERVICES[id].name,
  brief: serviceBrief(id),
  body: async ({ env, s }) => {
    const days = await loadDays(env, id, TABLE_DAYS, { now: s.now, budget: { left: 300 } });
    const lastBuild = days.find((d) => d.build);
    return `<div class="bar">${button({ label: "Read today's questions", href: questionsHref(id), primary: true })}</div>
${serviceTable(id, days)}
${id === "bills" ? `<p class="dim small">Code answering: ${lastBuild ? `build <b>${esc(lastBuild.build)}</b>, last seen ${when(lastBuild.buildAt)}` : "no build seen yet"}. To compare with a commit: <code>python -c "from billrag.chatlog import build_id; print(build_id())"</code>. Public questions today: ${days[0].public}.</p>` : ""}
<p class="dim small">Model cost leaves out the backup model and any model without a price. Problem counts stop listing questions after 300 a day.</p>`;
  },
});

// ---------- visitors ----------
const VISIT_DAYS = [7, 30, 90];
const SITE_NAMES = { home: "citizenlk.com", bills: "Bill Assistant", procurement: "Procurement Rules" };
const pct = (n, of) => (of ? `${Math.round((n / of) * 100)}%` : "—");
const tableOf = (head, rows) => (rows.length ? `<div class="scroll"><table><tr>${head.map((h, i) => `<th${i ? ' class="n"' : ""}>${esc(h)}</th>`).join("")}</tr>${rows.map((r) => `<tr>${r.map((v, i) => `<td${i ? ' class="n"' : ""}>${esc(v)}</td>`).join("")}</tr>`).join("")}</table></div>` : `<p class="dim small">Nothing yet.</p>`);
const ranked = (o, n = 10) => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n);
const siteName = (k) => SITE_NAMES[k] || k;

export function visitorsBody(v, days) {
  const tabs = `<div class="bar">${VISIT_DAYS.map((d) => button({ label: `${d} days`, href: `?days=${d}`, primary: d === days })).join("")}</div>`;
  if (v.setup) return `${tabs}<p>Visitor counting is not connected to this panel yet.</p><p class="dim small">Create an API token with <b>Account &gt; Account Analytics: Read</b>, then <code>npx wrangler secret put CF_API_TOKEN -c workers/admin/wrangler.toml</code>. The pages count only once <code>EV_KEY</code> (homepage) or <code>OTP_PEPPER</code> (the services) exists and the Workers have been deployed with the <code>EVENTS</code> binding.</p>`;
  if (v.error) return `${tabs}<p class="red">Could not read the counts: ${esc(v.error)}</p>`;
  const sites = Object.entries(v.bySite).sort((a, b) => b[1].views - a[1].views);
  const trend = v.prev ? ` (${v.views >= v.prev ? "+" : ""}${Math.round(((v.views - v.prev) / v.prev) * 100)}% on the ${days} days before)` : "";
  const visitsBy = Object.fromEntries(v.dailyVisits.map((d) => [d.day, d]));
  const dayRows = [...v.daily].reverse().slice(0, 21).map((d) => [d.day, d.total, visitsBy[d.day]?.total || 0, Object.entries(d.sites).sort().map(([k, n]) => `${siteName(k)} ${n}`).join(" · ")]);
  const scrollBase = Object.values(v.scroll).reduce((a, b) => a + b, 0);
  return `${tabs}
<p><b>${v.views}</b> page views by a person, <b>${v.visits}</b> visits${esc(trend)}. A view is counted only after the page was visible for 2 seconds and was touched; a visit is a page opened from outside the site.</p>
<h2>By site</h2>${tableOf(["Site", "Views", "Visits"], sites.map(([k, x]) => [siteName(k), x.views, x.visits]))}
<h2>By day (Sri Lanka time)</h2>${tableOf(["Day", "Views", "Visits", "Where"], dayRows)}
<h2>Where visits came from</h2>${tableOf(["Sending site", "Visits"], ranked(v.referrers))}
${Object.keys(v.sources).length ? `<h2>Tagged links</h2>${tableOf(["Kind of link", "Visits"], ranked(v.sources))}${Object.keys(v.campaigns).length ? tableOf(["Campaign", "Visits"], ranked(v.campaigns)) : ""}` : ""}
<h2>What they did</h2>${tableOf(["Action", "Count"], [
    ...ranked(v.go).map(([k, n]) => [`Went on to ${k === "other" ? "another citizenlk address" : siteName(k === "admin" ? "admin" : k)}`, n]),
    ...["25", "50", "75", "100"].map((k) => [`Scrolled ${k}% down the page`, `${v.scroll[k] || 0} (${pct(v.scroll[k] || 0, scrollBase)})`]),
    ["Reloaded", v.nav.reload], ["Pressed back", v.nav.back], ["Moved within the site", v.nav.inside],
  ])}
<h2>Countries</h2>${tableOf(["Country", "Views"], v.countries)}
<p class="dim small">Not counted: ${v.droppedTotal} requests (${esc(ranked(v.dropped, 6).map(([k, n]) => `${k} ${n}`).join(", ") || "none")}) - bots, pages where nobody touched anything, bad tokens. Browsers with Global Privacy Control or Do Not Track on send nothing at all, so real traffic is a little higher. Nothing here identifies a person: no cookie, no IP address, no ID. Updated ${v.at ? when(Date.parse(v.at)) : "—"}; new events take a minute or two to show.</p>`;
}

const SOURCES_VISITORS = {
  title: "Visitors",
  brief: () => "Anonymous page counts, no IDs",
  body: async ({ env, url, fetchFn, now }) => {
    const asked = Number(url?.searchParams.get("days"));
    const days = VISIT_DAYS.includes(asked) ? asked : 30;
    return visitorsBody(await visitors(env, days, { fetchFn, now }), days);
  },
};

const SOURCES = {
  visitors: SOURCES_VISITORS,
  bills: serviceSource("bills"),
  procurement: serviceSource("procurement"),
  money: {
    title: "Credit and spend",
    brief: (s) => {
      const b = s.balance;
      const today = Object.values(s.stats).reduce((n, st) => n + (st.days?.[0].usd || 0), 0);
      if (b?.failed || !b) return `Credit not checked yet · ${usd(today)} today`;
      return `${Number.isFinite(b.balance) ? `$${b.balance} left` : "Check failing"} · ${usd(today)} today`;
    },
    body: async ({ env, s }) => {
      const b = s.balance, rows = {};
      for (const id of Object.keys(SERVICES))
        rows[id] = await loadDays(env, id, TABLE_DAYS, { now: s.now, budget: { left: 150 } });
      const day = (i) => Object.values(rows).reduce((n, list) => n + list[i].usd, 0);
      const week = Array.from({ length: 7 }, (_, i) => day(i + 1)).reduce((a, x) => a + x, 0) / 7;
      const lasts = b && Number.isFinite(b.balance) && week > 0 ? Math.floor(b.balance / week) : null;
      const credit = b?.failed ? failedLine(b) : !b ? `<p class="dim">Not checked yet.</p>` : `<dl>
<dt>Credit</dt><dd>${Number.isFinite(b.balance) ? `<b>${b.balance} ${esc(b.currency)}</b>` : `unknown: ${esc(b.error)}`}</dd>
<dt>Alert below</dt><dd>${b.limit ?? esc(env.BALANCE_ALERT_USD)} (an email to the alert address, at most one a day)</dd>
<dt>Last checked</dt><dd>${when(Date.parse(b.at))} (${ago(s.now - Date.parse(b.at))} ago)</dd>
<dt>Lasts</dt><dd>${lasts === null ? "unknown: no priced spending in the last 7 days" : `about ${plural(lasts, "day")} at the last 7 days' average of ${usd(week)} a day`}</dd></dl>`;
      return `${credit}<div class="bar">${button({ label: "Check now", post: "/admin/check-credit" })}${button({ label: "Top up", href: TOP_UP })}</div>
<h2>Model cost by day</h2><div class="scroll"><table><tr><th>Day (UTC)</th>${Object.values(SERVICES).map((x) => `<th class="n">${esc(x.name)}</th>`).join("")}<th class="n">Total</th></tr>
${rows.bills.map((d, i) => `<tr><td>${d.day}</td>${Object.keys(SERVICES).map((id) => `<td class="n">${rows[id][i].priced ? usd(rows[id][i].usd) : "—"}</td>`).join("")}<td class="n">${usd(day(i))}</td></tr>`).join("")}</table></div>
<p class="dim small">From each answer's log, at the prices in billrag/calltrace.py; leaves out the backup model. Container and Workers AI costs are not here.</p>`;
    },
  },
  public: {
    title: "Public questions",
    brief: (s) => (s.paused?.at ? "Paused" : !s.settings || s.settings.failed ? "Settings not reported yet" : !s.publicWorks ? "Off: login only" : Number.isFinite(s.used) ? `${s.used} of ${s.limit} used today` : "Counter unavailable"),
    body: async ({ s }) => {
      const L = s.limits || {}, on = s.publicWorks && !s.paused?.at;
      return `<dl>
<dt>State</dt><dd>${s.paused?.at ? `<b>Paused</b> since ${when(Date.parse(s.paused.at))} by ${esc(s.paused.by)}` : on ? `<span class="good">On</span>` : "Off: login only"}</dd>
<dt>Used today</dt><dd>${Number.isFinite(s.used) ? `${s.used} of ${s.limit} (resets at midnight in Sri Lanka)` : "unknown"}</dd>
<dt>Per visitor</dt><dd>${L.cookie ?? "unknown"} a day</dd><dt>Per IP address</dt><dd>${L.ip ?? "unknown"} a day (a flood backstop: mobile users share addresses)</dd><dt>New visitors per IP</dt><dd>${L.issue ?? "unknown"} a day</dd></dl>
<div class="bar">${s.paused?.at ? button({ label: "Resume public questions", post: "/admin/public-resume", primary: true })
  : on ? button({ label: "Pause public questions", post: "/admin/public-pause", confirm: "Pause public questions? Visitors will have to sign in until you resume. Signed-in people are not affected." }) : ""}</div>
<p class="dim small">Pausing takes up to a minute to reach every location. Limits are set in Bill Assistant's wrangler.toml and reach this page within the hour.</p>`;
    },
  },
  containers: {
    title: "Containers",
    brief: (s) => s.containers.map((c) => `${c.name}: ${containerWord(c, s.now)}`).join(" · "),
    body: async ({ s }) => `<div class="scroll"><table><tr><th>Service</th><th>State</th><th>Since</th><th>While awake</th></tr>
${s.containers.map((c) => `<tr><td>${esc(c.name)}</td><td>${esc(containerWord(c, s.now))}${c.state?.exitCode ? ` <span class="red">exit code ${esc(c.state.exitCode)}</span>` : ""}${c.state?.failed ? ` <span class="dim">${esc(c.state.failed)}</span>` : ""}</td><td>${when(c.state?.lastChange)}</td><td>about $${c.usdPerHour}/h</td></tr>`).join("")}</table></div>
<p class="dim small">Reading this does not wake them; they sleep after 15 quiet minutes. For memory and CPU while awake, see the Peak MB column on Bill Assistant, or the Cloudflare dashboard.</p>`,
  },
  index: {
    title: "Bill index",
    brief: (s) => (s.index?.sqlite ? `Published ${ago(s.now - s.index.sqlite.uploaded)} ago · ${mb(s.index.sqlite.size)}` : "Not found"),
    body: async ({ s }) => {
      if (s.index?.failed) return failedLine(s.index);
      const row = (name, f) => `<tr><td>${name}</td><td>${f ? when(f.uploaded) : `<span class="red">missing</span>`}</td><td class="n">${f ? mb(f.size) : ""}</td></tr>`;
      return `<div class="scroll"><table><tr><th>File in R2</th><th>Published</th><th class="n">Size</th></tr>${row("bills/index.sqlite", s.index.sqlite)}${row("bills/vectors.npz", s.index.vectors)}</table></div>
${refreshSection(s)}`;
    },
  },
};
function refreshSection(s) {
  const r = s.refresh;
  if (!r || r.failed) return `<p class="dim">${esc(r?.failed || "Build state not read.")}</p>`;
  const { state, status } = r, check = state.check, phase = phaseOf(state, status, s.now), pending = check?.pending || {};
  const last = status?.state ? `${esc(status.state)} ${when(Date.parse(status.at))}${status.by ? ` by ${esc(status.by)}` : ""}` : "no build yet";
  return `<h2>New bills</h2><dl>
<dt>Last check</dt><dd>${check?.at ? `${when(Date.parse(check.at))} (${ago(s.now - Date.parse(check.at))} ago): ${check.listed ?? "?"} documents listed` : "never"}${check?.error ? ` <span class="red">${esc(check.error)}</span>` : ""}</dd>
<dt>Not in the index</dt><dd>${Object.keys(pending).length ? esc(listTitles(pending)) : "none"}</dd>
<dt>Last build</dt><dd>${last}${status?.mrr ? ` · retrieval ${esc(status.mrr)}` : ""}${status?.error ? ` · <span class="red">${esc(squeezeText(status.error))}</span>` : ""}</dd>
<dt>Now</dt><dd>${esc(phase)}</dd></dl>
<div class="bar">${button({ label: "Check now", post: "/admin/refresh-check" })}${phase === "idle" || phase === "failed" ? button({ label: "Build the index", post: "/admin/refresh-build", primary: Object.keys(pending).length > 0, confirm: "Start a build on GitHub Actions? It publishes nothing." }) : ""}${r.actionsUrl ? button({ label: "Open runs on GitHub", href: r.actionsUrl }) : ""}</div>
<p class="dim small">The daily check compares documents.gov.lk with the live index. A build runs <code>scripts/refresh_bills.py</code> on GitHub Actions and publishes nothing; Publish then swaps the checked pair in R2 (see AGENTS.md "Refreshing the index"). A running container picks up a new index the next time it starts.</p>`;
}
const GROUPS = [
  { id: "questions", name: "Questions", sources: ["bills", "procurement"] },
  { id: "visitors", name: "Visitors", sources: ["visitors"] },
  { id: "running", name: "Running", sources: ["money", "public", "containers", "index"] },
];

// ---------- pages ----------
async function homePage(c) {
  const s = await snapshot(c.env, { now: c.now, containerState: c.containerState });
  const items = todoItems(s);
  const tile = (id) => {
    const src = SOURCES[id], lv = levelOf(items, id);
    return `<a class="tile ${lv}" href="/admin/source/${id}"><div class="tn">${dot(lv)}${esc(src.title)}</div><div class="tf">${esc(src.brief(s))}</div></a>`;
  };
  return page("Home", `<div class="top"><h1>${items.length ? `To do · ${items.length}` : "Nothing to do"}</h1><span class="dim small">updated ${when(s.now)}</span></div>
${todoHtml(items, s.now)}
<div class="groups">${GROUPS.map((g) => `<section class="grp"><h3>${esc(g.name)}</h3><div class="tiles">${g.sources.map(tile).join("")}</div></section>`).join("")}</div>`, c, { active: "home" });
}

async function sourcePage(c, id) {
  const src = SOURCES[id];
  if (!src) return notFound(c);
  const s = await snapshot(c.env, { now: c.now, containerState: c.containerState });
  const items = todoItems(s), group = GROUPS.find((g) => g.sources.includes(id));
  const tabs = group.sources.map((x) => `<a${x === id ? ' class="on"' : ""} href="/admin/source/${x}">${dot(levelOf(items, x))}${esc(SOURCES[x].title)}</a>`).join("");
  return page(src.title, `<div class="tabs">${tabs}</div><div class="top"><h1>${esc(src.title)}</h1></div>
${todoHtml(items.filter((x) => x.source === id), s.now)}${await src.body({ env: c.env, s, url: c.url, fetchFn: c.fetchFn, now: c.now })}`, c, { active: group.id });
}

const clip = (text, n) => {
  const t = String(text ?? "").replace(/\s+/g, " ").trim();
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
};

async function questionsPage(c) {
  const q = c.url.searchParams;
  const service = SERVICES[q.get("s")] ? q.get("s") : "bills";
  const day = /^\d{4}-\d{2}-\d{2}$/.test(q.get("day") || "") ? q.get("day") : utcDay(c.now);
  const only = PROBLEMS[q.get("only")] ? q.get("only") : null;
  const { rows, next } = await listQuestions(c.env, service, day, { only, before: validKey(q.get("before")) ? q.get("before") : null, now: c.now });
  const link = (extra) => `/admin/questions?s=${service}&day=${day}${extra}`;
  const nextDay = new Date(Date.parse(day) + DAY).toISOString().slice(0, 10), prevDay = new Date(Date.parse(day) - DAY).toISOString().slice(0, 10);
  const dayLink = (d, label) => `<a class="btn" href="/admin/questions?s=${service}&day=${d}${only ? `&only=${only}` : ""}">${label}</a>`;
  const table = rows.map(({ key, rec }) => (rec ? `<tr><td>${when(Date.parse(rec.ts))}</td><td class="wrapped"><a href="/admin/q/${key.split("/").map(url).join("/")}">${esc(clip(rec.question, 120)) || "(no text)"}</a><div class="dim small">${esc(clip(rec.error || rec.answer, 140))}</div></td><td class="n">${seconds(rec) !== null ? Math.round(seconds(rec)) : "—"}</td><td>${esc(rec.lane === "public" ? "visitor" : "")}</td></tr>`
    : `<tr><td colspan="4" class="dim">Could not read ${esc(key)}</td></tr>`)).join("");
  return page("Questions", `<div class="tabs">${Object.entries(SERVICES).map(([id, x]) => `<a${id === service ? ' class="on"' : ""} href="/admin/questions?s=${id}&day=${day}">${esc(x.name)}</a>`).join("")}</div>
<div class="top"><h1>${day}</h1><span class="dim small">${only ? `only questions that ${esc(PROBLEMS[only])} · <a href="${link("")}">show all</a>` : "newest first"} · private: these are people's own words</span></div>
<div class="bar">${dayLink(prevDay, "← Earlier day")}${day < utcDay(c.now) ? dayLink(nextDay, "Later day →") : ""}</div>
${rows.length ? `<div class="scroll"><table><tr><th>Time</th><th>Question and answer</th><th class="n">s</th><th></th></tr>${table}</table></div>` : `<p class="dim">No questions here.</p>`}
${next ? `<div class="bar"><a class="btn" href="${link(`${only ? `&only=${only}` : ""}&before=${url(next)}`)}">Older →</a></div>` : ""}`, c, { active: "questions" });
}

const listOf = (items) => `<ul>${items.map((x) => `<li>${esc(typeof x === "string" ? x : JSON.stringify(x))}</li>`).join("")}</ul>`;
async function questionPage(c, key) {
  const service = Object.keys(SERVICES).find((id) => key.startsWith(`${SERVICES[id].prefix}/`));
  const r = await readLog(c.env, key);
  if (!r) return notFound(c);
  const calls = (r.calls || []).filter((x) => x.kind === "chat" || x.kind === "ai_run");
  const facts = [["Time", when(Date.parse(r.ts))], ["Seconds", seconds(r)], ["Who", r.lane === "public" ? "Visitor" : r.lane || "Signed in"], ["Model", r.model], ["Language", r.lang], ["Path", r.path],
    ["Cost", r.cost?.priced_calls ? usd(r.cost.usd) : null], ["Build", r.build], ["Searched", (r.queries || []).join(" · ")], ["Evidence enough", r.enough === undefined ? null : r.enough ? "yes" : "no"]]
    .filter(([, v]) => v !== null && v !== undefined && v !== "");
  const evidence = r.evidence?.length ? `<h2>Evidence</h2><div class="scroll"><table><tr><th>Bill</th><th>Section</th><th class="n">Grade</th></tr>${r.evidence.map((e) => `<tr><td>${esc(e.bill)}</td><td>${esc(e.section)}</td><td class="n">${esc(e.score)}</td></tr>`).join("")}</table></div>`
    : r.sources?.length ? `<h2>Sources</h2>${listOf(r.sources)}` : "";
  return page("Question", `<div class="bar"><a class="btn" href="${questionsHref(service)}&day=${esc(key.split("/")[1])}">← ${esc(SERVICES[service].name)}, ${esc(key.split("/")[1])}</a></div>
<h2>Question</h2><p class="text">${esc(r.question)}</p>
${r.error ? `<h2>Error</h2><p class="text red">${esc(r.error)}</p>` : ""}
<h2>Answer</h2><p class="text">${esc(r.answer ?? "(none)")}</p>
${r.warnings?.length ? `<h2>Warnings</h2>${listOf(r.warnings)}` : ""}
<h2>Details</h2><dl>${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
${evidence}
${calls.length ? `<h2>Model calls</h2><div class="scroll"><table><tr><th>Step</th><th>Model</th><th class="n">s</th><th class="n">In</th><th class="n">Out</th></tr>${calls.map((x) => `<tr><td>${esc(x.step)}</td><td>${esc(x.model)}</td><td class="n">${esc(x.s)}</td><td class="n">${esc(x.in ?? "")}</td><td class="n">${esc(x.out ?? "")}</td></tr>`).join("")}</table></div>` : ""}
<details><summary class="dim small">The whole log record</summary><pre class="raw">${esc(JSON.stringify(r, null, 1))}</pre></details>`, c, { active: "questions" });
}

async function auditPage(c) {
  const log = JSON.parse((await c.env.AUTH.get(AUDIT_KEY)) || "[]");
  return page("Audit", `<div class="top"><h1>Audit</h1><span class="dim small">buttons pressed here, newest first</span></div>
${log.length ? `<div class="scroll"><table><tr><th>When</th><th>Who</th><th>What</th></tr>${log.map((x) => `<tr><td>${when(Date.parse(x.at))}</td><td>${esc(x.who)}</td><td class="wrapped">${esc(x.what)}</td></tr>`).join("")}</table></div>` : `<p class="dim">Nothing yet.</p>`}`, c, { active: "audit" });
}

const notFound = (c) => page("Not found", `<div class="top"><h1>Not found</h1></div><p><a href="/admin">Back to the panel</a></p>`, c, { status: 404 });

// ---------- actions ----------
async function audit(env, who, what, now) {
  const log = JSON.parse((await env.AUTH.get(AUDIT_KEY)) || "[]");
  log.unshift({ at: new Date(now).toISOString(), who, what });
  await env.AUTH.put(AUDIT_KEY, JSON.stringify(log.slice(0, AUDIT_MAX)));
}

const ACTIONS = {
  "check-credit": async (c) => {
    // Same KV as Bill Assistant's cron, so the two share the once-a-day mail limit and the last result.
    const r = await checkBalance({ ...c.env, AUTH: c.env.BILLS_KV }, c.balanceOptions);
    const text = { ok: `Credit is ${r.balance} ${r.currency}.`, low: `Credit is low: ${r.balance} ${r.currency}.`, check_failed: `The check failed: ${r.error}`, off: "The check is off: DEEPSEEK_API_KEY or ALERT_EMAIL is missing." }[r.state];
    return { text, log: `Checked the credit: ${text}` };
  },
  "public-pause": async (c) => {
    await c.env.BILLS_KV.put(PAUSE_KEY, JSON.stringify({ at: new Date(c.now).toISOString(), by: c.user }));
    return { text: "Public questions are paused. Visitors must sign in.", log: "Paused public questions" };
  },
  "public-resume": async (c) => {
    await c.env.BILLS_KV.delete(PAUSE_KEY);
    return { text: "Public questions are back on.", log: "Resumed public questions" };
  },
  "refresh-check": async (c) => {
    const k = await detect(c.env, { now: c.now, fetchFn: c.fetchFn, send: c.send });
    const n = Object.keys(k.pending || {}).length;
    const text = k.error ? `The check failed: ${k.error}` : k.noManifest ? "R2 has no list of indexed documents yet." : n ? `${plural(n, "document")} not in the index.` : "The index has every listed document.";
    return { text, log: `Checked documents.gov.lk: ${text}` };
  },
  "refresh-build": async (c) => {
    const r = await startRun(c.env, "build", { by: c.user, now: c.now, fetchFn: c.fetchFn });
    const text = r.ok ? "Build started on GitHub. It takes 20-40 minutes and publishes nothing." : r.error;
    return { text, log: r.ok ? "Started an index build" : `Index build not started: ${r.error}` };
  },
  "refresh-publish": async (c) => {
    const r = await startRun(c.env, "publish", { by: c.user, now: c.now, fetchFn: c.fetchFn });
    const text = r.ok ? "Publish started on GitHub. It takes a few minutes." : r.error;
    return { text, log: r.ok ? "Started publishing the built index" : `Publish not started: ${r.error}` };
  },
  "refresh-discard": async (c) => {
    const r = await discard(c.env, { by: c.user, now: c.now });
    const text = r.ok ? "The built index was discarded." : r.error;
    return { text, log: r.ok ? "Discarded the built index" : `Discard refused: ${r.error}` };
  },
  "refresh-ignore": async (c) => {
    const n = await ignorePending(c.env, { now: c.now });
    return { text: `${plural(n, "document")} will not be reported again.`, log: `Ignored ${plural(n, "new document")}` };
  },
};

async function action(c, request, name) {
  // SameSite=Lax already keeps cross-site posts from carrying the session; this is the second lock.
  if (request.headers.get("Origin") !== c.url.origin) return json({ error: "Bad origin." }, 403);
  if (!Object.hasOwn(ACTIONS, name)) return notFound(c);
  const { text, log } = await ACTIONS[name](c);
  await audit(c.env, c.user, log, c.now);
  // Back to the page the button was on (same site only).
  let back = "/admin";
  try {
    const ref = new URL(request.headers.get("Referer") || "");
    if (ref.origin === c.url.origin && (ref.pathname === "/admin" || ref.pathname.startsWith("/admin/"))) back = ref.pathname;
  } catch { /* no referer: go home */ }
  return new Response(null, { status: 303, headers: { Location: `${back}?done=${url(text)}`, "Cache-Control": "no-store" } });
}

// containerState(env, bindingName) -> the container's stored state, without starting it (worker.js gives the real one).
export function createAdmin({ containerState = null, now = () => Date.now(), balanceOptions = {}, fetchFn = fetch, send = undefined } = {}) {
  return async function admin(request, env, user) {
    const u = new URL(request.url);
    if (!user) {
      if (request.method === "GET") return Response.redirect(new URL("/login", u).toString(), 302);
      return json({ error: "Not signed in." }, 401);
    }
    const c = { env, user, url: u, now: now(), containerState, balanceOptions, fetchFn, send };
    const path = u.pathname.replace(/\/+$/, "");
    if (request.method === "POST" && path.startsWith("/admin/")) return action(c, request, path.slice(7));
    if (request.method !== "GET") return json({ error: "Not found." }, 404);
    if (path === "") return Response.redirect(new URL("/admin", u).toString(), 302);
    if (path === "/admin") return homePage(c);
    if (path.startsWith("/admin/source/")) return sourcePage(c, path.slice(14));
    if (path === "/admin/questions") return questionsPage(c);
    if (path === "/admin/audit") return auditPage(c);
    if (path.startsWith("/admin/q/")) {
      let key = "";
      try { key = decodeURIComponent(path.slice(9)); } catch { /* malformed: not found below */ }
      return questionPage(c, key);
    }
    return notFound(c);
  };
}
