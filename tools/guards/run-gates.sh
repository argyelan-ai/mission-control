#!/usr/bin/env bash
# run-gates.sh — run gates one by one and stop hard at the first red one.
#
# Lessons:
#  * `preflight.sh | tail -1; docker compose up --build ...` — the gate printed
#    FAILED (an agent was working), and the rebuild ran anyway, because ';' (and
#    '| tail' without pipefail) swallow the exit code. A gate whose exit code nobody reads is decoration.
#  * A build hidden in an `A && B &` chain silently did not run.
#
# A gates file has one command per line. Empty lines and '#' comments are skipped.
# Before anything runs, every line is checked OUTSIDE quotes: ';', '||' and a lone
# '&' (background) are refused, because each of them can hide a failure.
# Text inside '...' or "..." is data (`psql -c "select 1;"`, `python3 -c "a; b"`)
# and is not refused. A pipe is allowed: the gates run with
# `bash -o pipefail -c`, so `false | tail -1` is still red. Output is unchanged.
# Known limit: a nested shell in quotes (`bash -c 'a; b'`) is not looked into.
# The first gate that exits non-zero stops the run.
#
# Usage: run-gates.sh <gates-file>
#        run-gates.sh -- '<gate 1>' '<gate 2>' ...
# Exit:  0 all gates green, 1 a gate was red or a line was refused, 2 usage.
set -u
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"   # unquoted()

if [ $# -eq 0 ]; then
  echo "usage: run-gates.sh <gates-file> | run-gates.sh -- '<gate>'..." >&2
  exit 2
fi

gates=()
if [ "$1" = "--" ]; then
  shift
  for g in "$@"; do gates+=("$g"); done
else
  [ -f "$1" ] || { echo "run-gates: not found: $1" >&2; exit 2; }
  while IFS= read -r line || [ -n "$line" ]; do
    t="${line#"${line%%[![:space:]]*}"}"
    case "$t" in ''|'#'*) continue ;; esac
    gates+=("$t")
  done < "$1"
fi
[ ${#gates[@]} -gt 0 ] || { echo "run-gates: no gates given" >&2; exit 2; }

# ';'  — the next command runs whatever the first one returned
# '||' — a red gate is turned green by its fallback
# '&'  — a lone '&' (not '&&', '>&', '&>') sends a command to the background
RE_HIDES='(;|\|\||(^|[^&>])&([^&>]|$))'
refused=0
i=0
for g in "${gates[@]}"; do
  i=$((i + 1))
  bare="$(unquoted "$g")"
  # >>> guard-logic
  if [[ $bare =~ $RE_HIDES ]]; then
    echo "REFUSED gate $i: '$g' — ';', '||' or a lone '&' outside quotes can hide a failure. One command per line."
    refused=1
  fi
  # <<< guard-logic
done
[ "$refused" = 0 ] || { echo "GATES REFUSED — nothing was run"; exit 1; }

n=${#gates[@]}
i=0
for g in "${gates[@]}"; do
  i=$((i + 1))
  echo "[gate $i/$n] $g"
  bash -o pipefail -c "$g"
  rc=$?
  # >>> guard-logic
  if [ "$rc" -ne 0 ]; then
    echo "GATES FAILED at $i/$n (exit $rc): $g"
    echo "Gates after this one were not run."
    exit 1
  fi
  # <<< guard-logic
  echo "[gate $i/$n] ok"
done
echo "GATES GREEN ($n/$n)"
exit 0
