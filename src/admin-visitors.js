// Panel > Visitors: the anonymous counts that src/events.js writes to Workers Analytics Engine (dataset citizenlk_events), read back
// through Cloudflare's SQL API, as LankaWatch's panel does. Needs the secret CF_API_TOKEN (Account Analytics: Read) and the var
// CF_ACCOUNT_ID. Cached 2 minutes in KV (Analytics Engine itself shows a new event after a minute or two). Never reads a person's words:
// the rows hold fixed words, a country, a site name and a one-page-view random number.
// Rows (events.js point): blob1 ev, blob2 site, blob3 page, blob4 country, blob5 a, blob6 b, blob7 pv. Counts use _sample_interval
// (Analytics Engine samples only at high volume).
import { DATASET } from "./events.js";

const CACHE_S = 120;
export const CACHE_KEY = (days) => `admin:visitors:${days}`;
const since = (days, back = 0) => `timestamp > NOW() - INTERVAL '${days + back}' DAY${back ? ` AND timestamp <= NOW() - INTERVAL '${back}' DAY` : ""}`;

export const queries = (days) => ({
  // By hour, folded into Sri Lanka days below (the panel shows Colombo time).
  daily: `SELECT toStartOfInterval(timestamp, INTERVAL '1' HOUR) AS hour, blob2 AS site, SUM(_sample_interval) AS n FROM ${DATASET} WHERE blob1 = 'view' AND ${since(days)} GROUP BY hour, site ORDER BY hour`,
  // Visits: pages opened fresh from outside the site (nav: new + out).
  dailyVisits: `SELECT toStartOfInterval(timestamp, INTERVAL '1' HOUR) AS hour, blob2 AS site, SUM(_sample_interval) AS n FROM ${DATASET} WHERE blob1 = 'nav' AND blob5 = 'new' AND blob6 = 'out' AND ${since(days)} GROUP BY hour, site ORDER BY hour`,
  events: `SELECT blob1 AS ev, blob2 AS site, blob5 AS a, blob6 AS b, SUM(_sample_interval) AS n FROM ${DATASET} WHERE ${since(days)} GROUP BY ev, site, a, b`,
  countries: `SELECT blob2 AS site, blob4 AS country, SUM(_sample_interval) AS n FROM ${DATASET} WHERE blob1 = 'view' AND ${since(days)} GROUP BY site, country ORDER BY n DESC LIMIT 40`,
  prev: `SELECT SUM(_sample_interval) AS n FROM ${DATASET} WHERE blob1 = 'view' AND ${since(days, days)}`,
});

export async function sql(env, query, fetchFn = fetch) {
  const res = await fetchFn(`https://api.cloudflare.com/client/v4/accounts/${env.CF_ACCOUNT_ID}/analytics_engine/sql`, {
    method: "POST", headers: { authorization: `Bearer ${env.CF_API_TOKEN}` }, body: query, signal: AbortSignal.timeout(15000),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`Analytics Engine HTTP ${res.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text).data || [];
}

const num = (x) => Number(x) || 0;
const add = (o, k, n) => { o[k] = (o[k] || 0) + n; return o; };
// '2026-11-10 20:00:00' (UTC, as the SQL API writes it) -> the Sri Lanka date it falls on (UTC+5:30).
const colomboDay = (t) => new Date(Date.parse(`${String(t).replace(" ", "T")}Z`) + 330 * 60e3).toISOString().slice(0, 10);
// [{hour, site, n}] -> [{day, total, sites:{site:n}}], oldest first.
function perDay(rows) {
  const days = {};
  for (const r of rows) {
    const d = (days[colomboDay(r.hour)] ||= { total: 0, sites: {} });
    d.total += num(r.n);
    add(d.sites, r.site, num(r.n));
  }
  return Object.entries(days).sort().map(([day, d]) => ({ day, ...d }));
}

// Query results -> what the page shows. Pure (tested in test/events.test.mjs).
export function summarize({ daily = [], dailyVisits = [], events = [], countries = [], prev = [] }, days) {
  const by = {};
  for (const r of events) (by[r.ev] ||= []).push({ site: r.site, a: r.a, b: r.b, n: num(r.n) });
  const sumBy = (ev, key = "a", keep = () => true) => (by[ev] || []).filter(keep).reduce((o, r) => add(o, r[key], r.n), {});
  const perSite = (ev, keep = () => true) => (by[ev] || []).filter(keep).reduce((o, r) => add(o, r.site, r.n), {});
  const navs = (by.nav || []).reduce((o, r) => add(o, r.a === "new" ? `new-${r.b}` : r.a, r.n), {});
  const skipped = sumBy("skip");
  const dropped = { ...sumBy("drop"), ...Object.fromEntries(Object.entries(skipped).map(([k, n]) => [`page:${k}`, n])) };
  const views = Object.values(perSite("view")).reduce((a, b) => a + b, 0);
  return {
    days,
    views,
    visits: navs["new-out"] || 0,
    prev: num(prev[0] && prev[0].n),
    bySite: Object.fromEntries(Object.keys({ ...perSite("view"), ...perSite("nav", (r) => r.a === "new" && r.b === "out") }).map((s) => [s, { views: perSite("view")[s] || 0, visits: perSite("nav", (r) => r.a === "new" && r.b === "out")[s] || 0 }])),
    daily: perDay(daily),
    dailyVisits: perDay(dailyVisits),
    nav: { reload: navs.reload || 0, back: navs.back || 0, inside: navs["new-in"] || 0 },
    // How a visit's link was tagged (src, fixed words), the sending site's host and the link's own words (from, tag).
    sources: sumBy("src"), mediums: sumBy("src", "b"),
    referrers: sumBy("from", "b", (r) => r.b !== ""), tags: sumBy("from", "a", (r) => r.a !== ""), campaigns: sumBy("tag", "b", (r) => r.b !== ""),
    // Taps from one citizenlk page to another service (the homepage's two buttons, mostly).
    go: sumBy("go"),
    scroll: sumBy("scroll"),
    countries: Object.entries(countries.reduce((o, r) => add(o, r.country, num(r.n)), {})).sort((a, b) => b[1] - a[1]).slice(0, 12),
    dropped, droppedTotal: Object.values(dropped).reduce((a, b) => a + b, 0),
  };
}

// { setup: true } when the token is missing, { error } when Cloudflare refuses, else the summary (+ at).
export async function visitors(env, days = 30, { fresh = false, fetchFn = fetch, now = Date.now() } = {}) {
  if (!env.CF_API_TOKEN || !env.CF_ACCOUNT_ID) return { setup: true };
  const key = CACHE_KEY(days);
  let hit = null;
  if (!fresh) { try { hit = await env.AUTH.get(key, "json"); } catch { /* no cache: ask */ } }
  if (hit) return hit;
  try {
    const parts = await Promise.all(Object.entries(queries(days)).map(async ([k, q]) => [k, await sql(env, q, fetchFn)]));
    const out = { at: new Date(now).toISOString(), ...summarize(Object.fromEntries(parts), days) };
    try { await env.AUTH.put(key, JSON.stringify(out), { expirationTtl: CACHE_S }); } catch { /* the numbers still show */ }
    return out;
  } catch (e) {
    return { error: String((e && e.message) || e) };
  }
}
