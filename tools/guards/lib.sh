#!/usr/bin/env bash
# lib.sh — shared helpers for the line-based lint guards.
#
# A lint guard sources this file, defines check_line(), and calls lint_main "$@".
# check_line gets: $1 file, $2 line number, $3 line text. It calls hit() for a finding.
#
# Contract for every guard: exit 0 = green, 1 = red (findings), 2 = usage or internal error.
# A line containing "guard-allow: <guard-name>" is skipped. Use it rarely and give a reason
# on the same line, because every allow is a blind spot.

GUARD_NAME="${GUARD_NAME:-guard}"
_hits=0

hit() { # file line message
  printf '%s:%s: [%s] %s\n' "$1" "$2" "$GUARD_NAME" "$3"
  _hits=$((_hits + 1))
}

# True for lines that are only a comment (shell "#", Markdown headings count too).
is_comment() {
  case "$1" in
    [[:space:]]*'#'*) local t="${1#"${1%%[![:space:]]*}"}"; [ "${t#\#}" != "$t" ] ;;
    '#'*) return 0 ;;
    *) return 1 ;;
  esac
}

# unquoted <text>: the text with everything inside '...' and "..." (and any
# backslash-escaped character) replaced by '_', so separators in data do not count.
unquoted() {
  local s="$1" out="" q="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    if [ -n "$q" ]; then
      if [ "$q" = '"' ] && [ "$c" = '\' ]; then out+="__"; i=$((i + 1)); continue; fi
      if [ "$c" = "$q" ]; then q=""; out+="$c"; else out+="_"; fi
    else
      if [ "$c" = '\' ]; then out+="__"; i=$((i + 1)); continue; fi
      case "$c" in "'"|'"') q="$c" ;; esac
      out+="$c"
    fi
  done
  printf '%s' "$out"
}

# Files to lint: plain files as given, folders searched for text-like files.
_lint_files() {
  local p
  for p in "$@"; do
    if [ -d "$p" ]; then
      find "$p" -type f \( -name '*.sh' -o -name '*.bash' -o -name '*.zsh' -o -name '*.md' \
        -o -name '*.mjs' -o -name '*.js' -o -name '*.yml' -o -name '*.yaml' -o -name '*.txt' \
        -o -name '*.gates' \) -not -path '*/node_modules/*' -not -path '*/.git/*' | sort
    elif [ -f "$p" ]; then
      printf '%s\n' "$p"
    else
      echo "[$GUARD_NAME] not found: $p" >&2
      return 2
    fi
  done
}

lint_main() {
  if [ $# -eq 0 ]; then
    echo "usage: $(basename "$0") <file-or-folder>..." >&2
    exit 2
  fi
  local files f n line md fence
  files="$(_lint_files "$@")" || exit 2
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=0; md=0; fence=0
    # In Markdown only fenced code blocks are commands; prose and tables are text.
    case "$f" in *.md) md=1 ;; esac
    if command -v begin_file >/dev/null 2>&1; then begin_file "$f"; fi
    while IFS= read -r line || [ -n "$line" ]; do
      n=$((n + 1))
      if [ "$md" = 1 ]; then
        case "$line" in [[:space:]]*'```'*|'```'*) fence=$((1 - fence)); continue ;; esac
        [ "$fence" = 1 ] || continue
      fi
      case "$line" in *"guard-allow: $GUARD_NAME"*) continue ;; esac
      check_line "$f" "$n" "$line"
    done < "$f"
    if command -v end_file >/dev/null 2>&1; then end_file "$f"; fi
  done <<< "$files"
  if [ "$_hits" -gt 0 ]; then
    echo "[$GUARD_NAME] RED: $_hits finding(s)"
    exit 1
  fi
  echo "[$GUARD_NAME] green"
  exit 0
}
