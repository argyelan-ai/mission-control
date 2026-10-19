#!/usr/bin/env bash
# tmp-previews.sh — previews, deploy folders and worktrees do not belong in /tmp.
#
# Lesson: preview and deploy folders were built under /tmp. macOS clears /tmp
# entries after a few days, so one preview lost its deploy link and a handoff
# pointed to nothing. It happened twice before the rule stuck.
#
# Red for a line that names /tmp/, /private/tmp/ or $TMPDIR together with one of:
# preview, vorschau, deploy, handoff, worktree, render, screenshot, .vercel, release.
# Short-lived files (health stamps, scratch output read once) stay allowed.
# Opt out on one line with "guard-allow: tmp-previews (<reason>)".
#
# Usage: tmp-previews.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
GUARD_NAME=tmp-previews
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RE_TMP='(^|[^A-Za-z0-9_.-])(/private)?/tmp/|[$][{]?TMPDIR'
RE_DURABLE='([Pp]review|[Vv]orschau|[Dd]eploy|[Hh]andoff|[Ww]orktree|[Rr]ender|[Ss]creenshot|[.]vercel|[Rr]elease)'

check_line() {
  is_comment "$3" && return 0
  # >>> guard-logic
  if [[ $3 =~ $RE_TMP ]] && [[ $3 =~ $RE_DURABLE ]]; then
    hit "$1" "$2" "durable output under /tmp (macOS deletes it after a few days). Use a folder under the project or \$HOME"
  fi
  # <<< guard-logic
}

lint_main "$@"
