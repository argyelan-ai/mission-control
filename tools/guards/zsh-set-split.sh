#!/usr/bin/env bash
# zsh-set-split.sh — catch `set -- $var` that relies on word splitting.
#
# Lesson: the agent shell is zsh. zsh does NOT split an unquoted $var into words,
# so `set -- $p` puts the whole string into $1 and leaves $2 empty. Twice this made
# `git worktree add "$1" -b "$2" <ref>` create a local branch named after the ref.
#
# Red: `set -- $name` / `set -- ${name}` (unquoted, not zsh's explicit ${=name}).
# Not checked: files that declare bash (a bash shebang or `# shellcheck shell=bash`),
# because bash does split there. Live commands and briefs run in the agent's zsh.
# Fix: `read -r a b <<< "$p"`, or write the commands out one by one.
# Not checked inside quotes: `ssh host 'for r in ...; do set -- $r; ...'` runs
# in the remote bash.
#
# Usage: zsh-set-split.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=zsh-set-split
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RE_SET_SPLIT='(^|[;&|[:space:]])set[[:space:]]+--[[:space:]]+[$][{]?[A-Za-z_@*]'

_bash=0
begin_file() {
  _bash=0
  if head -1 "$1" | grep -Eq '^#!.*[/ ]bash([[:space:]]|$)' || grep -q '^# shellcheck shell=bash' "$1"; then
    _bash=1
  fi
  return 0
}

check_line() {
  [ "$_bash" = 1 ] && return 0
  is_comment "$3" && return 0
  # Inside quotes it is data, or code for another shell (`ssh host '...'` runs bash).
  local l; l="$(unquoted "$3")"
  # >>> guard-logic
  if [[ $l =~ $RE_SET_SPLIT ]] && [[ $l != *'${='* ]]; then
    hit "$1" "$2" 'set -- $var relies on word splitting, which zsh does not do. Use: read -r a b <<< "$var"'
  fi
  # <<< guard-logic
}

lint_main "$@"
