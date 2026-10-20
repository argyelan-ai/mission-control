#!/usr/bin/env bash
# bash-hook.sh — the guards at the place where the mistakes happen: a Claude Code
# PreToolUse hook on the Bash tool. It checks the command an agent is about to run.
#
# Lessons it covers (see the guard each one delegates to):
#  * push-gate       — cut or hidden push output, bare `git push`, --no-verify,
#                      force without lease, push through a variable.
#  * privacy scan    — for a push into a checkout that has scripts/privacy-scan.py,
#                      the hook RUNS the scan there. It does not trust the text.
#  * gate-exit       — a gate (preflight, privacy scan, ...) whose exit code is
#                      dropped by '| tail', ';' or '|| true' before the next step.
#  * zsh-set-split   — `set -- $var` in the agent's zsh.
#  * background-tests — a test run with the tool flag run_in_background, or sent
#                      to the background with '&' and no wait.
#  * tmp-previews    — a preview, deploy folder, handoff or screenshot written
#                      under /tmp (macOS clears it after a few days).
#  * uncommitted-work — `git checkout <ref|file>`, `switch`, `restore`, `reset --hard`,
#                      `clean -f`, `stash drop` in a tree with uncommitted changes
#                      (the hook reads `git status` of that tree; read-only).
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
# the push targets and the trees of commands that can throw work away. Pushes and
# git calls are read by hookparse.py — the same parser as post-push-hook.sh.
python3 - "$work" "$HERE" <<'PY' || exit 0
import json, os, re, sys
w, here = sys.argv[1], sys.argv[2]
sys.path.insert(0, here)
import hookparse
try:
    d = json.load(open(os.path.join(w, "in.json")))
except Exception:
    sys.exit(1)
if d.get("tool_name", "Bash") != "Bash":
    sys.exit(1)
ti = d.get("tool_input") or {}
cmd = hookparse.strip_heredocs(ti.get("command") or "")
cwd = d.get("cwd") or os.getcwd()
open(os.path.join(w, "cmd"), "w").write(cmd + "\n")
open(os.path.join(w, "bg"), "w").write("1" if ti.get("run_in_background") else "0")

def write(name, rows):
    out = []
    for r in rows:
        if r not in out:
            out.append(r)
    open(os.path.join(w, name), "w").write("\n".join(out) + ("\n" if out else ""))

targets, unresolved = [], []
for m in hookparse.PUSH_RE.finditer(cmd):
    t, raw = hookparse.target_dir(cmd, m, cwd)
    if t is None:
        unresolved.append(raw)
    else:
        targets.append(t)
write("targets", targets)
write("unresolved", unresolved)

# Commands that throw away uncommitted work: checkout/switch to another state,
# restore, reset --hard, clean -f, stash drop/clear. Creating a branch keeps it.
WIPE = hookparse.git_re("checkout|switch|restore|reset|clean|stash")
def wipes(verb, rest):
    if verb in ("checkout", "switch"):
        return not re.search(r"(^|\s)(-b|-B|-c|-C|--orphan|--detach\s+-b)\s", rest + " ") and rest.strip() != ""
    if verb == "restore":
        return "--staged" not in rest or "--worktree" in rest
    if verb == "reset":
        return "--hard" in rest
    if verb == "clean":
        return re.search(r"(^|\s)-[a-zA-Z]*f", rest) is not None
    if verb == "stash":
        return re.search(r"^\s*(drop|clear)\b", rest) is not None
    return False
rows = []
for m in WIPE.finditer(cmd):
    verb = m.group(2)
    rest = re.split(r"[;&|\n]", cmd[m.end():], maxsplit=1)[0]
    if not wipes(verb, rest):
        continue
    t, _ = hookparse.target_dir(cmd, m, cwd)
    if t:
        rows.append(f"{verb}\t{t}")
write("wipe_targets", rows)
PY

RE_TEST='(pytest|vitest|jest|npm[[:space:]]+(run[[:space:]]+)?test|go[[:space:]]+test|cargo[[:space:]]+test)'
: > "$work/findings"
add() { printf '%s\n' "$1" >> "$work/findings"; }

# A "guard-allow: <name>" comment exempts a line in a repo file (reviewed in a PR).
# In a live command it would be the agent exempting itself, so the hook ignores it.
export GUARD_ALLOW_DISABLED=1
# >>> guard-logic
PUSH_GATE_SCAN=0 bash "$HERE/push-gate.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[push-gate\] /push-gate: /p' >> "$work/findings"
bash "$HERE/gate-exit.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[gate-exit\] /gate-exit: /p' >> "$work/findings"
bash "$HERE/zsh-set-split.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[zsh-set-split\] /zsh-set-split: /p' >> "$work/findings"
bash "$HERE/background-tests.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[background-tests\] /background-tests: /p' >> "$work/findings"
bash "$HERE/tmp-previews.sh" "$work/cmd" | sed -n 's/^.*:[0-9][0-9]*: \[tmp-previews\] /tmp-previews: /p' >> "$work/findings"
if [ "$(cat "$work/bg")" = 1 ] && grep -Eq "$RE_TEST" "$work/cmd"; then
  add "background-tests: test run with run_in_background — run it in the foreground and commit before the report"
fi
while IFS= read -r raw; do
  [ -n "$raw" ] && add "privacy-scan: push target not resolvable ($raw) — the hook cannot run the scan there. Write the path out: git -C <worktree> push ..."
done < "$work/unresolved"
while IFS= read -r t; do
  [ "${GUARD_HOOK_NO_SCAN:-0}" = 1 ] && break   # replays of old commands only (the hook process env, not the command's)
  [ -n "$t" ] && [ -f "$t/scripts/privacy-scan.py" ] || continue
  if ! out="$(cd "$t" && python3 scripts/privacy-scan.py 2>&1)"; then
    add "privacy-scan: red in $t — fix it before the push: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')"
  fi
done < "$work/targets"
while IFS="$(printf '\t')" read -r verb t; do
  [ -n "$t" ] || continue
  # Fixtures carry the tree state as captured evidence (<fixture>.status).
  if [ -n "$fixture" ]; then
    st="$(cat "$fixture.status" 2>/dev/null)"
  else
    st="$(git -C "$t" status --porcelain 2>/dev/null)" || continue
  fi
  # Untracked files survive everything except `git clean`.
  [ "$verb" = clean ] || st="$(printf '%s\n' "$st" | grep -v '^??')"
  if [ -n "$st" ]; then
    add "uncommitted-work: $t has uncommitted changes ($(printf '%s\n' "$st" | wc -l | tr -d ' ') files) — this command can throw them away silently. Commit or WIP-commit first"
  fi
done < "$work/wipe_targets"
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
