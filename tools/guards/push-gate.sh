#!/usr/bin/env bash
# push-gate.sh — every push in a script or brief must be gated and readable.
#
# Lessons:
#  * PRs failed in CI on the privacy scan because nobody ran it before pushing
#    (one full CI run plus a fix round per PR). Rule: privacy scan before every push.
#  * The pre-push hook only accepts `git -C <worktree> push`; a bare `git push`
#    can also run in the wrong checkout.
#  * A rejected force-push was hidden by `git push ... 2>&1 | tail -1`. A PR text
#    then described a state that never reached the remote, and the old branch was
#    merged. Rule: never cut push output.
#
# Red for a line that runs `git ... push` when:
#  1. it is not `git -C <dir> push`,
#  2. it uses --no-verify,
#  3. its output is piped into tail/head/grep/sed/awk,
#  4. no privacy scan gates it: neither `privacy-scan... && ... git ... push` on the
#     same line (no ';' or '||' between), nor an earlier privacy-scan line in a file
#     that runs with `set -e` or is a gates file (*.gates, see run-gates.sh).
#
# Usage: push-gate.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=push-gate
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RE_PUSH='(^|[;&|([:space:]])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?([[:space:]]+-[^[:space:]]+)*[[:space:]]+push([[:space:]]|$)'
RE_PUSH_C='git[[:space:]]+-C[[:space:]]+[^[:space:]]+[[:space:]]+push'
RE_SCAN_GATED='privacy-scan[^;|]*&&.*git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push'
RE_SCAN_ALONE='^[[:space:]]*(python3?[[:space:]]+)?[^[:space:]]*privacy-scan[^;|&]*$'
RE_CUT='git[^|]*push[^|]*\|[[:space:]]*(tail|head|grep|sed|awk)'
RE_SET_E='^[[:space:]]*set[[:space:]]+-[a-z]*e'

_scan_seen=0; _set_e=0
begin_file() {
  _scan_seen=0; _set_e=0
  case "$1" in *.gates) _set_e=1 ;; esac
  grep -Eq "$RE_SET_E" "$1" && _set_e=1
  return 0
}

check_line() {
  is_comment "$3" && return 0
  if [[ $3 =~ $RE_SCAN_ALONE ]]; then _scan_seen=1; return 0; fi
  [[ $3 =~ $RE_PUSH ]] || return 0
  # >>> guard-logic
# SABOTAGED   if ! [[ $3 =~ $RE_PUSH_C ]]; then
# SABOTAGED     hit "$1" "$2" "push without 'git -C <worktree>' (the pre-push hook rejects it, and it may run in the wrong checkout)"
# SABOTAGED   fi
# SABOTAGED   if [[ $3 == *--no-verify* ]]; then
# SABOTAGED     hit "$1" "$2" "--no-verify skips the pre-push hook"
# SABOTAGED   fi
# SABOTAGED   if [[ $3 =~ $RE_CUT ]]; then
# SABOTAGED     hit "$1" "$2" "push output is cut (tail/head/grep): a rejected push looks like success. Show it whole, then run proof-push.sh"
# SABOTAGED   fi
# SABOTAGED   if ! [[ $3 =~ $RE_SCAN_GATED ]] && ! { [ "$_scan_seen" = 1 ] && [ "$_set_e" = 1 ]; }; then
# SABOTAGED     hit "$1" "$2" "push is not gated by the privacy scan. Use: python3 scripts/privacy-scan.py && git -C <worktree> push ..."
# SABOTAGED   fi
  # <<< guard-logic
}

lint_main "$@"
