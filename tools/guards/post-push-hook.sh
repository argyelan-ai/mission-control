#!/usr/bin/env bash
# post-push-hook.sh — the push proof at the place where the push happens: a Claude
# Code PostToolUse hook on the Bash tool. After a command that pushed, it checks
# that the remote branch really points at HEAD (proof-push.sh).
#
# Lesson: a --force-with-lease push was rejected, `| tail -1` hid the rejection,
# the PR text then described a state that never reached the remote, and the old
# branch was merged. The agent said "pushed"; nothing checked it.
#
# What it checks: every `git [-C <dir>] push` in the command (heredoc bodies are
# data and skipped). The target is -C <dir>, else the last `cd <dir>` before it,
# else the hook's cwd. Skipped, because there is nothing to compare: --dry-run/-n,
# --delete/-d, --tags, --all, --mirror, and a refspec that is not HEAD or the
# branch HEAD is on (pushing another branch says nothing about HEAD).
#
# Modes (GUARD_HOOK_MODE, else the first word of ~/.mc/guards/mode, else shadow):
#   shadow — findings go to the log (GUARD_HOOK_LOG, default
#            ~/.mc/guards/hook-log.jsonl), exit 0.
#   block  — exit 2 with the reason on stderr. A PostToolUse hook cannot undo the
#            push; exit 2 puts the reason in front of the agent, so it cannot
#            report "pushed" without seeing that the remote disagrees.
# The hook never fails on its own error (missing python3, broken input): exit 0.
#
# Usage: as a hook: JSON on stdin.  Test: post-push-hook.sh <hook-json-file>
#        (fixture mode: the evidence is <fixture>.proof instead of a live
#        ls-remote; exit 1 red, 0 green, no log, no mode file).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
fixture=""; [ $# -gt 0 ] && fixture="$1"

work="$(mktemp -d "${TMPDIR:-/tmp}/guard-post.XXXXXX")" || exit 0   # guard-allow: tmp-previews (scratch, removed on exit)
trap 'rm -rf "$work"' EXIT
if [ -n "$fixture" ]; then cp "$fixture" "$work/in.json" || exit 2; else cat > "$work/in.json"; fi

# One line per push: <dir>\t<refspecs separated by spaces, may be empty>
python3 - "$work" <<'PY' || exit 0
import json, os, re, shlex, sys
w = sys.argv[1]
try:
    d = json.load(open(os.path.join(w, "in.json")))
except Exception:
    sys.exit(1)
if d.get("tool_name", "Bash") != "Bash":
    sys.exit(1)
cmd = (d.get("tool_input") or {}).get("command") or ""
cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", "<<HEREDOC-STRIPPED", cmd, flags=re.S)
cwd = d.get("cwd") or os.getcwd()
SKIP = {"--dry-run", "-n", "--delete", "-d", "--tags", "--all", "--mirror", "--prune"}
WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
out = []
for m in re.finditer(r"\bgit\s+(?:-C\s+(\S+)\s+)?(?:-\S+\s+)*push\b([^;&|<>\n]*)", cmd):
    t = m.group(1)
    if t is None:
        cds = re.findall(r"(?:^|&&|;|\n)\s*cd\s+(\S+)", cmd[:m.start()])
        t = cds[-1] if cds else cwd
    t = os.path.expanduser(t.strip("'\""))
    if "$" in t:
        continue  # a shell variable: the push-gate (PreToolUse) already flags it
    t = t if os.path.isabs(t) else os.path.join(cwd, t)
    try:
        words = shlex.split(m.group(2))
    except ValueError:
        words = m.group(2).split()
    if any(x in SKIP or x.startswith("--delete") for x in words):
        continue
    pos, skip_next = [], False
    for x in words:
        if skip_next:
            skip_next = False
            continue
        if x in WITH_VALUE:
            skip_next = True
            continue
        if x.startswith("-"):
            continue
        pos.append(x)
    refspecs = [r.lstrip("+") for r in pos[1:]]
    if any(r.startswith(":") for r in refspecs):
        continue  # deletes a remote branch
    remote = pos[0] if pos else "origin"
    out.append(f"{t}\t{remote}\t{' '.join(refspecs)}")
open(os.path.join(w, "pushes"), "w").write("\n".join(out) + ("\n" if out else ""))
PY

: > "$work/findings"
n=0
# >>> guard-logic
while IFS="$(printf '\t')" read -r t remote refspecs; do
  [ -n "$t" ] || continue
  n=$((n + 1)); ev="$work/evidence.$n"
  if [ -n "$fixture" ]; then
    cp "$fixture.proof" "$ev" 2>/dev/null || continue
  else
    bash "$HERE/proof-push.sh" --capture -C "$t" --remote "$remote" > "$ev" 2>/dev/null || continue
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
    [ "$hit" = 1 ] || continue   # pushed another branch than HEAD's: nothing to compare
  fi
  if ! verdict="$(bash "$HERE/proof-push.sh" "$ev" 2>&1)"; then
    printf 'proof-push: %s — %s\n' "$t" "$(printf '%s\n' "$verdict" | tr '\n' ' ' | sed 's/  */ /g')" >> "$work/findings"
  fi
done < "$work/pushes"
# <<< guard-logic

if [ -n "$fixture" ]; then
  if [ -s "$work/findings" ]; then cat "$work/findings"; echo "[post-push-hook] RED"; exit 1; fi
  echo "[post-push-hook] green"; exit 0
fi

[ -s "$work/findings" ] || exit 0
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
                  "command": excerpt}, ensure_ascii=False))
PY

if [ "$mode" = block ]; then
  {
    echo "GUARD: the push is NOT proven (tools/guards/post-push-hook.sh):"
    sed 's/^/  - /' "$work/findings"
    echo "Do not report this push as done. Read the push output in full, fix the cause, push again."
  } >&2
  exit 2
fi
exit 0
