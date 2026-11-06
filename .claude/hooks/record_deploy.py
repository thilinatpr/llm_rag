#!/usr/bin/env python3
"""PostToolUse hook (Bash): after a successful `wrangler deploy`, tell Claude to record it in AGENTS.md and merge that.

Wired up in .claude/settings.json. Reads the hook payload on stdin; does nothing (exit 0, no output) unless the command
was a real `wrangler deploy` (not --dry-run) whose output ends with "Current Version ID". It then collects what it can
see locally - commit, build id, Worker version, container image - and returns them as additionalContext with the steps
from AGENTS.md "Deploying from the cloud sandbox" step 6. It never pushes, opens PRs or merges by itself: Claude does
that with the GitHub tools, so the usual review of each step still applies.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def git(*args):
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def output_text(resp):
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        return "\n".join(str(resp.get(k) or "") for k in ("stdout", "stderr", "output"))
    return ""


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    cmd = str((data.get("tool_input") or {}).get("command", ""))
    if not re.search(r"\bwrangler\s+deploy\b", cmd) or "--dry-run" in cmd:
        return
    out = output_text(data.get("tool_response"))
    worker = re.search(r"Current Version ID:\s*([0-9a-f-]{8,})", out)
    if not worker:
        return  # failed deploy, or output cut off before the end: nothing to record
    image = re.findall(r"^\s*[│|]?\s*\+\s*image\s*=\s*\"[^\"]*@(sha256:[0-9a-f]{8})", out, re.M)
    sha = git("rev-parse", "--short", "HEAD")
    on_main = bool(git("branch", "-r", "--contains", "HEAD", "--list", "origin/main"))
    try:
        sys.path.insert(0, str(ROOT))
        from billrag.chatlog import build_id
        build = build_id()
    except Exception as e:
        build = f"unknown ({type(e).__name__})"
    facts = (f"commit {sha or 'unknown'} ({'on origin/main' if on_main else 'NOT on origin/main'}), build {build}, "
             f"Worker version {worker.group(1)[:8]}, container image {image[-1] + '...' if image else 'unchanged/not shown'}")
    ctx = (
        f"A production deploy just finished: {facts}.\n"
        "Record it now (AGENTS.md 'Deploying from the cloud sandbox' step 6), without waiting to be asked:\n"
        "1. Wait for the container rollout to reach 'completed' (step 5) and note the container app version; the previous "
        "live version/image is the rollback target.\n"
        "2. On your designated branch, freshly based on origin/main (restart it from origin/main if its last PR was merged), "
        "update the 'Deployed vs main (keep this honest)' paragraph of AGENTS.md: build id, commit, what it contains, Worker "
        "version, container version and image, previous version, rollback commit, and 'not yet confirmed from a chat log' "
        "unless a log already shows the new build. Change nothing else.\n"
        "3. Run `python -m pytest tests -q`, commit, push, open a PR to main with the GitHub tools, and merge it with "
        "merge_method 'merge' (not squash) once it is clean.\n"
        "4. Tell the owner what is live and what is only merged."
        + ("" if on_main else "\nWARNING: the deployed commit is not on origin/main. AGENTS.md says to deploy main or merge "
           "it right after with a merge commit: say so to the owner before recording.")
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ctx},
                      "systemMessage": f"Deploy detected ({facts}): recording it in AGENTS.md"}))


if __name__ == "__main__":
    main()
