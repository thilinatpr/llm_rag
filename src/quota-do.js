// Durable Object holding the public lane's daily counters (SQLite). One instance ("main") serves everything: a few thousand
// requests a day is far below what a single Durable Object handles, and one writer is what makes the limits exact.
// This file imports `cloudflare:workers`, so only wrangler loads it (the Node tests use quota.js directly).
import { DurableObject } from "cloudflare:workers";
import { Counters } from "./quota.js";

export class QuotaCounter extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    const sql = ctx.storage.sql;
    sql.exec("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, n INTEGER NOT NULL, exp INTEGER NOT NULL)");
    this.counters = new Counters({
      get: (k, now) => {
        const row = [...sql.exec("SELECT n FROM c WHERE k = ? AND exp > ?", k, now)][0];
        return row ? row.n : 0;
      },
      set: (k, n, exp) => void sql.exec("INSERT OR REPLACE INTO c (k, n, exp) VALUES (?, ?, ?)", k, n, Math.ceil(exp)),
      prune: (now) => void sql.exec("DELETE FROM c WHERE exp <= ?", now),
    });
  }

  take(items) {
    return this.counters.take(items);
  }

  peek(items) {
    return this.counters.peek(items);
  }

  refund(items) {
    return this.counters.refund(items);
  }
}
