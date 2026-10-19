#!/usr/bin/env bash
# zsh-set-split.sh — catch `set -- $var` that relies on word splitting.
#
# Lesson: the agent shell is zsh. zsh does NOT split an unquoted $var into words,
# so `set -- $p` puts the whole string into $1 and leaves $2 empty. Twice this made
# `git worktree add "$1" -b "$2" <ref>` create a local branch named after the ref.
#
# Red: `set -- $name` / `set -- ${name}` (unquoted, not zsh's explicit ${=name}).
# Fix: `read -r a b <<< "$p"`, or write the commands out one by one.
#
# Usage: zsh-set-split.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=zsh-set-split
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RE_SET_SPLIT='(^|[;&|[:space:]])set[[:space:]]+--[[:space:]]+[$][{]?[A-Za-z_@*]'

check_line() {
  is_comment "$3" && return 0
  # >>> guard-logic
  if [[ $3 =~ $RE_SET_SPLIT ]] && [[ $3 != *'${='* ]]; then
    hit "$1" "$2" 'set -- $var relies on word splitting, which zsh does not do. Use: read -r a b <<< "$var"'
  fi
  # <<< guard-logic
}

lint_main "$@"
