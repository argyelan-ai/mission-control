#!/usr/bin/env bash
# post-push-hook.sh — the push proof at the place where the push happens: a Claude
# Code PostToolUse hook on the Bash tool. After a command that pushed, it checks
# that the remote branch really points at HEAD (proof-push.sh).
#
# Lesson: a --force-with-lease push was rejected, `| tail -1` hid the rejection,
# the PR text then described a state that never reached the remote, and the old
# branch was merged. The agent said "pushed"; nothing checked it.
#
# What it checks: every `git [options] push` in the command (hookparse.py, the
# same parser as bash-hook.sh; heredoc bodies are data, redirections like `2>&1`
# are not arguments). The target is -C <dir> (after any `cd <dir>`), else the
# hook's cwd. `$VAR` paths are resolved from assignments in the same command.
# The remote is the one named, else the one git picks (pushRemote, pushDefault,
# the branch's remote, origin).
#
# Never a silent pass for a push it saw:
#   * path it cannot resolve, or capture failed (ls-remote error,
#     unknown remote)                     → finding "not proven / not provable"
#   * --dry-run/-n, --delete/-d, --tags, --all, --mirror, a delete refspec,
#     or a refspec for another branch than HEAD's → "skipped" line in the log
#
# Modes (GUARD_HOOK_MODE, else the first word of ~/.mc/guards/mode, else shadow):
#   shadow — findings and skips go to the log (GUARD_HOOK_LOG, default
#            ~/.mc/guards/hook-log.jsonl), exit 0.
#   block  — a finding: exit 2 with the reason on stderr. A PostToolUse hook cannot
#            undo the push; exit 2 puts the reason in front of the agent, so it
#            cannot report "pushed" without seeing that the remote disagrees.
#            Skips never block; they are logged.
# The hook never fails on its own error (missing python3, broken input): exit 0.
#
# Usage: as a hook: JSON on stdin.  Test: post-push-hook.sh <hook-json-file>
#        (fixture mode: the evidence is <fixture>.proof instead of a live
#        ls-remote — no .proof means the capture failed; exit 1 red, 0 green,
#        no log, no mode file).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
fixture=""; [ $# -gt 0 ] && fixture="$1"

work="$(mktemp -d "${TMPDIR:-/tmp}/guard-post.XXXXXX")" || exit 0   # scratch, removed on exit
trap 'rm -rf "$work"' EXIT
if [ -n "$fixture" ]; then cp "$fixture" "$work/in.json" || exit 2; else cat > "$work/in.json"; fi

# One line per push, fields split by \037 (not tab: read collapses empty tab fields):
# <status> <dir> <remote, empty = git picks> <refspecs> <reason>
# status: check | unresolved (a path the hook cannot know) | skipped (nothing to compare)
python3 - "$work" "$HERE" <<'PY' || exit 0
import json, os, sys
w, here = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)
import hookparse
try:
    d = json.load(open(os.path.join(w, "in.json")))
except Exception:
    sys.exit(1)
if d.get("tool_name", "Bash") != "Bash":
    sys.exit(1)
cmd = (d.get("tool_input") or {}).get("command") or ""
cwd = d.get("cwd") or os.getcwd()
def clean(x):
    return str(x).replace("\037", " ").replace("\n", " ")
rows = ["\037".join([p["status"], clean(p["dir"]), clean(p["remote"]),
                   clean(" ".join(p["refspecs"])), clean(p["reason"])])
        for p in hookparse.pushes(cmd, cwd)]
open(os.path.join(w, "pushes"), "w").write("\n".join(rows) + ("\n" if rows else ""))
PY

: > "$work/findings"; : > "$work/skipped"
n=0
# The remote git pushes to when none is named (git's own order).
default_remote() {
  local t="$1" b r k
  b="$(git -C "$t" symbolic-ref -q --short HEAD 2>/dev/null)"
  for k in ${b:+"branch.$b.pushRemote"} remote.pushDefault ${b:+"branch.$b.remote"}; do
    r="$(git -C "$t" config --get "$k" 2>/dev/null)" && [ -n "$r" ] && { printf '%s' "$r"; return; }
  done
  printf origin
}
# >>> guard-logic
while IFS="$(printf '\037')" read -r status t remote refspecs reason; do
  [ -n "$status" ] || continue
  case "$status" in
    skipped) printf '%s\n' "$reason" >> "$work/skipped"; continue ;;
    unresolved)
      printf 'proof-push: %s — NOT PROVEN. Prove it yourself: tools/guards/proof-push.sh -C <worktree>\n' "$reason" >> "$work/findings"
      continue ;;
  esac
  n=$((n + 1)); ev="$work/evidence.$n"; : > "$ev.err"
  if [ -n "$fixture" ]; then
    [ -n "$remote" ] || remote=origin
    cp "$fixture.proof" "$ev" 2>/dev/null || : > "$ev"
  else
    [ -n "$remote" ] || remote="$(default_remote "$t")"
    bash "$HERE/proof-push.sh" --capture -C "$t" --remote "$remote" > "$ev" 2> "$ev.err" || : > "$ev"
  fi
  if ! grep -q '^head=' "$ev"; then
    printf 'proof-push: %s (remote %s) — push not provable: HEAD or the remote branch could not be read (%s). Check it yourself: tools/guards/proof-push.sh -C <worktree>\n' \
      "$t" "$remote" "$(tr '\n' ' ' < "$ev.err" | cut -c1-160)" >> "$work/findings"
    continue
  fi
  branch="$(sed -n 's/^branch=//p' "$ev")"
  if [ -n "$refspecs" ]; then
    hit=0
    for r in $refspecs; do
      src="${r%%:*}"; dst="${r#*:}"            # no ':' means dst = src
      src="${src#refs/heads/}"; dst="${dst#refs/heads/}"
      case "$src" in HEAD|"$branch") ;; *) continue ;; esac
      case "$dst" in HEAD|"$branch") hit=1 ;; esac
    done
    if [ "$hit" != 1 ]; then   # another branch than HEAD's: nothing to compare
      printf 'push of %s from %s is not the branch HEAD is on (%s): not compared\n' "$refspecs" "$t" "$branch" >> "$work/skipped"
      continue
    fi
  fi
  if ! verdict="$(bash "$HERE/proof-push.sh" "$ev" 2>&1)"; then
    printf 'proof-push: %s (remote %s) — %s\n' "$t" "$remote" "$(printf '%s\n' "$verdict" | tr '\n' ' ' | sed 's/  */ /g')" >> "$work/findings"
  fi
done < "$work/pushes"
# <<< guard-logic

if [ -n "$fixture" ]; then
  if [ -s "$work/findings" ]; then cat "$work/findings"; echo "[post-push-hook] RED"; exit 1; fi
  echo "[post-push-hook] green"; exit 0
fi

[ -s "$work/findings" ] || [ -s "$work/skipped" ] || exit 0
mode="${GUARD_HOOK_MODE:-}"
[ -n "$mode" ] || mode="$(awk 'NR==1{print $1}' "$HOME/.mc/guards/mode" 2>/dev/null)"
[ -n "$mode" ] || mode=shadow

log="${GUARD_HOOK_LOG:-$HOME/.mc/guards/hook-log.jsonl}"
mkdir -p "$(dirname "$log")" 2>/dev/null
python3 - "$work" "$mode" >> "$log" 2>/dev/null <<'PY'
import datetime, json, os, re, sys
w, mode = sys.argv[1], sys.argv[2]
d = json.load(open(os.path.join(w, "in.json")))
cmd = (d.get("tool_input") or {}).get("command") or ""
excerpt = re.sub(r"[A-Za-z0-9_\-]{32,}", "<redacted>", cmd)[:240]   # no tokens in the log
print(json.dumps({"ts": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                  "hook": "post", "mode": mode, "session": d.get("session_id", ""),
                  "cwd": d.get("cwd", ""),
                  "findings": open(os.path.join(w, "findings")).read().splitlines(),
                  "skipped": open(os.path.join(w, "skipped")).read().splitlines(),
                  "command": excerpt}, ensure_ascii=False))
PY

if [ "$mode" = block ] && [ -s "$work/findings" ]; then
  {
    echo "GUARD: the push is NOT proven (tools/guards/post-push-hook.sh):"
    sed 's/^/  - /' "$work/findings"
    echo "Do not report this push as done. Read the push output in full, fix the cause, push again."
  } >&2
  exit 2
fi
exit 0
