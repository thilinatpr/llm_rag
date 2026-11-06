"""CPU and memory figures for the running server (stdlib only; Linux /proc and cgroup files, each optional).

- `snapshot()`: what GET /api/stats returns: process RSS/peak, process CPU seconds, the machine's memory (/proc/meminfo;
  a Cloudflare container is its own small VM, so this is the instance), the cgroup limit/usage when there is one, load
  average and uptime. Any file that is missing (macOS, no cgroup) just leaves its fields out.
- `Usage`: context manager measuring one request: wall seconds, process CPU seconds used meanwhile, RSS before/after and
  the peak so far. CPU is process-wide (all threads); `App.ask` holds the app lock for the whole turn, so concurrent
  turns cannot inflate it, but a `search` running at the same time as an `ask` can.
- `usage_summary(records)`: median/p90/max of those fields over chat log records (scripts/read_chatlogs.py).
"""
import os
import resource
import sys
import time
from pathlib import Path

STARTED = time.time()
_MB = 1024 * 1024


def _read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _kb_fields(path, names):
    """{name: MB} for the `Name:   123 kB` lines of a /proc file."""
    out, text = {}, _read(path)
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        if key in names and rest.split():
            out[names[key]] = round(int(rest.split()[0]) / 1024, 1)
    return out


def _bytes_mb(path):
    v = _read(path)
    if v is None or v == "max" or not v.isdigit():
        return None
    n = int(v)
    return None if n >= 1 << 60 else round(n / _MB, 1)  # cgroup v1 "no limit" is a huge number


def rss_mb():
    """Current resident memory of this process in MB (None where /proc is missing)."""
    return _kb_fields("/proc/self/status", {"VmRSS": "rss"}).get("rss")


def peak_rss_mb():
    """Highest RSS this process has reached (ru_maxrss is KB on Linux, bytes on macOS)."""
    m = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(m / (_MB if sys.platform == "darwin" else 1024), 1)


def cpu_seconds():
    """User + system CPU seconds used by this process so far (all threads)."""
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def cgroup(root="/sys/fs/cgroup"):
    """Container memory/CPU accounting: cgroup v2 first, then v1. Empty dict when neither is visible."""
    out = {}
    for key, v2, v1 in (("mem_mb", "memory.current", "memory/memory.usage_in_bytes"),
                        ("mem_limit_mb", "memory.max", "memory/memory.limit_in_bytes"),
                        ("mem_peak_mb", "memory.peak", "memory/memory.max_usage_in_bytes")):
        v = _bytes_mb(f"{root}/{v2}")
        v = _bytes_mb(f"{root}/{v1}") if v is None else v
        if v is not None:
            out[key] = v
    stat = _read(f"{root}/cpu.stat")
    for line in (stat or "").splitlines():
        k, _, v = line.partition(" ")
        if k in ("usage_usec", "throttled_usec") and v.isdigit():
            out[k.replace("_usec", "_s")] = round(int(v) / 1e6, 1)
        elif k == "nr_throttled" and v.isdigit():
            out["nr_throttled"] = int(v)
    quota = (_read(f"{root}/cpu.max") or "").split()
    if len(quota) == 2 and quota[0].isdigit() and quota[1].isdigit():
        out["cpu_limit"] = round(int(quota[0]) / int(quota[1]), 3)  # vCPUs allowed
    return out


def snapshot():
    up = time.time() - STARTED
    cpu = cpu_seconds()
    out = {
        "uptime_s": round(up), "pid": os.getpid(),
        "rss_mb": rss_mb(), "peak_rss_mb": peak_rss_mb(),
        "cpu_s": round(cpu, 1), "cpu_avg_pct": round(100 * cpu / up, 1) if up >= 10 else None,  # since start, all threads
        "cpus": os.cpu_count(), "threads": len(os.listdir("/proc/self/task")) if os.path.isdir("/proc/self/task") else None,
    }
    try:
        out["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    mem = _kb_fields("/proc/meminfo", {"MemTotal": "total_mb", "MemAvailable": "available_mb"})
    if mem:
        out["machine"] = mem
    cg = cgroup()
    if cg:
        out["cgroup"] = cg
    return {k: v for k, v in out.items() if v is not None}


class Usage:
    """with Usage() as u: ...;  u.fields() -> {"cpu_s", "cpu_pct", "rss_mb", "rss_delta_mb", "peak_rss_mb"} for a log record."""

    def __enter__(self):
        self.t, self.cpu, self.rss0 = time.time(), cpu_seconds(), rss_mb()
        return self

    def __exit__(self, *a):
        self.seconds = time.time() - self.t
        self.cpu_s = cpu_seconds() - self.cpu
        self.rss1 = rss_mb()

    def fields(self):
        if not hasattr(self, "cpu_s"):  # still inside the block (e.g. logging from a finally): measure up to now
            self.__exit__()
        out = {"cpu_s": round(self.cpu_s, 2),
               "cpu_pct": round(100 * self.cpu_s / self.seconds) if self.seconds > 0 else None,
               "rss_mb": self.rss1, "peak_rss_mb": peak_rss_mb()}
        if self.rss0 is not None and self.rss1 is not None:
            out["rss_delta_mb"] = round(self.rss1 - self.rss0, 1)
        return out


def usage_summary(recs):
    """CPU/memory lines from the `usage` field (records from builds before it was logged have none)."""
    use = [r["usage"] for r in recs if isinstance(r.get("usage"), dict)]
    if not use:
        return ["usage: no CPU/memory figures in these records (logged only by builds that include billrag/resources.py)"]

    def stat(name, unit):
        v = sorted(u[name] for u in use if isinstance(u.get(name), (int, float)))
        if not v:
            return None
        return f"{name} median {v[len(v) // 2]:g}{unit}, p90 {v[int(len(v) * 0.9)]:g}{unit}, max {v[-1]:g}{unit}"

    parts = [x for x in (stat("cpu_s", " s"), stat("cpu_pct", "%"), stat("rss_mb", " MB"), stat("peak_rss_mb", " MB")) if x]
    return [f"usage ({len(use)} records): " + "; ".join(parts[:2])] + (["  " + "; ".join(parts[2:])] if parts[2:] else [])
