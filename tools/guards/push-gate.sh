#!/usr/bin/env bash
# push-gate.sh — every push in a script, brief or live command must be gated and readable.
#
# Lessons:
#  * PRs failed in CI on the privacy scan because nobody ran it before pushing
#    (one full CI run plus a fix round per PR). Rule: privacy scan before every push.
#  * The pre-push hook only accepts `git -C <worktree> push`; a bare `git push`
#    can also run in the wrong checkout.
#  * A rejected force-push was hidden by `git push ... 2>&1 | tail -1`. A PR text
#    then described a state that never reached the remote, and the old branch was
#    merged. Rule: never cut or hide push output.
#
# Red for a line that runs a push when:
#  1. it is not `git -C <dir> push`, or the command is a variable (`$G -C . push`),
#  2. it uses --no-verify, or sets core.hooksPath (`git -c core.hooksPath=… push`),
#  3. it forces without a lease (`-f`, `--force`, `+branch`),
#  4. its output is piped into tail/head/grep/sed/awk/cut, or redirected away
#     (`>/dev/null`, `&>`, `2>&1 >file`; `2>&1` alone is fine),
#  5. no privacy scan gates it. Gated means one of:
#     - a command that STARTS with the scan (`[python3] [path/]privacy-scan[.py]`,
#       not `--help`/`-h`, no pipe in it) comes earlier on the line, joined to
#       the push only by '&&' — no ';' or '||' in between;
#     - an earlier scan line in a file that runs with `set -e`, or a gates file.
#     Rule 5 is skipped with PUSH_GATE_SCAN=0. The Bash hook (bash-hook.sh) uses
#     that, because it runs the scan itself instead of reading it off the text.
#
# Usage: push-gate.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=push-gate
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

S='[[:space:]]'
Q="[\"']"
# Git options before the sub-command: `-C <dir>`, `-c <k=v>`, `--git-dir <p>`, … or any `-x`.
# (Only `-C` was allowed before; `git -c k=v push` then hid the push from the gate.)
VALOPT="(-C|-c|--git-dir|--work-tree|--namespace|--config-env)"
GITOPTS="($S+$VALOPT$S+[^[:space:]]+|$S+-[^[:space:]]+)*"
RE_PUSH="(^|[;&|(]|$Q|$S)git$GITOPTS$S+push($S|\$)"
RE_VAR_PUSH="(^|[;&|(]|$Q|$S)[\$][{]?[A-Za-z_][A-Za-z0-9_]*[}]?$GITOPTS$S+push($S|\$)"
RE_PUSH_C="git($S+$VALOPT$S+[^[:space:]]+|$S+-[^[:space:]]+)*$S+-C$S+[^[:space:]]+$GITOPTS$S+push"
RE_HOOKSPATH="core[.]hookspath"
RE_SCAN_CMD="^$S*(python3?$S+)?[^[:space:]]*privacy-scan(\\.py)?($S|\$)"
RE_HELP="$S(--help|-h)($S|\$)"
RE_CUT="push[^|]*\\|$S*(tail|head|grep|sed|awk|cut)($S|\$)"
RE_FORCE="$S(-f|--force|-[a-zA-Z]*f[a-zA-Z]*)($S|\$)|${S}[+][A-Za-z0-9_./:-]+"
RE_NESTED="(^|[;&|(]|$S)((ba|z)?sh$S+-[a-z]*c|eval|ssh)$S"
RE_SET_E="^$S*set$S+-[a-z]*e"

_scan_seen=0; _set_e=0
begin_file() {
  _scan_seen=0; _set_e=0
  case "$1" in *.gates) _set_e=1 ;; esac
  grep -Eq "$RE_SET_E" "$1" && _set_e=1
  return 0
}

# is_scan <segment>: the segment is a real privacy-scan run (not --help, no pipe).
is_scan() {
  [[ $1 =~ $RE_SCAN_CMD ]] || return 1
  [[ $1 =~ $RE_HELP ]] && return 1
  [[ $1 == *'|'* ]] && return 1
  return 0
}

# scan_gates_push <line>: a scan runs earlier on the line and only '&&' joins it to the push.
scan_gates_push() {
  local rest="$1" seg i last=-1
  local -a segs=()
  while [[ $rest == *'&&'* ]]; do segs+=("${rest%%&&*}"); rest="${rest#*&&}"; done
  segs+=("$rest")
  for i in "${!segs[@]}"; do
    if [[ ${segs[$i]} =~ $RE_PUSH ]]; then last=$i; break; fi
  done
  [ "$last" -ge 1 ] || return 1
  # `scan && a; git push`: the ';' sits in the push segment, in front of the push
  seg="${segs[$last]%%push*}"
  [[ $seg == *';'* || $seg == *'||'* ]] && return 1
  for ((i = last - 1; i >= 0; i--)); do
    seg="${segs[$i]}"
    if [[ $seg == *';'* || $seg == *'||'* ]]; then
      # the chain breaks here; only a scan right after the break still gates the push
      seg="${seg##*;}"; seg="${seg##*||}"
      is_scan "$seg"
      return
    fi
    is_scan "$seg" && return 0
  done
  return 1
}

check_line() {
  local line="$3" l pseg after nodir
  is_comment "$line" && return 0
  if is_scan "$line" && [[ $line != *'&&'* && $line != *';'* ]]; then _scan_seen=1; return 0; fi
  [[ $line == *push* ]] || return 0
  # Text inside quotes is data (a message, an awk program, a doc line) and is
  # blanked out — unless the quotes go to a nested shell (`bash -c "..."`, `eval`,
  # `ssh host "..."`), where it is a command.
  if [[ $line =~ $RE_NESTED ]]; then l="$line"; else l="$(unquoted "$line")"; fi
  # >>> guard-logic
  # `git -C $WT push` is fine; only a command word that is a variable is not
  nodir="$(printf '%s' "$l" | sed -E 's/-C[[:space:]]+[^[:space:]]+//g')"
  if [[ $nodir =~ $RE_VAR_PUSH ]]; then
    hit "$1" "$2" "push through a variable command cannot be checked. Write it out: git -C <worktree> push ..."
    return 0
  fi
  [[ $l =~ $RE_PUSH ]] || return 0
  # the push command itself: from 'git' to the next '&&', '||' or ';' (pipes stay in)
  pseg="${BASH_REMATCH[0]}${l#*"${BASH_REMATCH[0]}"}"
  pseg="${pseg%%&&*}"; pseg="${pseg%%||*}"; pseg="${pseg%%;*}"
  after="${pseg%%|*}"
  if ! [[ $pseg =~ $RE_PUSH_C ]]; then
    hit "$1" "$2" "push without 'git -C <worktree>' (the pre-push hook rejects it, and it may run in the wrong checkout)"
  fi
  if [[ $after == *--no-verify* ]]; then
    hit "$1" "$2" "--no-verify skips the pre-push hook"
  fi
  # on the raw line: quotes would hide `-c "core.hooksPath=…"` in the unquoted copy
  if [[ "$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')" =~ $RE_HOOKSPATH ]]; then
    hit "$1" "$2" "core.hooksPath on a push line switches the pre-push hook off"
  fi
  if [[ $after =~ $RE_FORCE ]] && [[ $after != *--force-with-lease* ]]; then
    hit "$1" "$2" "force-push without --force-with-lease can overwrite someone else's commits"
  fi
  if [[ $pseg =~ $RE_CUT ]]; then
    hit "$1" "$2" "push output is cut (tail/head/grep/...): a rejected push looks like success. Show it whole, then run proof-push.sh"
  fi
  after="$(printf '%s' "$after" | sed -E 's/[0-9]?>&[0-9]//g')"
  if [[ $after == *'>'* ]]; then
    hit "$1" "$2" "push output is redirected away: a rejected push looks like success. Show it whole, then run proof-push.sh"
  fi
  if [ "${PUSH_GATE_SCAN:-1}" != 0 ] && ! scan_gates_push "$l" && ! { [ "$_scan_seen" = 1 ] && [ "$_set_e" = 1 ]; }; then
    hit "$1" "$2" "push is not gated by the privacy scan. Use: python3 scripts/privacy-scan.py && git -C <worktree> push ..."
  fi
  # <<< guard-logic
}

lint_main "$@"
