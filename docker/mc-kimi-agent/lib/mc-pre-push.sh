#!/bin/bash
# mc-pre-push.sh — git pre-push hook installed globally in mc-agent-base.
#
# Called by git with remote-name + remote-url on stdin + argv:
#   $ git push <remote>    →   hook runs with:   $1=<remote> $2=<remote-url>
#
# Purpose: last-line-of-defense against pushing to the wrong GitHub repo.
# If MC wrote an expected-remote file into the workspace at dispatch time,
# this hook compares the push's actual origin URL against that expectation
# and aborts on mismatch.
#
# Expectation file: ${GIT_TOP}/.mc-expected-remote
#   Single line, the URL form of `git remote get-url origin` as MC wrote it.
#   Absence of the file = no expectation, hook is silent (pass-through).
#
# Why this matters: 2026-04-19 incident — FreeCode wandered into an unrelated
# repo on the agent's host-mount and pushed two commits there. Layer 2
# (no silent-fallback on clone) closes most of that hole. This hook is the
# belt-and-braces for the "agent worked in a different directory" edge.

set -euo pipefail

git_top=$(git rev-parse --show-toplevel 2>/dev/null || true)
if [ -z "$git_top" ]; then
    # Not in a git repo — something else is going on, let git handle it.
    exit 0
fi

expected_file="${git_top}/.mc-expected-remote"
if [ ! -f "$expected_file" ]; then
    # No MC expectation for this repo. Agent is likely doing something
    # unrelated (e.g. a throwaway experiment). Don't block.
    exit 0
fi

expected=$(cat "$expected_file" | head -1 | tr -d '[:space:]')
actual=$(git remote get-url "$1" 2>/dev/null || true)

# Normalize: strip trailing .git, lowercase host-part. Both ssh:// and
# https://github.com/owner/repo(.git) should compare equal if the
# owner/repo pair matches.
_strip() {
    local url="$1"
    url="${url%.git}"
    url="${url/git@github.com:/https://github.com/}"
    printf '%s' "$url"
}
expected_norm=$(_strip "$expected")
actual_norm=$(_strip "$actual")

if [ "$expected_norm" != "$actual_norm" ]; then
    cat >&2 <<EOF
────────────────────────────────────────────────────────────────────
   MC PRE-PUSH GUARD — WRONG REMOTE
────────────────────────────────────────────────────────────────────
   Expected: ${expected_norm}
   Actual:   ${actual_norm}

   Push aborted. This agent was dispatched to work against the
   'Expected' remote but is about to push to 'Actual'. That's the
   2026-04-19 FreeCode bug class — stop before the wrong repo gets
   commits.

   If 'Actual' really is the intended target, remove or update:
       ${expected_file}
   ...and try again.
────────────────────────────────────────────────────────────────────
EOF
    exit 1
fi

exit 0
