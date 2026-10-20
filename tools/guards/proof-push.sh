#!/usr/bin/env bash
# proof-push.sh — a push is done only when the remote branch points at HEAD.
#
# Lesson: a --force-with-lease push was rejected, and `| tail -1` hid the rejection.
# The PR text was then edited to describe a state that never reached the remote,
# and a watcher merged the OLD branch on green CI. Production was down for minutes.
# CI did not help: it tested the branch against its own base, not against main.
#
# Two steps, so the decision can be tested without a network:
#   capture  — read HEAD and the remote branch SHA (read-only: rev-parse, ls-remote)
#   evaluate — red unless both are full SHAs and equal
#
# Usage: proof-push.sh [-C <worktree>] [--remote <name>] [--branch <name>]   capture + evaluate
#        proof-push.sh --capture [-C ...]                                   print evidence only
#        proof-push.sh <evidence-file>                                      evaluate a capture
# Evidence format: key=value lines with branch=, head=, remote=.
# Exit:  0 remote == HEAD, 1 not proven, 2 usage or git error.
set -u

dir=.; remote=origin; branch=""; mode=live; evidence=""
while [ $# -gt 0 ]; do
  case "$1" in
    -C) dir="${2:?}"; shift ;;
    --remote) remote="${2:?}"; shift ;;
    --branch) branch="${2:?}"; shift ;;
    --capture) mode=capture ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "proof-push: unknown option $1" >&2; exit 2 ;;
    *) mode=evaluate; evidence="$1" ;;
  esac
  shift
done

capture() {
  local head rem
  [ -n "$branch" ] || branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD)" || return 2
  head="$(git -C "$dir" rev-parse HEAD)" || return 2
  rem="$(git -C "$dir" ls-remote "$remote" "refs/heads/$branch")" || return 2
  printf 'branch=%s\nhead=%s\nremote=%s\n' "$branch" "$head" "${rem%%[[:space:]]*}"
}

evaluate() { # reads evidence on stdin
  local key val b="" h="" r=""
  while IFS='=' read -r key val || [ -n "$key" ]; do
    case "$key" in branch) b="$val" ;; head) h="$val" ;; remote) r="$val" ;; esac
  done
  echo "branch: ${b:-?}"
  echo "HEAD:   ${h:-<empty>}"
  echo "remote: ${r:-<missing>}"
  # >>> guard-logic
  if ! [[ $h =~ ^[0-9a-f]{40}$ ]]; then echo "PUSH NOT PROVEN: HEAD is not a full SHA"; return 1; fi
  if [ -z "$r" ]; then echo "PUSH NOT PROVEN: the branch does not exist on the remote"; return 1; fi
  if [ "$h" != "$r" ]; then echo "PUSH NOT PROVEN: the remote has another commit than HEAD (push rejected or not done)"; return 1; fi
  # <<< guard-logic
  echo "PUSH PROVEN: remote == HEAD"
  return 0
}

case "$mode" in
  capture) capture ;;
  evaluate)
    [ -f "$evidence" ] || { echo "proof-push: not found: $evidence" >&2; exit 2; }
    evaluate < "$evidence" ;;
  live)
    ev="$(capture)" || { echo "proof-push: git failed" >&2; exit 2; }
    printf '%s\n' "$ev" | evaluate ;;
esac
