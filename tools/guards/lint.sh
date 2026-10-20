#!/usr/bin/env bash
# lint.sh — run every lint guard over scripts, briefs or workflow files.
#
# Use it on the files that agents actually follow: build briefs, workflow
# scripts, skill files, deploy scripts. Each guard prints file:line findings.
#
# Usage: lint.sh <file-or-folder>...   Exit: 0 all green, 1 any guard red, 2 usage.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
[ $# -gt 0 ] || { echo "usage: lint.sh <file-or-folder>..." >&2; exit 2; }
rc=0
for g in zsh-set-split push-gate gate-exit background-tests tmp-previews; do
  bash "$HERE/$g.sh" "$@"
  r=$?
  [ "$r" -gt "$rc" ] && rc=$r
done
exit "$rc"
