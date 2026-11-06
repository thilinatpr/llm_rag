// Daily counters for the public (anonymous) lane. Pure logic: the Durable Object (quota-do.js) gives it SQLite storage, the
// tests give it a Map. All items of one call are checked first and then counted together, so a request that fails on one limit
// (say the day's global cap) does not use up the visitor's own allowance.
//
// item = { k: "c|<cookie id>|2026-11-06", limit: 10, ttl: seconds the counter lives, tag: "cookie" | "ip" | "global" }

export class Counters {
  // store: { get(k, now) -> n (0 when missing or expired), set(k, n, exp), prune(now) }
  constructor(store) {
    this.store = store;
  }

  take(items, now = Date.now() / 1000) {
    this.store.prune(now);
    const counts = items.map((i) => this.store.get(i.k, now));
    const over = items.findIndex((i, x) => counts[x] >= i.limit);
    if (over >= 0) return { ok: false, failed: items[over].tag, left: items.map((i, x) => Math.max(0, i.limit - counts[x])) };
    items.forEach((i, x) => this.store.set(i.k, counts[x] + 1, now + i.ttl));
    return { ok: true, left: items.map((i, x) => i.limit - counts[x] - 1) };
  }

  peek(items, now = Date.now() / 1000) {
    return { left: items.map((i) => Math.max(0, i.limit - this.store.get(i.k, now))) };
  }

  // Give a count back (the container answered 5xx, so the visitor got nothing).
  refund(items, now = Date.now() / 1000) {
    for (const i of items) {
      const n = this.store.get(i.k, now);
      if (n > 0) this.store.set(i.k, n - 1, now + i.ttl);
    }
    return { ok: true };
  }
}

export function memoryStore() {
  const m = new Map();
  return {
    get: (k, now) => {
      const e = m.get(k);
      return e && e.exp > now ? e.n : 0;
    },
    set: (k, n, exp) => void m.set(k, { n, exp }),
    prune: (now) => {
      for (const [k, e] of m) if (e.exp <= now) m.delete(k);
    },
    m,
  };
}

// Sri Lanka has no DST (UTC+5:30), so "today" for the visitor ends at local midnight.
const SL_OFFSET_MS = 5.5 * 3600 * 1000;
export const slDay = (nowMs = Date.now()) => new Date(nowMs + SL_OFFSET_MS).toISOString().slice(0, 10);
export function secondsToSlMidnight(nowMs = Date.now()) {
  const local = nowMs + SL_OFFSET_MS;
  return Math.max(1, Math.ceil((Math.floor(local / 86_400_000) * 86_400_000 + 86_400_000 - local) / 1000));
}
