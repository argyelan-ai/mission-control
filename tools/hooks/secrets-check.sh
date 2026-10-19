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

# Parse the command once: which verbs, which checkout, does the commit take
# unstaged work along (`commit -a/--all`, or a `git add` in the same line -
# the hook runs BEFORE the command, so nothing of that is staged yet)?
# Git global options before the verb (`git -c k=v`, `--no-pager`, `-C dir`)
# do not hide the verb.
parsed="$(printf '%s' "$cmd" | "$PY" -c '
import re, shlex, sys
c = sys.stdin.read()
OPT = r"(?:\s+(?:-[Cc]\s+(?:\"[^\"]*\"|\x27[^\x27]*\x27|\S+)|--(?:git-dir|work-tree|namespace|exec-path)\s+\S+|-\S+))*"
mode, target, all_, add = [], "", "0", "0"
for m in re.finditer(r"(?:^|[^\w-])git(" + OPT + r")\s+(commit|push|add)(?=[^\w-]|$)", c):
    verb = m.group(2)
    opts = shlex.split(m.group(1)) if m.group(1).strip() else []
    if "-C" in opts and opts.index("-C") + 1 < len(opts) and not target:
        target = opts[opts.index("-C") + 1]
    if verb == "add":
        add = "1"
        continue
    if verb not in mode:
        mode.append(verb)
    if verb == "commit":
        rest = re.split(r"[;&|]", c[m.end():], 1)[0]
        try:
            args = shlex.split(rest)
        except ValueError:
            args = rest.split()
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a in ("-m", "-F", "-c", "-C", "--message", "--file", "--author", "--date"):
                skip = True  # next token is a value, not a flag
                continue
            if a == "--all" or re.fullmatch(r"-[b-zA-Z]*a[A-Za-z]*", a):
                all_ = "1"
if not target:
    m = re.search(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s*&&", c)
    if m:
        target = shlex.split(m.group(1))[0]
print("+".join(sorted(mode)))
print(target)
print(all_ if "commit" in mode else "0")
print(add if "commit" in mode else "0")
')"
mode="$(printf '%s\n' "$parsed" | sed -n 1p)"
target="$(printf '%s\n' "$parsed" | sed -n 2p)"
take_all="$(printf '%s\n' "$parsed" | sed -n 3p)"
take_add="$(printf '%s\n' "$parsed" | sed -n 4p)"
[ -n "$mode" ] || exit 0

[ -n "$target" ] || target="${hook_cwd:-$PWD}"
case "$target" in "~"*) target="$HOME${target#\~}";; esac
[ -d "$target" ] || exit 0
top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0

diffs=""
files=""
untracked=""
case "$mode" in *commit*)
  if [ "$take_all" = 1 ] || [ "$take_add" = 1 ]; then
    # Staged + unstaged changes of tracked files (what -a / git add takes).
    if git -C "$top" rev-parse --verify -q HEAD >/dev/null; then
      diffs="$(git -C "$top" diff HEAD -U0 --no-color)"
      files="$(git -C "$top" diff HEAD --name-only --diff-filter=ACMR)"
    else
      diffs="$(git -C "$top" diff --cached -U0 --no-color)"
      files="$(git -C "$top" diff --cached --name-only --diff-filter=ACMR)"
    fi
    # `git add` also picks up new files; -a alone does not.
    [ "$take_add" = 1 ] && untracked="$(git -C "$top" ls-files --others --exclude-standard)"
  else
    diffs="$(git -C "$top" diff --cached -U0 --no-color)"
    files="$(git -C "$top" diff --cached --name-only --diff-filter=ACMR)"
  fi ;;
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

findings="$(DIFFS="$diffs" FILES="$files" UNTRACKED="$untracked" TOP="$top" "$PY" - <<'EOF'
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
# A value that says it is not real (test fixtures: "xoxb-TEST-token").
PLACEHOLDER = re.compile(r"test|dummy|fake|example|placeholder|not-a-real|xxxxxxxx", re.I)
ALLOW = re.compile(r"secrets-check:\s*allow\b(.*)", re.I)
REASON = re.compile(r"^\s*[-—–:]+\s*\S")
out, current = [], "?"


def scan(path, text):
    for line in text.splitlines():
        for name, rx in rules:
            m = re.search(rx, line)
            if not m or PLACEHOLDER.search(m.group(0)):
                continue
            a = ALLOW.search(line)
            if a and REASON.match(a.group(1)):
                continue
            if a:
                out.append(f"{path}: {name} (allow marker needs a reason: '# secrets-check: allow - <reason>')")
            else:
                out.append(f"{path}: {name}")


added = {}
for line in os.environ.get("DIFFS", "").splitlines():
    if line.startswith("+++ "):
        current = line[6:] if line.startswith("+++ b/") else line[4:]
        continue
    if line.startswith("+") and not line.startswith("+++"):
        added.setdefault(current, []).append(line[1:])
for path, lines in added.items():
    scan(path, "\n".join(lines))
top = os.environ.get("TOP", ".")
for f in os.environ.get("UNTRACKED", "").splitlines():
    p = os.path.join(top, f)
    try:
        if os.path.getsize(p) > 2_000_000:
            continue
        with open(p, "rb") as fh:
            data = fh.read()
    except OSError:
        continue
    if b"\0" in data[:8192]:
        continue
    scan(f, data.decode("utf-8", "replace"))
bad_file = re.compile(r"(^|/)(\.env(\.[A-Za-z0-9_-]+)?|[^/]+\.pem|[^/]+\.p12|id_rsa[^/]*)$")
for f in (os.environ.get("FILES", "") + "\n" + os.environ.get("UNTRACKED", "")).splitlines():
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
