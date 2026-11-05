"""Retrieval evaluation: recall@k and MRR of the configured Retriever against evals/*.jsonl.

Item format (one JSON per line):
  id, type, level ("clause"|"bill"), question, answer, gold: [{"bill": "077/2026", "section": 12}, ...], ...
A clause-level item is a hit when a returned chunk is one of the gold (bill, section) pairs. Chunks whose text is
identical in several bills (GS vs Bill versions) carry `also_in`; those count too. A bill-level item is a hit when
any returned chunk belongs to a gold bill.
"""
import json
import time
from collections import defaultdict

KS = (1, 3, 5, 10)


def load_items(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def result_pairs(chunk):
    sec = chunk.get("section_no")
    pairs = {(chunk["bill_key"], sec)}
    pairs |= {(b, sec) for b in chunk.get("also_in", [])}
    return pairs


def first_hit_rank(item, results):
    """1-based rank of the first result matching the gold labels, else None."""
    if item["level"] == "bill":
        gold = {g["bill"] for g in item["gold"]}
        for r, c in enumerate(results, 1):
            if {b for b, _ in result_pairs(c)} & gold:
                return r
        return None
    gold = {(g["bill"], g["section"]) for g in item["gold"]}
    for r, c in enumerate(results, 1):
        if result_pairs(c) & gold:
            return r
    return None


def evaluate(retriever, items, k=10):
    rows, t0 = [], time.time()
    for it in items:
        res = retriever.search(it["question"], k=k)
        rows.append({"item": it, "rank": first_hit_rank(it, res), "top": [(c["bill_key"], c.get("section_no")) for c in res[:5]]})
    elapsed = time.time() - t0
    return summarize(rows), rows, elapsed


def summarize(rows):
    def agg(rs):
        n = len(rs)
        out = {"n": n, "mrr": sum(1 / r["rank"] for r in rs if r["rank"]) / n if n else 0.0}
        for k in KS:
            out[f"r@{k}"] = sum(1 for r in rs if r["rank"] and r["rank"] <= k) / n if n else 0.0
        return out
    groups = defaultdict(list)
    for r in rows:
        groups[r["item"]["type"]].append(r)
    return {"all": agg(rows), "by_type": {t: agg(rs) for t, rs in sorted(groups.items())}}


def format_report(summary, label=""):
    hdr = f"{'':14}{'n':>4}" + "".join(f"{'R@' + str(k):>8}" for k in KS) + f"{'MRR':>8}"
    lines = [f"== {label}" if label else "== retrieval eval", hdr]
    for name, s in [("ALL", summary["all"])] + list(summary["by_type"].items()):
        lines.append(f"{name:14}{s['n']:>4}" + "".join(f"{s[f'r@{k}']:>8.2f}" for k in KS) + f"{s['mrr']:>8.2f}")
    return "\n".join(lines)
