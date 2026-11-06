#!/usr/bin/env bash
# Parallel development with git worktrees on this machine: one checkout per branch, sharing the heavy git-ignored parts.
#
#   scripts/worktree.sh setup                      # once: .venv (Python 3.12), node_modules, index from R2 in the MAIN checkout
#   scripts/worktree.sh new <branch> [--base REF] [--own-data]
#                                                  # worktree at ../llm_rag.worktrees/<branch>, from origin/main by default
#   scripts/worktree.sh link [<path>]              # (re)link shared parts into a worktree (e.g. one made by `claude -w`)
#   scripts/worktree.sh list
#   scripts/worktree.sh rm <branch|path> [--force] # remove the worktree; delete the branch only if it is merged
#
# Shared from the main checkout (symlinks):  .venv  data/  .local.env      Per worktree: node_modules (npm ci)
# data/ is shared, so `python -m billrag ingest|embed|ingest-si|glossary` in one worktree rewrites the index every other
# worktree reads. For parser/index work use `new <branch> --own-data` (a private copy of data/bills/{index.sqlite,vectors.npz}).
# Env: WT_ROOT overrides the worktree directory (default: <main>/../llm_rag.worktrees).
set -euo pipefail

MAIN=$(cd "$(git rev-parse --path-format=absolute --git-common-dir)/.." && pwd)
WT_ROOT=${WT_ROOT:-$(dirname "$MAIN")/$(basename "$MAIN").worktrees}
SHARED=(.venv data .local.env)

die() { echo "worktree.sh: $*" >&2; exit 1; }
say() { echo "-- $*"; }

setup() {
  cd "$MAIN"
  if [ ! -x .venv/bin/python ]; then
    say "creating .venv (Python 3.12, same as the container image)"
    if command -v uv >/dev/null; then uv venv -q --python 3.12 .venv; else python3.12 -m venv .venv; fi
  fi
  say "installing Python requirements into $MAIN/.venv"
  if command -v uv >/dev/null; then
    uv pip install -q --python .venv/bin/python -r requirements-billrag.txt pytest boto3
  else
    .venv/bin/pip install -q -r requirements-billrag.txt pytest boto3
  fi
  say "npm ci (main checkout)"; npm ci --silent
  mkdir -p data/bills
  if [ -f data/bills/index.sqlite ] && [ -f data/bills/vectors.npz ]; then
    say "index already in $MAIN/data/bills (refresh: scripts/fetch_index.py --force)"
  elif [ -f .local.env ]; then
    say "fetching index.sqlite + vectors.npz from R2 (~260 MB)"
    (set -a; . ./.local.env; set +a
     env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY .venv/bin/python scripts/fetch_index.py --bucket "${R2_BUCKET:-gov-rag}")
  else
    say "no .local.env: skipped the R2 index download (search/ask need data/bills/index.sqlite)"
  fi
}

link() {  # link <worktree path> [own-data]
  local wt=$1 own=${2:-}
  [ "$wt" = "$MAIN" ] && die "that is the main checkout; nothing to link"
  [ -e "$wt/.git" ] || die "$wt is not a git worktree"
  for name in "${SHARED[@]}"; do
    [ "$name" = data ] && [ -n "$own" ] && continue
    if [ -L "$wt/$name" ] || [ ! -e "$wt/$name" ]; then
      [ -e "$MAIN/$name" ] || { say "skip $name (missing in main checkout; run: scripts/worktree.sh setup)"; continue; }
      ln -sfn "$MAIN/$name" "$wt/$name"
    else
      say "keep $wt/$name (a real file/dir, not replaced)"
    fi
  done
  if [ -n "$own" ] && [ ! -e "$wt/data" ]; then
    say "copying data/bills index files (private to this worktree)"
    mkdir -p "$wt/data/bills"
    for f in index.sqlite vectors.npz bills.json; do
      [ -f "$MAIN/data/bills/$f" ] && cp --reflink=auto "$MAIN/data/bills/$f" "$wt/data/bills/"
    done
    [ -d "$MAIN/data/bills/pdfs" ] && ln -sfn "$MAIN/data/bills/pdfs" "$wt/data/bills/pdfs"   # PDFs are read-only input
  fi
  if [ -f "$wt/package-lock.json" ] && [ ! -d "$wt/node_modules" ]; then
    say "npm ci"; (cd "$wt" && npm ci --silent --prefer-offline)
  fi
}

new() {
  local branch="" base=origin/main own=""
  while [ $# -gt 0 ]; do
    case $1 in --base) base=$2; shift 2;; --own-data) own=1; shift;; -*) die "unknown option $1";; *) branch=$1; shift;; esac
  done
  [ -n "$branch" ] || die "usage: new <branch> [--base REF] [--own-data]"
  local wt=$WT_ROOT/${branch//\//-}
  [ -e "$wt" ] && die "$wt already exists"
  git -C "$MAIN" fetch -q origin
  mkdir -p "$WT_ROOT"
  if git -C "$MAIN" show-ref -q --verify "refs/heads/$branch"; then
    git -C "$MAIN" worktree add "$wt" "$branch"
  elif git -C "$MAIN" show-ref -q --verify "refs/remotes/origin/$branch"; then
    git -C "$MAIN" worktree add --track -b "$branch" "$wt" "origin/$branch"
  else
    git -C "$MAIN" worktree add --no-track -b "$branch" "$wt" "$base"
  fi
  link "$wt" "$own"
  echo
  echo "Worktree ready: $wt   (branch $branch)"
  echo "  cd $wt && .venv/bin/python -m pytest tests -q        # or: code $wt"
}

rm_wt() {
  local target="" force=""
  while [ $# -gt 0 ]; do case $1 in --force) force=--force; shift;; *) target=$1; shift;; esac; done
  [ -n "$target" ] || die "usage: rm <branch|path> [--force]"
  local wt=$target
  [ -d "$wt" ] || wt=$WT_ROOT/${target//\//-}
  [ -d "$wt" ] || die "no worktree at $target or $wt"
  wt=$(cd "$wt" && pwd)
  [ "$wt" = "$MAIN" ] && die "refusing to remove the main checkout"
  local branch; branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD)
  # drop our links first (a branch older than the /data, /.venv ignore patterns sees them as untracked files);
  # `rm` on a symlink never touches the shared target
  for name in "${SHARED[@]}"; do
    [ -L "$wt/$name" ] && [ "$(readlink "$wt/$name")" = "$MAIN/$name" ] && rm "$wt/$name"
  done
  git -C "$MAIN" worktree remove $force "$wt"
  git -C "$MAIN" fetch -q origin main || true
  if [ "$branch" != HEAD ] && git -C "$MAIN" merge-base --is-ancestor "$branch" origin/main 2>/dev/null; then
    git -C "$MAIN" branch -D "$branch" >/dev/null && say "deleted branch $branch (merged into origin/main)"
  else
    say "kept branch $branch (not merged into origin/main)"
  fi
}

cmd=${1:-}; shift || true
case $cmd in
  setup) setup;;
  new) new "$@";;
  link) link "$(cd "${1:-.}" && git rev-parse --show-toplevel)" "${2:-}";;
  list) git -C "$MAIN" worktree list;;
  rm) rm_wt "$@";;
  *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1;;
esac
