#!/usr/bin/env bash
# run-all.sh — prove that every guard bites.
#
# For every guard <name>.sh in this folder:
#   fixtures/<name>.bad  and fixtures/<name>.*.bad   must make the guard exit 1 (red)
#   fixtures/<name>.good and fixtures/<name>.*.good  must make the guard exit 0 (green)
# A guard without at least one .bad and one .good fixture fails the run,
# and so does a fixture whose guard is missing.
# Exit 2 from a guard (usage or internal error) never counts as "red".
#
# Usage: run-all.sh [guard-dir]     (default: the folder this script is in)
# Exit:  0 all pairs as expected, 1 at least one mismatch.
set -uo pipefail

DIR="$(cd "${1:-$(dirname "$0")}" && pwd)" || exit 2
FIX="$DIR/fixtures"
# Scripts in this folder that are not guards themselves.
NOT_GUARDS=" run-all.sh sabotage-probe.sh lib.sh lint.sh "

fail=0; pairs=0; guards=0
for g in "$DIR"/*.sh; do
  base="$(basename "$g")"
  case "$NOT_GUARDS" in *" $base "*) continue ;; esac
  name="${base%.sh}"
  guards=$((guards + 1))
  nbad=0; ngood=0
  for f in "$FIX/$name".bad "$FIX/$name".*.bad "$FIX/$name".good "$FIX/$name".*.good; do
    [ -f "$f" ] || continue
    case "$f" in *.bad) want=1; nbad=$((nbad + 1)) ;; *) want=0; ngood=$((ngood + 1)) ;; esac
    out="$(cd "$FIX" && bash "$g" "$f" 2>&1)"; rc=$?
    pairs=$((pairs + 1))
    if [ "$rc" -eq "$want" ]; then
      printf 'ok    %-22s %-34s exit %s\n' "$name" "$(basename "$f")" "$rc"
    else
      printf 'FAIL  %-22s %-34s exit %s, expected %s\n' "$name" "$(basename "$f")" "$rc" "$want"
      printf '%s\n' "$out" | sed 's/^/      | /'
      fail=1
    fi
  done
  if [ "$nbad" -eq 0 ] || [ "$ngood" -eq 0 ]; then
    printf 'FAIL  %-22s needs at least one .bad and one .good fixture (has %s bad, %s good)\n' "$name" "$nbad" "$ngood"
    fail=1
  fi
done

# A fixture without its guard means the guard was deleted or renamed.
for f in "$FIX"/*.bad "$FIX"/*.good; do
  [ -f "$f" ] || continue
  owner="$(basename "$f")"; owner="${owner%%.*}"
  if [ ! -f "$DIR/$owner.sh" ]; then
    printf 'FAIL  %-22s fixture %s has no guard %s.sh\n' "$owner" "$(basename "$f")" "$owner"
    fail=1
  fi
done

if [ "$guards" -eq 0 ]; then echo "FAIL  no guards found in $DIR"; exit 1; fi
echo "---"
echo "$guards guards, $pairs fixture runs: $([ $fail -eq 0 ] && echo ALL AS EXPECTED || echo MISMATCH)"
exit "$fail"
