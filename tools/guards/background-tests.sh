#!/usr/bin/env bash
# background-tests.sh — tests must not run in the background without a wait.
#
# Lesson: build agents started the big test suite in the background, then ended
# with "waiting for the background run" — without a commit and without a report.
# Three times in three days; each time the uncommitted tree sat in a shared
# checkout and the orchestrator had to chase the agent.
# Rule: run tests in the foreground, commit before the report.
#
# Lint mode (scripts, briefs, workflow files). Red when a test command
# (pytest, vitest, jest, npm test, npm run test..., go test, cargo test):
#  * ends with '&' (sent to the background) and no `wait` follows later in the file,
#  * runs under nohup or setsid,
#  * shares a line (or the line before) with `run_in_background: true`.
#
# Worktree mode: --worktree <dir> is red when the tree has uncommitted changes —
# run it before an agent reports "done".
#
# Usage: background-tests.sh <file-or-folder>...  |  background-tests.sh --worktree <dir>
# Exit:  0 green, 1 red, 2 usage.
GUARD_NAME=background-tests
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

if [ "${1:-}" = "--worktree" ]; then
  wt="${2:?usage: background-tests.sh --worktree <dir>}"
  st="$(git -C "$wt" status --porcelain)" || exit 2
  if [ -n "$st" ]; then
    printf '%s\n' "$st" | sed 's/^/  /'
    echo "[$GUARD_NAME] RED: uncommitted changes in $wt — commit before the report"
    exit 1
  fi
  echo "[$GUARD_NAME] green: $wt is committed ($(git -C "$wt" rev-parse --short HEAD))"
  exit 0
fi

RE_TEST='(pytest|vitest|jest|npm[[:space:]]+(run[[:space:]]+)?test|go[[:space:]]+test|cargo[[:space:]]+test)'
RE_AMP_END='[^&]&[[:space:]]*$'
RE_DETACH='(nohup|setsid)[[:space:]]'
RE_TOOL_BG='run_in_background["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"']?true'
RE_WAIT='^[[:space:]]*wait([[:space:]]|$)'

_pending_line=0; _prev=""
begin_file() { _pending_line=0; _prev=""; return 0; }
end_file() {
  # >>> guard-logic
  if [ "$_pending_line" -gt 0 ]; then
    hit "$1" "$_pending_line" "test run sent to the background with no 'wait' after it — the agent can end without commit or result"
  fi
  # <<< guard-logic
  return 0
}

check_line() {
  local cur="$3" prev="$_prev"
  _prev="$cur"
  is_comment "$cur" && return 0
  if [[ $cur =~ $RE_WAIT ]]; then _pending_line=0; return 0; fi
  # >>> guard-logic
  if [[ $cur =~ $RE_TEST ]]; then
    if [[ $cur =~ $RE_DETACH ]]; then
      hit "$1" "$2" "test run detached with nohup/setsid — run tests in the foreground"
    elif [[ $cur =~ $RE_AMP_END ]]; then
      [ "$_pending_line" -gt 0 ] || _pending_line="$2"
    fi
    if [[ $cur =~ $RE_TOOL_BG ]] || [[ $prev =~ $RE_TOOL_BG ]]; then
      hit "$1" "$2" "test run with run_in_background — run it in the foreground and commit before the report"
    fi
  elif [[ $cur =~ $RE_TOOL_BG ]] && [[ $prev =~ $RE_TEST ]]; then
    hit "$1" "$2" "test run with run_in_background — run it in the foreground and commit before the report"
  fi
  # <<< guard-logic
}

lint_main "$@"
