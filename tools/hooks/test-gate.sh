#!/usr/bin/env bash
# test-gate — Claude Code PreToolUse hook (matcher: Bash).
#
# Blocks `git push` / `gh pr create` when a test file that the branch adds or
# changes (against origin/main) is red. "A failing test first, then the
# change" (AGENTS.md) only means something if the test is green when the
# branch leaves the machine.
#
# Red case (exit 2 = Claude Code blocks the tool call, stderr goes to the
# agent): one of the branch's changed test files fails.
#   backend/tests/**/test_*.py          -> pytest   (cwd backend/)
#   docker/omp-bridge/tests/test_*.py   -> pytest   (cwd docker/omp-bridge/)
#   scripts/mc-cli/tests/test_*.py      -> pytest   (cwd repo root)
#   tools/**/tests/test_*.py            -> pytest   (cwd repo root)
#   frontend-v2/**/*.test.ts(x)         -> vitest   (cwd frontend-v2/)
#   scripts/tests/test_*.sh             -> bash     (cwd repo root; other
#                                          shell tests need args/images)
# Green: no changed test file, or all of them pass.
# Honest limits (warn on stderr, never block):
#   - no runner available (no pytest / no node_modules) -> that suite is skipped
#   - origin/main unknown in that repo                 -> whole gate skipped
#   - per-suite timeout (MC_TEST_GATE_TIMEOUT, default 240 s) -> counts as red
# Sabotage probe: tools/hooks/tests/test_test_gate.py (a broken test -> exit 2).
#
# Input: the hook JSON on stdin ({"tool_input":{"command":...},"cwd":...}).
set -u

PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "test-gate: python3 missing - gate skipped" >&2; exit 0; }

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

# Only pushes and PR creations are gated.
printf '%s' "$cmd" | grep -Eq '(^|[^[:alnum:]_-])(git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+push|gh[[:space:]]+pr[[:space:]]+create)([^[:alnum:]_-]|$)' || exit 0

# Which checkout? `git -C <dir> push`, else `cd <dir> && ...`, else the hook cwd.
target="$(printf '%s' "$cmd" | "$PY" -c '
import re, shlex, sys
c = sys.stdin.read()
m = re.search(r"git\s+-C\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s+push", c)
if not m:
    m = re.search(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s*&&", c)
if m:
    print(shlex.split(m.group(1))[0])
')"
[ -n "$target" ] || target="${hook_cwd:-$PWD}"
case "$target" in "~"*) target="$HOME${target#\~}";; esac
[ -d "$target" ] || { echo "test-gate: directory '$target' not found - gate skipped" >&2; exit 0; }

top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0
git -C "$top" rev-parse --verify -q origin/main >/dev/null || {
  echo "test-gate: origin/main unknown in $top - gate skipped" >&2; exit 0; }
base="$(git -C "$top" merge-base origin/main HEAD 2>/dev/null)" || exit 0

changed="$(git -C "$top" diff --name-only --diff-filter=ACMR "$base" HEAD)"
[ -n "$changed" ] || exit 0

# Shared checkout (worktrees have no venv/node_modules of their own). The
# shared venv's editable install points at the shared checkout's backend, so
# backend suites get PYTHONPATH=<this checkout>/backend - the branch's code.
common="$(cd "$top" && cd "$(git rev-parse --git-common-dir)/.." && pwd)"

pick_python() {
  local c
  for c in "$top/backend/.venv/bin/python" "$common/backend/.venv/bin/python" "${MC_TEST_GATE_PYTHON:-}" python3; do
    [ -n "$c" ] || continue
    if "$c" -c 'import pytest' >/dev/null 2>&1; then echo "$c"; return 0; fi
  done
  return 1
}

limit="${MC_TEST_GATE_TIMEOUT:-240}"
run_limited() {  # run_limited <dir> <cmd...>  — portable timeout (macOS has no `timeout`)
  local dir="$1"; shift
  "$PY" - "$dir" "$limit" "$@" <<'EOF'
import subprocess, sys
d, limit, argv = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
try:
    p = subprocess.run(argv, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=limit)
except subprocess.TimeoutExpired:
    print(f"timeout after {limit}s"); sys.exit(124)
out = p.stdout.decode(errors="replace").splitlines()
print("\n".join(out[-25:]))
sys.exit(p.returncode)
EOF
}

red=0; ran=0; report=""
suite() {  # suite <label> <dir> <cmd...>
  local label="$1" dir="$2" out rc; shift 2
  ran=$((ran + 1))
  out="$(run_limited "$dir" "$@")"; rc=$?
  if [ $rc -ne 0 ]; then
    red=$((red + 1))
    report="$report
--- RED: $label (exit $rc)
$out"
  fi
}

be=(); ob=(); rootpy=(); fe=(); sh_tests=()
while IFS= read -r f; do
  case "$f" in
    backend/tests/*test_*.py) be+=("${f#backend/}") ;;
    docker/omp-bridge/tests/test_*.py) ob+=("${f#docker/omp-bridge/}") ;;
    scripts/mc-cli/tests/test_*.py|tools/*/tests/test_*.py) rootpy+=("$f") ;;
    frontend-v2/*.test.ts|frontend-v2/*.test.tsx) fe+=("${f#frontend-v2/}") ;;
    scripts/tests/test_*.sh) sh_tests+=("$f") ;;
  esac
done <<< "$changed"

if [ ${#be[@]} -gt 0 ] || [ ${#ob[@]} -gt 0 ] || [ ${#rootpy[@]} -gt 0 ]; then
  if pyt="$(pick_python)"; then
    [ ${#be[@]} -gt 0 ] && suite "backend pytest" "$top/backend" env PYTHONPATH="$top/backend" "$pyt" -m pytest -q -x -p no:cacheprovider "${be[@]}"
    [ ${#ob[@]} -gt 0 ] && suite "omp-bridge pytest" "$top/docker/omp-bridge" env PYTHONPATH="$top/backend" "$pyt" -m pytest -q -x -p no:cacheprovider "${ob[@]}"
    [ ${#rootpy[@]} -gt 0 ] && suite "pytest" "$top" "$pyt" -m pytest -q -x -p no:cacheprovider "${rootpy[@]}"
  else
    echo "test-gate: no python with pytest found - python tests NOT run" >&2
  fi
fi
if [ ${#fe[@]} -gt 0 ]; then
  if [ -x "$top/frontend-v2/node_modules/.bin/vitest" ]; then
    suite "frontend vitest" "$top/frontend-v2" "$top/frontend-v2/node_modules/.bin/vitest" run "${fe[@]}"
  else
    echo "test-gate: frontend-v2/node_modules missing in $top - vitest NOT run (${#fe[@]} file(s))" >&2
  fi
fi
for t in "${sh_tests[@]+"${sh_tests[@]}"}"; do
  suite "bash $t" "$top" bash "$t"
done

if [ $red -gt 0 ]; then
  {
    echo "test-gate: BLOCKED - $red of $ran test suite(s) changed on this branch are red."
    echo "Fix the tests (or the code) before pushing / opening the PR.$report"
  } >&2
  exit 2
fi
[ $ran -gt 0 ] && echo "test-gate: ok - $ran changed test suite(s) green" >&2
exit 0
