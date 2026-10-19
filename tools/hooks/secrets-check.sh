#!/usr/bin/env bash
# secrets-check — Claude Code PreToolUse hook (matcher: Bash).
#
# Blocks `git commit` and `git push` when the lines they would add contain a
# credential, or when a secret-bearing file is staged. The last line of
# defence is CI's leak gate - but by then the secret is already on GitHub.
# This hook stops it on the machine.
#
# Red case (exit 2 = Claude Code blocks the tool call; stderr names the file
# and the kind of secret, never the value):
#   git commit -> added lines of the STAGED diff
#   git push   -> added lines of the branch diff against origin/main
#   matches: private key block, GitHub token (ghp_/gho_/ghs_/github_pat_),
#            Anthropic key (sk-ant-), OpenAI-style key (sk-/sk-proj-, 40+),
#            AWS access key id (AKIA...), Slack token (xox?-), Hugging Face
#            token (hf_...), or a staged .env / *.pem / *.p12 / id_rsa* file
#            (.env.example and other *.example are fine).
# Green: nothing found, or the command is neither a commit nor a push.
# Honest limits: pattern list, not entropy analysis (CI's gitleaks is the
# broad scan). A push from a repo without origin/main is not scanned (warns).
# Sabotage probe: tools/hooks/tests/test_secrets_check.py.
set -u

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "secrets-check: python3 missing - check skipped" >&2; exit 0; }

payload="$(cat)"
read_field() {
  printf '%s' "$payload" | "$PY" -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
key = sys.argv[1]
v = d.get("tool_input", {}).get("command", "") if key == "command" else d.get(key, "")
print(v or "")
' "$1"
}
cmd="$(read_field command)"
hook_cwd="$(read_field cwd)"

verb_re='(^|[^[:alnum:]_-])git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+'
mode=""
printf '%s' "$cmd" | grep -Eq "${verb_re}commit([^[:alnum:]_-]|\$)" && mode="commit"
printf '%s' "$cmd" | grep -Eq "${verb_re}push([^[:alnum:]_-]|\$)" && mode="${mode:+$mode+}push"
[ -n "$mode" ] || exit 0

target="$(printf '%s' "$cmd" | "$PY" -c '
import re, shlex, sys
c = sys.stdin.read()
m = re.search(r"git\s+-C\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s+(?:commit|push)", c)
if not m:
    m = re.search(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s*&&", c)
if m:
    print(shlex.split(m.group(1))[0])
')"
[ -n "$target" ] || target="${hook_cwd:-$PWD}"
case "$target" in "~"*) target="$HOME${target#\~}";; esac
[ -d "$target" ] || exit 0
top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0

diffs=""
files=""
case "$mode" in *commit*)
  diffs="$(git -C "$top" diff --cached -U0 --no-color)"
  files="$(git -C "$top" diff --cached --name-only --diff-filter=ACMR)" ;;
esac
case "$mode" in *push*)
  if base="$(git -C "$top" merge-base origin/main HEAD 2>/dev/null)"; then
    diffs="$diffs
$(git -C "$top" diff -U0 --no-color "$base" HEAD)"
    files="$files
$(git -C "$top" diff --name-only --diff-filter=ACMR "$base" HEAD)"
  else
    echo "secrets-check: origin/main unknown in $top - push diff not scanned" >&2
  fi ;;
esac

findings="$(DIFFS="$diffs" FILES="$files" "$PY" - <<'EOF'
import os, re
rules = [
    ("private key block", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}"),
]
out, current = [], "?"
for line in os.environ.get("DIFFS", "").splitlines():
    if line.startswith("+++ "):
        current = line[6:] if line.startswith("+++ b/") else line[4:]
        continue
    if not line.startswith("+") or line.startswith("+++"):
        continue
    for name, rx in rules:
        if re.search(rx, line[1:]):
            out.append(f"{current}: {name}")
bad_file = re.compile(r"(^|/)(\.env(\.[A-Za-z0-9_-]+)?|[^/]+\.pem|[^/]+\.p12|id_rsa[^/]*)$")
for f in os.environ.get("FILES", "").splitlines():
    f = f.strip()
    if f and bad_file.search(f) and not f.endswith(".example") and not f.endswith(".pub"):
        out.append(f"{f}: secret-bearing file")
print("\n".join(sorted(set(out))))
EOF
)"

if [ -n "$findings" ]; then
  {
    echo "secrets-check: BLOCKED ($mode) - possible secret in what this would publish:"
    printf '%s\n' "$findings" | sed 's/^/  /'
    echo "Remove it (and rotate it if it was real). Values are not printed here."
  } >&2
  exit 2
fi
exit 0
