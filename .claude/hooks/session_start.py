#!/usr/bin/env python3
"""SessionStart hook: tell Claude, at the start of a session, what it would otherwise find out the hard way.

Wired up in .claude/settings.json. Local checks only, plus one `git fetch origin main` (short timeout, skipped on failure):
  - live vs main: AGENTS.md "Deployed vs main" names the live build; if the code on origin/main hashes to a different
    build id (same hash as billrag.chatlog.build_id), list the commits merged since the live commit - they are NOT live.
  - stale branch: the checked-out branch is already contained in origin/main (its PR was merged), so new work must
    restart it from origin/main instead of stacking onto merged history.
  - missing tools: pytest / boto3 / node_modules, so the first test run doesn't fail.
Prints one additionalContext block; never changes, pushes or deploys anything. Any error -> silent exit 0.
"""
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args, timeout=10):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else None


def text(*args, **kw):
    out = git(*args, **kw)
    return out.decode().strip() if out is not None else ""


def build_id_at(ref):
    """billrag.chatlog.build_id() for the billrag/*.py files at a git ref (same file order and hashing)."""
    names = [n for n in text("ls-tree", "--name-only", f"{ref}:billrag").split("\n") if n.endswith(".py")]
    if not names:
        return None
    h = hashlib.sha1()
    for name in sorted(names):
        h.update(name.encode() + (git("show", f"{ref}:billrag/{name}") or b""))
    return h.hexdigest()[:8]


def deploy_status(notes):
    m = re.search(r"Live since (.{0,40}?): build `([0-9a-f]{8})` = `main` at `([0-9a-f]{6,40})`", (ROOT / "AGENTS.md").read_text())
    if not m:
        notes.append("AGENTS.md has no parsable 'Live since ...: build `X` = `main` at `SHA`' line; check what is live before claiming it.")
        return
    when, live_build, live_sha = m.groups()
    main_build = build_id_at("origin/main")
    if main_build == live_build:
        notes.append(f"Deploy: origin/main has the same code as the live build {live_build} (live since {when}); nothing undeployed.")
        return
    log = text("log", "--oneline", "--first-parent", f"{live_sha}..origin/main") if git("cat-file", "-e", live_sha) is not None else ""
    lines = log.split("\n") if log else []
    notes.append(f"Deploy: live build is {live_build} (`main` at {live_sha}, since {when}), but origin/main's code is build "
                 f"{main_build}. Merged but NOT live ({len(lines)} first-parent commits):\n  " + "\n  ".join(lines[:10])
                 + ("\n  ..." if len(lines) > 10 else "")
                 + "\nNever tell the owner these are live; deploying needs their explicit go-ahead.")


def branch_status(notes):
    branch = text("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch in ("HEAD", "main"):
        return
    head, main = text("rev-parse", "HEAD"), text("rev-parse", "origin/main")
    if head and head != main and git("merge-base", "--is-ancestor", "HEAD", "origin/main") is not None:
        notes.append(f"Branch: `{branch}` is already merged into origin/main. Before new work: "
                     f"`git fetch origin main && git checkout -B {branch} origin/main` (then push with -f), so the next PR "
                     "is a fresh one on top of main.")
    elif head and head != main:
        behind = text("rev-list", "--count", "HEAD..origin/main")
        if behind not in ("", "0"):
            notes.append(f"Branch: `{branch}` is {behind} commits behind origin/main and has unmerged work; rebase onto it before a PR.")


def tools_status(notes):
    missing = [m for m in ("pytest", "boto3", "numpy") if importlib.util.find_spec(m) is None]
    if not (ROOT / "node_modules").is_dir():
        missing.append("node_modules")
    if missing:
        notes.append(f"Tools: missing {', '.join(missing)}. Install with `pip install -q -r requirements-billrag.txt pytest boto3` "
                     "and `npm ci` before running tests (boto3 is only for R2 scripts).")


def main():
    try:
        json.load(sys.stdin)
    except Exception:
        pass
    notes = []
    try:
        if git("fetch", "-q", "origin", "main", timeout=20) is None:
            notes.append("git fetch origin main failed; the checks below use the last fetched origin/main.")
    except Exception:
        notes.append("git fetch origin main timed out; the checks below use the last fetched origin/main.")
    for check in (deploy_status, branch_status, tools_status):
        try:
            check(notes)
        except Exception as e:
            notes.append(f"{check.__name__} skipped: {type(e).__name__}: {e}")
    if notes:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "Session-start checks (.claude/hooks/session_start.py):\n- "
                                                 + "\n- ".join(notes)}}))


if __name__ == "__main__":
    main()
