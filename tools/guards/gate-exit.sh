#!/usr/bin/env bash
# gate-exit.sh — a gate's exit code must decide what runs next.
#
# Lessons:
#  * 2026-09-04: `preflight.sh | tail -1; docker compose up --build ...` — the
#    preflight printed FAILED (an agent was working) and the rebuild ran anyway.
#    Same shape on 2026-07-08.
#  * 2026-10-18: `privacy-scan.py 2>&1 | tail -1 && git push ...` — the pipe makes
#    the line's exit code the one of tail, so '&&' no longer listens to the scan.
#
# A gate is one of: preflight.sh, privacy-scan[.py], run-all.sh, sabotage-probe.sh,
# lessons-check.py, run-gates.sh, post-deploy-probe.sh, proof-push.sh, migration-lock.sh.
# Red when, outside quotes, a gate is
#  1. piped (`gate | tail`) and the line goes on with '&&', ';' or '||' to another command,
#  2. followed by ';' and another command that is not echo/printf (reading rc=$? is fine),
#  3. followed by '||' and a fallback that does not stop (exit, return, false).
# Fine: a gate piped at the end of a line (nothing runs after it), a pipe that ends
# in `grep -q <verdict>`, and a gate name that is only an argument (`grep x preflight.sh`).
#
# Usage: gate-exit.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=gate-exit
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

GATES='(preflight\.sh|privacy-scan(\.py)?|run-all\.sh|sabotage-probe\.sh|lessons-check\.py|run-gates\.sh|post-deploy-probe\.sh|proof-push\.sh|migration-lock\.sh)'
# The gate must be the command that runs (`grep x privacy-scan.py` only reads it):
# at the start of a command, after optional VAR=value words and an interpreter.
RE_GATE="(^|[;&|({\`]|then|do)[[:space:]]*([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*[[:space:]]+)*(((ba|z)?sh|python3?|[^[:space:]]*/python3?)[[:space:]]+)?[^[:space:]]*${GATES}([[:space:]]|\$|[;&|)])"
# A pipe that ends in `grep -q <text>` is a check on the gate's own verdict, e.g.
# `preflight.sh | tail -1 | grep -q "PREFLIGHT OK" || exit 3`.
RE_GREP_Q='\|[[:space:]]*grep[[:space:]]+(-[a-zA-Z]*q[a-zA-Z]*)[^|]*$'
RE_HARMLESS='^[[:space:]]*(echo|printf|:)([[:space:]]|$)'
RE_STOP='^[[:space:]]*([{(][[:space:]]*)?(.*;[[:space:]]*)?(exit|return|false)([[:space:]]|;|$)'

check_line() {
  local l rest seg own next
  is_comment "$3" && return 0
  [[ $3 =~ $RE_GATE ]] || return 0
  l="$(unquoted "$3")"
  [[ $l =~ $RE_GATE ]] || return 0
  rest="${l#*"${BASH_REMATCH[0]}"}"
  [[ ${BASH_REMATCH[0]} == *[\;\&\|\)] ]] && rest="${BASH_REMATCH[0]: -1}$rest"
  # A backquote ends inline code in a brief (`gate`; then ...) — prose follows.
  rest="${rest%%\`*}"
  seg="${rest%%&&*}"
  # >>> guard-logic
  own="${seg%%;*}"; own="${own%%||*}"            # the gate's own pipeline
  [[ $own =~ [[:space:]](--help|-h)([[:space:]]|$) ]] && return 0
  if [[ $own == *'|'* ]]; then
    if [[ $own =~ $RE_GREP_Q ]]; then
      # the verdict text decides; what follows '||' must still stop
      next="${seg#*||}"
      if [[ $seg == *'||'* ]] && ! [[ $next =~ $RE_STOP ]]; then
        hit "$1" "$2" "gate followed by '||' and a fallback that does not stop: a red gate turns green"
      fi
      return 0
    fi
    if [[ $rest == *'&&'* || $seg == *';'* || $seg == *'||'* ]]; then
      hit "$1" "$2" "gate piped into another command, then the line goes on: its exit code is lost. Run the gate alone or with '&&', show its output whole"
      return 0
    fi
  fi
  if [[ $seg == *'||'* ]]; then
    next="${seg#*||}"
    if ! [[ $next =~ $RE_STOP ]]; then
      hit "$1" "$2" "gate followed by '||' and a fallback that does not stop: a red gate turns green"
      return 0
    fi
  elif [[ $seg == *';'* ]]; then
    next="${seg#*;}"
    if [ -n "${next//[[:space:]]/}" ] && ! [[ $next =~ $RE_HARMLESS ]]; then
      hit "$1" "$2" "gate followed by ';' and another command: the next step runs even when the gate is red. Use '&&'"
      return 0
    fi
  fi
  # <<< guard-logic
}

lint_main "$@"
