#!/usr/bin/env bash
# bash-hook.sh — the guards at the place where the mistakes happen: a Claude Code
# PreToolUse hook on the Bash tool. It checks the command an agent is about to run.
#
# Lessons it covers (see the guard each one delegates to):
#  * push-gate       — cut or hidden push output, bare `git push`, --no-verify,
#                      force without lease, push through a variable.
#  * privacy scan    — for a push into a checkout that has scripts/privacy-scan.py,
#                      the hook RUNS the scan there. It does not trust the text.
#  * zsh-set-split   — `set -- $var` in the agent's zsh.
#  * background-tests — a test run with the tool flag run_in_background, or sent
#                      to the background with '&' and no wait.
#
# Heredoc bodies are data (commit messages, PR texts) and are not checked.
#
# Modes (GUARD_HOOK_MODE, else the first word of ~/.mc/guards/mode, else shadow):
#   shadow — never blocks. Findings go to the log (GUARD_HOOK_LOG, default
#            ~/.mc/guards/hook-log.jsonl), one JSON line each. Exit 0.
#   block  — a finding blocks the call: exit 2, reason on stderr (Claude sees it).
# The hook never blocks on its own failure (missing python3, broken input): exit 0.
#
# Usage: as a hook: JSON on stdin.  Test: bash-hook.sh <hook-json-file>
#        (fixture mode: exit 1 red, 0 green, no log, no mode file).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
fixture=""; [ $# -gt 0 ] && fixture="$1"

work="$(mktemp -d "${TMPDIR:-/tmp}/guard-hook.XXXXXX")" || exit 0   # guard-allow: tmp-previews (scratch, removed on exit)
trap 'rm -rf "$work"' EXIT
if [ -n "$fixture" ]; then cp "$fixture" "$work/in.json" || exit 2; else cat > "$work/in.json"; fi

# Split the hook JSON into: the command (heredoc bodies stripped), the flag, the cwd,
# and the push targets (-C <dir>, else the last `cd <dir>` before the push, else cwd).
python3 - "$work" <<'PY' || exit 0
import json, os, re, sys
w = sys.argv[1]
try:
    d = json.load(open(os.path.join(w, "in.json")))
except Exception:
    sys.exit(1)
if d.get("tool_name", "Bash") != "Bash":
    sys.exit(1)
ti = d.get("tool_input") or {}
cmd = ti.get("command") or ""
cmd = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", "<<HEREDOC-STRIPPED", cmd, flags=re.S)
cwd = d.get("cwd") or os.getcwd()
open(os.path.join(w, "cmd"), "w").write(cmd + "\n")
open(os.path.join(w, "bg"), "w").write("1" if ti.get("run_in_background") else "0")
targets = []
for m in re.finditer(r"\bgit\s+(?:-C\s+(\S+)\s+)?(?:-\S+\s+)*push\b", cmd):
    t = m.group(1)
    if t is None:
        cds = re.findall(r"(?:^|&&|;|\n)\s*cd\s+(\S+)", cmd[:m.start()])
        t = cds[-1] if cds else cwd
    t = os.path.expanduser(t.strip("'\""))
    if "$" in t:
        continue  # a shell variable: cannot be resolved here
    if not os.path.isabs(t):
        t = os.path.join(cwd, t)
    if t not in targets:
        targets.append(t)
open(os.path.join(w, "targets"), "w").write("\n".join(targets) + ("\n" if targets else ""))
PY

RE_TEST='(pytest|vitest|jest|npm[[:space:]]+(run[[:space:]]+)?test|go[[:space:]]+test|cargo[[:space:]]+test)'
: > "$work/findings"
add() { printf '%s\n' "$1" >> "$work/findings"; }

# >>> guard-logic
PUSH_GATE_SCAN=0 bash "$HERE/push-gate.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[push-gate\] /push-gate: /p' >> "$work/findings"
bash "$HERE/zsh-set-split.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[zsh-set-split\] /zsh-set-split: /p' >> "$work/findings"
bash "$HERE/background-tests.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[background-tests\] /background-tests: /p' >> "$work/findings"
if [ "$(cat "$work/bg")" = 1 ] && grep -Eq "$RE_TEST" "$work/cmd"; then
  add "background-tests: test run with run_in_background — run it in the foreground and commit before the report"
fi
while IFS= read -r t; do
  [ "${GUARD_HOOK_NO_SCAN:-0}" = 1 ] && break   # replays of old commands only (the hook process env, not the command's)
  [ -n "$t" ] && [ -f "$t/scripts/privacy-scan.py" ] || continue
  if ! out="$(cd "$t" && python3 scripts/privacy-scan.py 2>&1)"; then
    add "privacy-scan: red in $t — fix it before the push: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  fi
done < "$work/targets"
# <<< guard-logic

if [ -n "$fixture" ]; then
  if [ -s "$work/findings" ]; then cat "$work/findings"; echo "[bash-hook] RED"; exit 1; fi
  echo "[bash-hook] green"; exit 0
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
                  "mode": mode, "session": d.get("session_id", ""), "cwd": d.get("cwd", ""),
                  "findings": open(os.path.join(w, "findings")).read().splitlines(),
                  "command": excerpt}, ensure_ascii=False))
PY

if [ "$mode" = block ]; then
  {
    echo "GUARD blocked this command (tools/guards, lessons as code):"
    sed 's/^/  - /' "$work/findings"
    echo "Fix the command, do not work around the guard."
  } >&2
  exit 2
fi
exit 0
