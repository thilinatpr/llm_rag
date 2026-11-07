// Numbers for the admin panel, read from the chat logs in R2 without waking a container.
// A log is one R2 object per question, so counting a day by reading every object on every page view would not scale. Instead each
// day has a rollup, admin-rollups/<service>/<day>.json, which remembers the last key it counted and only reads what came after it.
// Page views and the hourly cron both move it forward, each within a budget of R2 calls. Rollups hold counts and keys, never words:
// the question text stays in the log objects, which only /admin/q/... opens.

export const SERVICES = {
  bills: { name: "Bill Assistant", prefix: "chatlogs" },
  procurement: { name: "Procurement Rules", prefix: "procurement-logs" },
};
export const SLOW_SECONDS = 60;
export const PROBLEMS = {
  error: "failed",
  nothing: "found nothing",
  insufficient: "evidence judged insufficient",
  warned: "had a warning",
  backup: "used the backup model",
  slow: `took over ${SLOW_SECONDS} s`,
};
const ROLLUPS = "admin-rollups";
const SETTLE_MS = 60_000; // a log is written just after its key's time: leave the last minute for the next read, or a late write is skipped for good
const MAX_PROBLEMS = 300;
const DAY_MS = 86_400_000;

export const utcDay = (ms, back = 0) => new Date(ms - back * DAY_MS).toISOString().slice(0, 10);
export const emptyDay = (day) => ({ v: 1, day, last: null, done: false, behind: false, records: 0, asks: 0, searches: 0, bad: 0, public: 0, usd: 0, priced: 0,
  error: 0, nothing: 0, insufficient: 0, warned: 0, backup: 0, slow: 0, secs: [], peakRss: 0, build: null, buildAt: null, problems: [] });

export const seconds = (r) => (Number.isFinite(r.seconds) ? r.seconds : Number.isFinite(r.ms) ? r.ms / 1000 : null);

// Which problems a question record shows. A greeting or a clarifying question searched nothing, so it did not "find nothing";
// records from before a field existed count as not having that problem.
export function problemsOf(r) {
  const out = [];
  if (r.error || r.status >= 500) out.push("error");
  else {
    const empty = r.evidence ? !r.evidence.length && r.queries?.length > 0 : Array.isArray(r.sources) && !r.sources.length && !!r.answer;
    if (empty) out.push("nothing");
    if (r.evidence?.length && r.enough === false) out.push("insufficient");
  }
  if (r.warnings?.length) out.push("warned");
  if (r.fallback) out.push("backup");
  if (seconds(r) > SLOW_SECONDS) out.push("slow");
  return out;
}

export function addRecord(s, key, r) {
  s.records++;
  if (r.type === "search") return void s.searches++;
  s.asks++;
  if (r.lane === "public") s.public++;
  const sec = seconds(r);
  if (sec !== null) s.secs.push(Math.round(sec * 10) / 10);
  if (r.cost?.priced_calls) { s.usd += r.cost.usd || 0; s.priced++; }
  if (r.usage?.peak_rss_mb > s.peakRss) s.peakRss = r.usage.peak_rss_mb;
  if (r.build) { s.build = r.build; s.buildAt = r.ts; }
  const why = problemsOf(r);
  for (const w of why) s[w]++;
  if (why.length && s.problems.length < MAX_PROBLEMS) s.problems.push({ k: key, t: r.ts, w: why });
}

const keyTime = (key, day) => {
  const m = /\/(\d{2})(\d{2})(\d{2})(\d{3})[^/]*$/.exec(key);
  return m ? Date.parse(`${day}T${m[1]}:${m[2]}:${m[3]}.${m[4]}Z`) : 0;
};
const readObject = async (env, key) => {
  const o = await env.LOGS.get(key);
  return o ? o.json() : null;
};

// The day's rollup, moved forward as far as `budget.left` R2 calls allow. `behind` = there is more to count.
export async function loadDay(env, service, day, { now = Date.now(), budget = { left: 150 } } = {}) {
  const key = `${ROLLUPS}/${service}/${day}.json`;
  const s = (await readObject(env, key).catch(() => null)) || emptyDay(day);
  if (s.done) return s;
  const prefix = `${SERVICES[service].prefix}/${day}/`;
  const before = JSON.stringify([s.last, s.behind, s.done]);
  let more = true;
  s.behind = false;
  while (more) {
    if (budget.left <= 1) { s.behind = true; break; }
    const listing = await env.LOGS.list({ prefix, limit: Math.min(100, budget.left - 1), ...(s.last ? { startAfter: s.last } : {}) });
    budget.left--;
    const fresh = listing.objects.map((o) => o.key).filter((k) => now - keyTime(k, day) >= SETTLE_MS);
    const records = await Promise.all(fresh.map((k) => readObject(env, k).catch(() => null)));
    budget.left -= fresh.length;
    fresh.forEach((k, i) => {
      if (records[i]) addRecord(s, k, records[i]);
      else s.bad++;
      s.last = k;
    });
    more = listing.truncated && fresh.length === listing.objects.length;
  }
  // Only a day that is over can be closed; yesterday stays open for the settle margin of its last minute.
  s.done = !s.behind && day < utcDay(now, 1);
  if (JSON.stringify([s.last, s.behind, s.done]) !== before) await env.LOGS.put(key, JSON.stringify(s), { httpMetadata: { contentType: "application/json" } });
  return s;
}

// The last `n` days, newest first, so the budget goes to the days being looked at.
export async function loadDays(env, service, n, { now = Date.now(), budget = { left: 150 } } = {}) {
  const out = [];
  for (let i = 0; i < n; i++) out.push(await loadDay(env, service, utcDay(now, i), { now, budget }));
  return out;
}

export function quantile(list, q) {
  if (!list.length) return null;
  const sorted = [...list].sort((a, b) => a - b);
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * q))];
}

// Problems of the last 24 h, from today's and yesterday's rollups: { error: [{k, t, w}], ... }
export function lastDayProblems(days, now = Date.now()) {
  const out = {};
  for (const d of days)
    for (const p of d.problems)
      if (now - Date.parse(p.t) <= DAY_MS) for (const w of p.w) (out[w] ||= []).push(p);
  return out;
}

// Hourly, so the numbers keep up (and older days get counted) even when nobody opens the panel.
export async function refreshStats(env, { now = Date.now(), days = 14 } = {}) {
  if (!env.LOGS) return;
  try {
    for (const service of Object.keys(SERVICES)) await loadDays(env, service, days, { now, budget: { left: 300 } });
  } catch (e) {
    console.error(JSON.stringify({ event: "admin_stats_failed", error: String((e && e.message) || e) }));
  }
}

const isDay = (d) => /^\d{4}-\d{2}-\d{2}$/.test(d || "");
export const validKey = (k) => new RegExp(`^(${Object.values(SERVICES).map((s) => s.prefix).join("|")})/\\d{4}-\\d{2}-\\d{2}/[\\w-]+\\.json$`).test(k || "");
export const readLog = async (env, key) => (validKey(key) ? readObject(env, key) : null);

async function dayKeys(env, service, day) {
  const keys = [];
  let cursor;
  do {
    const r = await env.LOGS.list({ prefix: `${SERVICES[service].prefix}/${day}/`, ...(cursor ? { cursor } : {}) });
    keys.push(...r.objects.map((o) => o.key));
    cursor = r.truncated ? r.cursor : null;
  } while (cursor);
  return keys;
}

// One page of a day's questions, newest first. `only` = a key of PROBLEMS, answered from the rollup.
export async function listQuestions(env, service, day, { only = null, before = null, limit = 25, now = Date.now() } = {}) {
  if (!SERVICES[service] || !isDay(day)) return { rows: [], next: null };
  let keys;
  if (PROBLEMS[only]) {
    const s = await loadDay(env, service, day, { now });
    keys = s.problems.filter((p) => p.w.includes(only)).map((p) => p.k);
  } else keys = await dayKeys(env, service, day);
  keys.reverse();
  if (before) keys = keys.filter((k) => k < before);
  const page = keys.slice(0, limit);
  const rows = await Promise.all(page.map(async (key) => ({ key, rec: await readObject(env, key).catch(() => null) })));
  return { rows, next: keys.length > limit ? page[page.length - 1] : null };
}
