"""Per-call timing: one small record for every model / Workers AI call (and the local search work) inside a turn.

`web.App` wraps each turn in `tracing()`; the adapters in llm.py and the retriever call `record(...)`, and the pipeline
names the current step with `step(...)`. The records go into the chat log as `calls`:
  {"kind": "chat", "step": "answer_direct", "model": ..., "s": 41.2, "at": 12.3, "in": 5210, "out": 1840,
   "reasoning": 220, "stop": "stop", "retries": 0, "max_tokens": 4000, "effort": null}
  {"kind": "ai_run", "step": "retrieve0", "model": "@cf/baai/bge-reranker-base", "s": 0.9, "n": 30, "attempts": 1}
  {"kind": "bm25" | "dense_scan", "step": "retrieve0", "s": 0.05}
`s` = seconds the call took (retries and back-off included), `at` = seconds from the start of the turn to the call's start,
`retries` = HTTP retries the OpenAI SDK made silently, `out` includes reasoning tokens where the provider counts them so.
The active trace is a ContextVar, so turns running at the same time (web.App lets several overlap) each record only their
own calls. A worker thread started inside a turn must run its task in `contextvars.copy_context()` to keep recording.
Outside a trace every function here is a no-op.
"""
import contextvars
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

_active = contextvars.ContextVar("billrag_trace", default=None)

# USD per 1M tokens: cache-hit input, cache-miss input, output (reasoning tokens are part of the output count).
# DeepSeek, https://api-docs.deepseek.com/quick_start/pricing (read 2026-11-06). Those are the OFF-PEAK prices; peak hours
# (01:00-04:00 and 06:00-10:00 UTC, Mon-Fri, not Chinese public holidays) cost double (`PEAK_FACTOR`). Prices change: override or
# extend with BILLRAG_PRICES='{"model-id": {"hit": 0.003, "miss": 0.15, "out": 0.6, "peak_factor": 2}}'. A model without a
# price is logged with tokens only (no `usd`): Workers AI models are billed differently and are not priced here.
PRICES = {
    "deepseek-flash": {"hit": 0.003, "miss": 0.15, "out": 0.6, "peak_factor": 2.0},
    "deepseek-v4-flash": {"hit": 0.003, "miss": 0.15, "out": 0.6, "peak_factor": 2.0},   # retired name, billed as Flash
    "deepseek-v4-pro": {"hit": 0.022, "miss": 0.66, "out": 1.98, "peak_factor": 2.0},
}
PEAK_UTC = ((1, 4), (6, 10))  # [start, end) hours, Monday-Friday


def prices(model):
    extra = {}
    if os.environ.get("BILLRAG_PRICES"):
        try:
            extra = json.loads(os.environ["BILLRAG_PRICES"])
        except ValueError:
            extra = {}
    return extra.get(model) or PRICES.get(model)


def is_peak(when=None):
    """DeepSeek peak window by UTC clock. Chinese public holidays (off-peak all day) are not known here, so on those days
    the logged cost is an upper bound."""
    d = when or datetime.now(timezone.utc)
    return d.weekday() < 5 and any(a <= d.hour < b for a, b in PEAK_UTC)


def cost_usd(model, tokens_in, tokens_out, cache_hit=None, when=None):
    """-> USD for one call, or None when the model has no price. `cache_hit` of None = no cache information: all input is billed
    as a cache miss (the upper bound)."""
    pr = prices(model)
    if pr is None or not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
        return None
    hit = min(cache_hit or 0, tokens_in)
    usd = (hit * pr["hit"] + (tokens_in - hit) * pr["miss"] + tokens_out * pr["out"]) / 1e6
    return usd * (pr.get("peak_factor", 1.0) if is_peak(when) else 1.0)


class Trace:
    def __init__(self):
        self.t0, self.step, self.calls, self.lock = time.time(), "", [], threading.Lock()

    def add(self, kind, seconds, fields):
        rec = {"kind": kind, "step": self.step, "s": round(seconds, 2), "at": round(time.time() - self.t0 - seconds, 2)}
        rec.update({k: v for k, v in fields.items() if v is not None})
        if kind == "chat":
            usd = cost_usd(rec.get("model"), rec.get("in"), rec.get("out"), rec.get("cache_hit"))
            if usd is not None:
                rec["usd"] = round(usd, 7)
        with self.lock:
            self.calls.append(rec)


@contextmanager
def tracing():
    trace = Trace()
    token = _active.set(trace)
    try:
        yield trace
    finally:
        _active.reset(token)


def step(name):
    t = _active.get()
    if t is not None:
        t.step = name


def record(kind, seconds, **fields):
    t = _active.get()
    if t is not None:
        t.add(kind, seconds, fields)


def usage_fields(usage):
    """Token counts from an OpenAI-style or Anthropic-style usage object (attributes or dict); missing -> absent."""
    if usage is None:
        return {}
    get = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
    out = {"in": get(usage, "prompt_tokens") or get(usage, "input_tokens"),
           "out": get(usage, "completion_tokens") or get(usage, "output_tokens")}
    hit = get(usage, "prompt_cache_hit_tokens")   # DeepSeek; OpenAI-style: prompt_tokens_details.cached_tokens
    if hit is None:
        hit = get(get(usage, "prompt_tokens_details"), "cached_tokens") if get(usage, "prompt_tokens_details") is not None else None
    out["cache_hit"] = hit
    details = get(usage, "completion_tokens_details")
    if details is not None:
        out["reasoning"] = get(details, "reasoning_tokens")
    return {k: v for k, v in out.items() if isinstance(v, int)}


def cost_summary(calls):
    """One turn's model spend from its `calls`: {"usd", "in", "out", "cache_hit", "chat_calls", "priced_calls", "peak"}.
    `usd` counts only priced calls (priced_calls < chat_calls means part of the turn ran on an unpriced model, e.g. Workers AI)."""
    chat = [c for c in calls or [] if c.get("kind") == "chat"]
    if not chat:
        return None
    priced = [c for c in chat if "usd" in c]
    return {"usd": round(sum(c["usd"] for c in priced), 6), "in": sum(c.get("in") or 0 for c in chat),
            "out": sum(c.get("out") or 0 for c in chat), "cache_hit": sum(c.get("cache_hit") or 0 for c in chat),
            "chat_calls": len(chat), "priced_calls": len(priced), "peak": is_peak()}


def summary(records):
    """Per step: model calls, seconds, output tokens and output tokens/second, from many log records' `calls` lists.
    Returns rows (step, kind, model, n, median s, max s, median out, median out/s, total retries), slowest median first."""
    groups = {}
    for calls in records:
        for c in calls or []:
            groups.setdefault((c.get("step") or "-", c.get("kind"), c.get("model") or ""), []).append(c)
    med = lambda v: sorted(v)[len(v) // 2] if v else None
    rows = []
    for (st, kind, model), cs in groups.items():
        secs = [c.get("s") or 0 for c in cs]
        outs = [c["out"] for c in cs if isinstance(c.get("out"), int)]
        rates = [c["out"] / c["s"] for c in cs if isinstance(c.get("out"), int) and c.get("s")]
        rows.append((st, kind, model, len(cs), med(secs), max(secs), med(outs), round(med(rates), 1) if rates else None,
                     sum(c.get("retries") or 0 for c in cs) + sum((c.get("attempts") or 1) - 1 for c in cs)))
    return sorted(rows, key=lambda r: -(r[4] or 0))
