#!/usr/bin/env python3
"""check-registration: do the Claude Code settings really run the hooks?

test-gate.sh and secrets-check.sh only protect a session whose settings
register them. A hook that is not registered, points at a checkout that sits
on some feature branch, or sits behind a narrow pre-filter (`case "$p" in
*push*)` misses `gh pr new`, git aliases and script files) looks like
protection and is none. This script reads the settings files a session loads
and says what is missing.

Usage:
  check-registration.py [--repo DIR] SETTINGS.json [SETTINGS.json ...]
      Red (exit 1) when, across the given files (they are merged, as Claude
      Code merges user + project settings):
        - a hook is not registered as PreToolUse / Bash at all
        - it runs a file from a checkout instead of `origin/main`
        - a pre-filter narrows which commands reach it
        - test-gate's timeout is below 300 s (its own budget is 270 s; a hook
          killed by Claude Code does NOT block)
  check-registration.py --print --repo DIR
      Prints the canonical PreToolUse entry (JSON) to paste into
      ~/.claude/settings.json - user level, so every session and every
      worktree gets it.

It never writes settings itself: changing them is the operator's decision.
Sabotage probe: tools/hooks/tests/test_check_registration.py.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HOOKS = {"test-gate.sh": 300, "secrets-check.sh": 30}


def canonical(repo):
    entries = []
    for name, timeout in HOOKS.items():
        cmd = ("bash -c 'f=$(mktemp) || exit 0; "
               f"git -C {repo} show origin/main:tools/hooks/{name} > \"$f\" || {{ rm -f \"$f\"; exit 0; }}; "
               f"MC_HOOK_REPO={repo} bash \"$f\"; rc=$?; rm -f \"$f\"; exit $rc'")
        entries.append({"type": "command", "command": cmd, "timeout": timeout})
    return {"PreToolUse": [{"matcher": "Bash", "hooks": entries}]}


def bash_hooks(settings):
    out = []
    for group in (settings.get("hooks") or {}).get("PreToolUse") or []:
        matcher = group.get("matcher", "")
        if matcher and not re.search(r"(^|\|)Bash(\||$)|^\*$|^\.\*$", matcher):
            continue
        for h in group.get("hooks") or []:
            if h.get("type") == "command":
                out.append(h)
    return out


def check(paths):
    problems, found = [], {name: [] for name in HOOKS}
    for p in paths:
        try:
            data = json.loads(Path(p).read_text())
        except (OSError, ValueError) as exc:
            problems.append(f"{p}: cannot read ({exc})")
            continue
        for h in bash_hooks(data):
            cmd = h.get("command", "")
            for name in HOOKS:
                if name in cmd:
                    found[name].append((p, h))
    for name, timeout in HOOKS.items():
        if not found[name]:
            problems.append(f"{name}: not registered as a PreToolUse hook for Bash in any of the given files")
            continue
        for p, h in found[name]:
            cmd = h.get("command", "")
            if not re.search(r"origin/main:tools/hooks/" + re.escape(name), cmd):
                problems.append(f"{p}: {name} runs a checkout's file, not origin/main - it runs whatever "
                                f"branch that checkout is on (a placeholder there means no gate)")
            if re.search(r"\bcase\b[^;]*\bin\b", cmd):
                problems.append(f"{p}: {name} sits behind a pre-filter (case ... in) - commands that do not "
                                f"match it (gh pr new, git aliases, a script file) never reach the hook")
            t = h.get("timeout")
            if t is not None and t < timeout:
                problems.append(f"{p}: {name} timeout {t}s is below {timeout}s - a killed hook does not block")
            if t is None and timeout > 60:
                problems.append(f"{p}: {name} has no timeout; set {timeout}s (the script keeps its budget below it)")
    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("settings", nargs="*")
    ap.add_argument("--repo", default="<path-to-mission-control-checkout>")
    ap.add_argument("--print", action="store_true", help="print the canonical entry")
    a = ap.parse_args(argv)
    if a.print:
        print(json.dumps(canonical(a.repo), indent=2))
        return 0
    if not a.settings:
        ap.error("give the settings files a session loads (user + project)")
    problems = check(a.settings)
    for x in problems:
        print("check-registration: " + x, file=sys.stderr)
    if problems:
        print("check-registration: FAIL - canonical entry: check-registration.py --print --repo <checkout>",
              file=sys.stderr)
        return 1
    print("check-registration: ok - both hooks run from origin/main without a pre-filter", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
