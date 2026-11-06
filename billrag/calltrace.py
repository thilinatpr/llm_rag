"""Per-call timing: one small record for every model / Workers AI call (and the local search work) inside a turn.

`web.App` wraps each turn in `tracing()`; the adapters in llm.py and the retriever call `record(...)`, and the pipeline
names the current step with `step(...)`. The records go into the chat log as `calls`:
  {"kind": "chat", "step": "answer_direct", "model": ..., "s": 41.2, "at": 12.3, "in": 5210, "out": 1840,
   "reasoning": 220, "stop": "stop", "retries": 0, "max_tokens": 4000, "effort": null}
  {"kind": "ai_run", "step": "retrieve0", "model": "@cf/baai/bge-reranker-base", "s": 0.9, "n": 30, "attempts": 1}
  {"kind": "bm25" | "dense_scan", "step": "retrieve0", "s": 0.05}
`s` = seconds the call took (retries and back-off included), `at` = seconds from the start of the turn to the call's start,
`retries` = HTTP retries the OpenAI SDK made silently, `out` includes reasoning tokens where the provider counts them so.
Not thread-local on purpose: `App` runs one turn at a time (its lock), and the embedder calls from worker threads.
Outside a trace every function here is a no-op.
"""
import threading
import time
from contextlib import contextmanager

_active = None


class Trace:
    def __init__(self):
        self.t0, self.step, self.calls, self.lock = time.time(), "", [], threading.Lock()

    def add(self, kind, seconds, fields):
        rec = {"kind": kind, "step": self.step, "s": round(seconds, 2), "at": round(time.time() - self.t0 - seconds, 2)}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with self.lock:
            self.calls.append(rec)


@contextmanager
def tracing():
    global _active
    prev, _active = _active, Trace()
    try:
        yield _active
    finally:
        _active = prev


def step(name):
    if _active is not None:
        _active.step = name


def record(kind, seconds, **fields):
    if _active is not None:
        _active.add(kind, seconds, fields)


def usage_fields(usage):
    """Token counts from an OpenAI-style or Anthropic-style usage object (attributes or dict); missing -> absent."""
    if usage is None:
        return {}
    get = (lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None))
    out = {"in": get(usage, "prompt_tokens") or get(usage, "input_tokens"),
           "out": get(usage, "completion_tokens") or get(usage, "output_tokens")}
    details = get(usage, "completion_tokens_details")
    if details is not None:
        out["reasoning"] = get(details, "reasoning_tokens")
    return {k: v for k, v in out.items() if isinstance(v, int)}


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
