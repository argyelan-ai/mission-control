#!/usr/bin/env python3
"""privacy-scan.py — stop the operator's private world from entering the repo.

This is a public repository that grew out of one person's machine, and the
things that leak out of it are not passwords. They are names and addresses:

  * agent names from the author's own fleet,
  * absolute macOS home paths carrying a login name,
  * Tailscale addresses and MagicDNS names of private machines.

Five commits published the author's fleet before anyone noticed, and the leak
gate never saw a thing — it only greps for FILE NAMES.

Two jobs, deliberately split:

  gitleaks (.gitleaks.toml, same CI job)
      Zero tolerance for the address shapes. The tree is clean under those
      rules today, so anything it finds is new by definition.

  this script
      The fleet-name blocklist, where the tree is NOT clean: dozens of files
      still carry the names. A baseline in docs/privacy-sweep-backlog.md
      records exactly which — so the check is green today and still turns red
      the moment a name lands somewhere new. The baseline is the work backlog,
      not an amnesty: clean a file up and the scan tells you to strike it off.

Usage:
    scripts/privacy-scan.py                    # check (CI)
    scripts/privacy-scan.py --update-baseline  # after cleaning a file up
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Agent names from the author's own fleet. Not a list of "bad words" — a list
# of things that describe one machine and mean nothing to anyone who downloads
# this. Ship roles (`reviewer`, `tester`) and slugs the product itself defines
# stay out of it; only the names a person gave their own agents belong here.
FLEET_NAMES = ("sparky", "shakespeare", "freecode", "davinci")

BACKLOG = Path("docs/privacy-sweep-backlog.md")
BASELINE_BEGIN = "<!-- privacy-baseline:begin -->"
BASELINE_END = "<!-- privacy-baseline:end -->"

# Files that must contain the names to do their job: this scanner, its
# backlog, and the tests that prove either of them actually bites. They are
# exempt rather than baselined on purpose — the baseline is a list of work to
# be done, and there is no work to do here.
#
# Keep this list tiny. Every entry is a blind spot, and "the guard reported
# itself, so we switched the guard off" is exactly how these checks die.
SELF = (
    "scripts/privacy-scan.py",
    "docs/privacy-sweep-backlog.md",
    "backend/tests/test_privacy_scan.py",
    "frontend-v2/src/components/pages/OfficeView/OrgChart/__tests__/org-chart-data.test.ts",
)

_MAX_BYTES = 2_000_000


def _tracked_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, timeout=120
    )
    return [p for p in out.stdout.split("\0") if p]


def scan(root: Path) -> dict[str, set[str]]:
    """Return {relative path: {fleet name, ...}} for every tracked file."""
    patterns = {name: re.compile(rf"\b{name}\b", re.IGNORECASE) for name in FLEET_NAMES}
    found: dict[str, set[str]] = {}
    for rel in _tracked_files(root):
        if rel in SELF:
            continue
        path = root / rel
        try:
            if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = {name for name, rx in patterns.items() if rx.search(text)}
        if hits:
            found[rel] = hits
    return found


def read_baseline(root: Path) -> dict[str, set[str]]:
    """Parse the baseline block out of the backlog document.

    One artifact, not two: the list a human reads and the list CI enforces are
    the same lines, so they cannot drift apart.
    """
    doc = root / BACKLOG
    if not doc.exists():
        return {}
    text = doc.read_text(encoding="utf-8")
    if BASELINE_BEGIN not in text or BASELINE_END not in text:
        return {}
    block = text.split(BASELINE_BEGIN, 1)[1].split(BASELINE_END, 1)[0]
    baseline: dict[str, set[str]] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "`", "<!--")):
            continue
        if ":" not in line:
            continue
        rel, names = line.split(":", 1)
        baseline[rel.strip()] = {n.strip() for n in names.split(",") if n.strip()}
    return baseline


def render_baseline(found: dict[str, set[str]]) -> str:
    return "\n".join(
        f"{rel}: {', '.join(sorted(found[rel]))}" for rel in sorted(found)
    )


def write_baseline(root: Path, found: dict[str, set[str]]) -> None:
    doc = root / BACKLOG
    text = doc.read_text(encoding="utf-8")
    head, rest = text.split(BASELINE_BEGIN, 1)
    _, tail = rest.split(BASELINE_END, 1)
    body = render_baseline(found)
    doc.write_text(
        f"{head}{BASELINE_BEGIN}\n```\n{body}\n```\n{BASELINE_END}{tail}",
        encoding="utf-8",
    )


def check(root: Path) -> int:
    found = scan(root)
    baseline = read_baseline(root)

    new: list[str] = []
    for rel, names in sorted(found.items()):
        unknown = names - baseline.get(rel, set())
        if unknown:
            new.append(f"  {rel}: {', '.join(sorted(unknown))}")

    stale: list[str] = []
    for rel, names in sorted(baseline.items()):
        gone = names - found.get(rel, set())
        if gone:
            stale.append(f"  {rel}: {', '.join(sorted(gone))}")

    if new:
        print("Fleet names in files the backlog does not know about:", file=sys.stderr)
        print("\n".join(new), file=sys.stderr)
        print(
            "\nThese names describe one person's machine. Use a neutral name "
            "(alpha, beta, <slug>) or, if the occurrence is genuinely needed, "
            "add the file to docs/privacy-sweep-backlog.md with a reason.",
            file=sys.stderr,
        )
    if stale:
        print(
            "\nThe backlog still lists names these files no longer contain:",
            file=sys.stderr,
        )
        print("\n".join(stale), file=sys.stderr)
        print(
            "\nGood news — that is progress. Run "
            "`scripts/privacy-scan.py --update-baseline` to strike them off.",
            file=sys.stderr,
        )

    if new or stale:
        return 1
    print(f"privacy-scan: clean ({len(baseline)} files on the known backlog)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="rewrite the baseline block in docs/privacy-sweep-backlog.md",
    )
    ap.add_argument("--root", default=None, help="repository root (default: git toplevel)")
    args = ap.parse_args()

    if args.root:
        root = Path(args.root).resolve()
    else:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        )
        if top.returncode != 0:
            print("privacy-scan: not a git checkout", file=sys.stderr)
            return 1
        root = Path(top.stdout.strip())

    if args.update_baseline:
        write_baseline(root, scan(root))
        print(f"privacy-scan: baseline in {BACKLOG} rewritten")
        return 0
    return check(root)


if __name__ == "__main__":
    raise SystemExit(main())
