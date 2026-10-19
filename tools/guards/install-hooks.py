#!/usr/bin/env python3
"""install-hooks.py — put the guards where the mistakes happen: into Claude Code's
settings.json, as a PreToolUse hook (bash-hook.sh) and a PostToolUse hook
(post-push-hook.sh) on the Bash tool.

Why a script: the hooks must sit in EVERY settings file a session can load
(user-wide ~/.claude/settings.json covers worktrees and sessions started outside
the repo), each entry must be the same text so Claude Code de-duplicates them,
and nothing that is already there may be removed.

Each hook runs the version of tools/guards on origin/main of a local checkout,
cached by the tree hash (~/.mc/guards/cache/<tree>). Before tools/guards/<script>
exists on origin/main the hook is a no-op (exit 0).

Default is a dry run: it prints what would change and writes nothing.
  install-hooks.py --repo <mc-checkout> --settings <settings.json> [--settings ...]
  install-hooks.py ... --apply      write; a backup <settings>.bak-guards-<ts> first
  install-hooks.py ... --check      exit 1 when a file lacks an entry (for audits)

Only entries whose command runs tools/guards/<bash-hook.sh|post-push-hook.sh>
are ever replaced, and only by the current text. Other hooks stay as they are.
Mode (shadow/block) is not set here: ~/.mc/guards/mode, see README.

Exit: 0 ok (or nothing to do), 1 --check found a missing entry, 2 usage/input error.
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import re
import shutil
import sys

HOOKS = (
    # (event, script, timeout seconds)
    ("PreToolUse", "bash-hook.sh", 60),
    ("PostToolUse", "post-push-hook.sh", 30),
)
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./~-]+$")


def launcher(repo: str, script: str) -> str:
    """The hook command: run tools/guards/<script> as it is on origin/main."""
    return (
        f"bash -c 'r={repo}; "
        f"t=$(git -C $r rev-parse -q --verify origin/main:tools/guards/{script} >/dev/null "
        f"&& git -C $r rev-parse origin/main:tools/guards) || exit 0; "
        f"d=$HOME/.mc/guards/cache/$t; "
        f"[ -f $d/{script} ] || {{ mkdir -p $d && git -C $r archive origin/main:tools/guards "
        f"| tar -x -C $d; }} || exit 0; bash $d/{script}'"
    )


def _is_ours(cmd: str, script: str) -> bool:
    return "tools/guards" in cmd and script in cmd


def merge(settings: dict, repo: str) -> tuple[dict, list[str]]:
    """Return (new settings, list of human-readable changes). Never removes
    foreign hooks; replaces an outdated entry of ours in place."""
    new = copy.deepcopy(settings)
    changes: list[str] = []
    hooks = new.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("'hooks' is not an object")
    for event, script, timeout in HOOKS:
        want = {"type": "command", "command": launcher(repo, script), "timeout": timeout}
        entries = hooks.setdefault(event, [])
        if not isinstance(entries, list):
            raise ValueError(f"'hooks.{event}' is not a list")
        found = False
        for entry in entries:
            for i, h in enumerate(entry.get("hooks") or []):
                if not _is_ours(str(h.get("command", "")), script):
                    continue
                if found:  # a second copy of ours: drop it, one run is enough
                    entry["hooks"][i] = None
                    changes.append(f"{event}: removed a duplicate {script} entry")
                    continue
                found = True
                if entry.get("matcher") != "Bash":
                    changes.append(f"{event}: {script} entry has matcher {entry.get('matcher')!r}, left as is")
                elif h != want:
                    entry["hooks"][i] = want
                    changes.append(f"{event}: updated {script} entry to the current text")
            if entry.get("hooks"):
                entry["hooks"] = [h for h in entry["hooks"] if h is not None]
        hooks[event] = [e for e in entries if e.get("hooks") != []]
        if not found:
            hooks[event].append({"matcher": "Bash", "hooks": [want]})
            changes.append(f"{event}: added {script} (matcher Bash, timeout {timeout}s)")
    return new, changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="local mission-control checkout (origin/main is read)")
    ap.add_argument("--settings", action="append", required=True, help="settings.json to update (repeatable)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    g.add_argument("--check", action="store_true", help="exit 1 when an entry is missing or outdated")
    a = ap.parse_args(argv)

    repo = os.path.abspath(os.path.expanduser(a.repo))
    if not _SAFE_PATH.match(repo):
        print(f"install-hooks: repo path has characters the hook command cannot carry: {repo}", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        print(f"install-hooks: not a git checkout: {repo}", file=sys.stderr)
        return 2

    rc = 0
    for path in a.settings:
        path = os.path.expanduser(path)
        try:
            with open(path) as f:
                current = json.load(f)
        except FileNotFoundError:
            current = {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"install-hooks: {path}: cannot read ({exc}) — nothing written", file=sys.stderr)
            return 2
        if not isinstance(current, dict):
            print(f"install-hooks: {path}: top level is not an object — nothing written", file=sys.stderr)
            return 2
        try:
            new, changes = merge(current, repo)
        except ValueError as exc:
            print(f"install-hooks: {path}: {exc} — nothing written", file=sys.stderr)
            return 2
        if not changes or new == current:
            print(f"ok      {path}: guards installed, nothing to change")
            continue
        for c in changes:
            print(f"{'change' if a.apply else 'would'}  {path}: {c}")
        if a.check:
            rc = 1
            continue
        if not a.apply:
            continue
        if os.path.exists(path):
            stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(path, f"{path}.bak-guards-{stamp}")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = f"{path}.tmp-guards"
        with open(tmp, "w") as f:
            json.dump(new, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    if not a.apply and not a.check:
        print("dry run — nothing written. Add --apply to write.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
