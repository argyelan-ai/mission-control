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
#   - total budget (MC_TEST_GATE_BUDGET, default 270 s, below the 300 s hook
#     timeout) used up -> BLOCK with a hint (a killed hook would let it through)
#   - a test file the branch deletes is listed, not run
# What it is: early feedback before CI, NOT a replacement for CI. It runs the
# branch's changed test files plus the existing tests named after changed
# source files (backend/app/x.py -> test_x*.py); other broken tests and
# weakened asserts are CI's job.
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

# Only pushes and PR creations are gated. Git global options before the verb
# (`git -c k=v push`, `git --no-pager push`, `git -C dir push`) do not hide it.
target="$(printf '%s' "$cmd" | "$PY" -c '
import re, shlex, sys
c = sys.stdin.read()
OPT = r"(?:\s+(?:-[Cc]\s+(?:\"[^\"]*\"|\x27[^\x27]*\x27|\S+)|--(?:git-dir|work-tree|namespace|exec-path)\s+\S+|-\S+))*"
hit, target = False, ""
for m in re.finditer(r"(?:^|[^\w-])git(" + OPT + r")\s+push(?=[^\w-]|$)", c):
    hit = True
    opts = shlex.split(m.group(1)) if m.group(1).strip() else []
    if "-C" in opts and opts.index("-C") + 1 < len(opts):
        target = target or opts[opts.index("-C") + 1]
if re.search(r"(?:^|[^\w-])gh\s+pr\s+create(?=[^\w-]|$)", c):
    hit = True
if not hit:
    sys.exit(1)
if not target:
    m = re.search(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|\x27[^\x27]+\x27|\S+)\s*&&", c)
    if m:
        target = shlex.split(m.group(1))[0]
print(target)
')" || exit 0
[ -n "$target" ] || target="${hook_cwd:-$PWD}"
case "$target" in "~"*) target="$HOME${target#\~}";; esac
[ -d "$target" ] || { echo "test-gate: directory '$target' not found - gate skipped" >&2; exit 0; }

top="$(git -C "$target" rev-parse --show-toplevel 2>/dev/null)" || exit 0
git -C "$top" rev-parse --verify -q origin/main >/dev/null || {
  echo "test-gate: origin/main unknown in $top - gate skipped" >&2; exit 0; }
base="$(git -C "$top" merge-base origin/main HEAD 2>/dev/null)" || exit 0

changed="$(git -C "$top" diff --name-only --diff-filter=ACMR "$base" HEAD)"

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
# Total budget for ALL suites. It must stay below the hook timeout in the
# Claude Code settings (300 s): a hook that is killed by Claude Code does NOT
# block - so when the budget runs out we block ourselves, on purpose.
budget="${MC_TEST_GATE_BUDGET:-270}"
started="$(date +%s)"
run_limited() {  # run_limited <dir> <seconds> <cmd...>  — portable timeout (macOS has no `timeout`)
  local dir="$1" secs="$2"; shift 2
  "$PY" - "$dir" "$secs" "$@" <<'EOF'
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

red=0; ran=0; report=""; over_budget=0
suite() {  # suite <label> <dir> <cmd...>
  local label="$1" dir="$2" out rc left secs; shift 2
  left=$((budget - ($(date +%s) - started)))
  if [ "$left" -le 0 ]; then
    over_budget=$((over_budget + 1))
    report="$report
--- NOT RUN: $label (time budget of ${budget}s used up)"
    return
  fi
  secs=$limit; [ "$left" -lt "$secs" ] && secs=$left
  ran=$((ran + 1))
  out="$(run_limited "$dir" "$secs" "$@")"; rc=$?
  if [ $rc -eq 124 ] && [ "$secs" -lt "$limit" ]; then
    over_budget=$((over_budget + 1))
    report="$report
--- STOPPED: $label (time budget of ${budget}s used up)"
    return
  fi
  if [ $rc -ne 0 ]; then
    red=$((red + 1))
    report="$report
--- RED: $label (exit $rc)
$out"
  fi
}

# Tests the branch DELETED never run - say so, so a dropped red test is seen.
deleted="$(git -C "$top" diff --name-only --diff-filter=D "$base" HEAD |
  grep -E '(^|/)test_[^/]*\.(py|sh)$|\.test\.tsx?$' || true)"
if [ -n "$deleted" ]; then
  echo "test-gate: note - this branch deleted test file(s); they are not run:" >&2
  printf '%s\n' "$deleted" | sed 's/^/  /' >&2
fi
[ -n "$changed" ] || exit 0

# Changed source files pull in the tests that already cover them (by name),
# so breaking an EXISTING test is caught too, not only a changed one:
#   backend/app/**/foo.py          -> backend/tests/**/test_foo.py, test_foo_*.py
#   docker/omp-bridge/**/foo.py    -> docker/omp-bridge/tests/test_foo*.py
#   frontend-v2/**/Foo.tsx|ts      -> Foo.test.tsx|ts next to it or in __tests__/
tracked="$(git -C "$top" ls-files 'backend/tests' 'docker/omp-bridge/tests' 'frontend-v2/src')"
mapped="$(CHANGED="$changed" TRACKED="$tracked" "$PY" - <<'EOF'
import os, posixpath, re
tracked = os.environ["TRACKED"].splitlines()
tset = set(tracked)
out = []
for f in os.environ["CHANGED"].splitlines():
    stem = posixpath.splitext(posixpath.basename(f))[0]
    if stem in ("__init__", "conftest") or posixpath.basename(f).startswith("test_"):
        continue
    if f.startswith("backend/app/") and f.endswith(".py"):
        rx = re.compile(r"^backend/tests/(.*/)?test_" + re.escape(stem) + r"(_[^/]*)?\.py$")
        out += [t for t in tracked if rx.match(t)]
    elif f.startswith("docker/omp-bridge/") and f.endswith(".py") and "/tests/" not in f:
        rx = re.compile(r"^docker/omp-bridge/tests/test_" + re.escape(stem) + r"(_[^/]*)?\.py$")
        out += [t for t in tracked if rx.match(t)]
    elif f.startswith("frontend-v2/src/") and re.search(r"\.tsx?$", f) and ".test." not in f:
        d, ext = posixpath.dirname(f), f.rsplit(".", 1)[1]
        for e in ("ts", "tsx"):
            for cand in (f"{d}/{stem}.test.{e}", f"{d}/__tests__/{stem}.test.{e}"):
                if cand in tset:
                    out.append(cand)
print("\n".join(sorted(set(out))))
EOF
)"
changed="$(printf '%s\n%s\n' "$changed" "$mapped" | grep -v '^$' | sort -u)"

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

if [ $red -gt 0 ] || [ $over_budget -gt 0 ]; then
  {
    if [ $red -gt 0 ]; then
      echo "test-gate: BLOCKED - $red of $ran test suite(s) touched by this branch are red."
      echo "Fix the tests (or the code) before pushing / opening the PR."
    fi
    if [ $over_budget -gt 0 ]; then
      echo "test-gate: BLOCKED - time budget (${budget}s) used up, $over_budget suite(s) not finished."
      echo "Run them yourself, or push again with MC_TEST_GATE_BUDGET raised in the hook entry (keep it below the hook timeout)."
    fi
    printf '%s\n' "$report"
  } >&2
  exit 2
fi
[ $ran -gt 0 ] && echo "test-gate: ok - $ran test suite(s) touched by this branch green" >&2
exit 0
