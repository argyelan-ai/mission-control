#!/usr/bin/env bash
# sabotage-probe.sh — prove that each guard's logic is what turns its fixtures red.
#
# For every guard: copy this folder, comment out everything between
# "# >>> guard-logic" and "# <<< guard-logic" in that one guard, and run run-all.sh
# on the copy. The probe passes only when the sabotaged guard then MISSES at least
# one fixture (a .bad turns green). A guard with no marked logic, or one that is
# still red/green as before, fails the probe: its fixtures do not test its logic.
#
# Usage: sabotage-probe.sh [guard-dir]   Exit: 0 every guard bites, 1 otherwise.
set -uo pipefail

DIR="$(cd "${1:-$(dirname "$0")}" && pwd)" || exit 2
NOT_GUARDS=" run-all.sh sabotage-probe.sh lib.sh lint.sh "
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
fail=0; n=0

for g in "$DIR"/*.sh; do
  base="$(basename "$g")"
  case "$NOT_GUARDS" in *" $base "*) continue ;; esac
  name="${base%.sh}"; n=$((n + 1))
  if ! grep -q '^[[:space:]]*# >>> guard-logic' "$g"; then
    echo "FAIL  $name: no '# >>> guard-logic' block — nothing to sabotage"
    fail=1; continue
  fi
  copy="$work/$name"; rm -rf "$copy"; cp -R "$DIR" "$copy"
  awk '/^[[:space:]]*# >>> guard-logic/{on=1; print; next}
       /^[[:space:]]*# <<< guard-logic/{on=0; print; next}
       on{print "# SABOTAGED " $0; next} {print}' "$g" > "$copy/$base"
  if ! bash -n "$copy/$base" 2>/dev/null; then
    echo "FAIL  $name: script does not parse with its logic commented out — keep the block self-contained"
    fail=1; continue
  fi
  out="$(bash "$copy/run-all.sh" "$copy" 2>&1)"
  missed="$(printf '%s\n' "$out" | grep -E "^FAIL  ${name} +${name}[^ ]*\.bad +exit 0" | wc -l | tr -d ' ')"
  if [ "$missed" -gt 0 ]; then
    echo "ok    $name: with its logic commented out, $missed .bad fixture(s) turn green — the guard bites"
  else
    echo "FAIL  $name: commenting out its logic changed nothing — the fixtures do not test the logic"
    printf '%s\n' "$out" | grep -E "^(FAIL|ok) +$name " | sed 's/^/      | /'
    fail=1
  fi
done

echo "---"
echo "$n guards sabotaged: $([ $fail -eq 0 ] && echo EVERY GUARD BITES || echo PROBE FAILED)"
exit "$fail"
