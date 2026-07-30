"""The migration chain must have exactly ONE head.

Two branches that each add a migration on top of the same parent produce two
heads. Every branch stays green on its own — the collision only exists once BOTH
are on main, and then `alembic upgrade head` refuses to run:

    Multiple head revisions are present

That is a broken deploy, not a broken test, and it is found at the worst moment.
It happened for real on 2026-07-28: #183 and #184 both revised
0166_thread_telegram_topic_id.

Alembic is not asked here — this parses the files, so the test needs no
database and fails in the PR that creates the second head.
"""
from __future__ import annotations

import re
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

_REV = re.compile(r'^revision(?::\s*str)?\s*=\s*["\'](.+?)["\']', re.M)
_DOWN = re.compile(r'^down_revision(?::\s*[^=]+)?\s*=\s*["\']?(.+?)["\']?\s*$', re.M)


def _chain() -> tuple[set[str], set[str]]:
    revs: set[str] = set()
    downs: set[str] = set()
    for f in sorted(VERSIONS.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        r = _REV.search(text)
        if not r:
            continue
        revs.add(r.group(1))
        d = _DOWN.search(text)
        if d and d.group(1) not in ("None", ""):
            downs.add(d.group(1))
    return revs, downs


def test_chain_is_not_empty() -> None:
    """Guard against a vacuously green test if the glob ever misses."""
    revs, _ = _chain()
    assert len(revs) > 50


def test_exactly_one_head() -> None:
    revs, downs = _chain()
    heads = sorted(revs - downs)
    assert len(heads) == 1, (
        f"{len(heads)} alembic heads: {heads}. Two migrations share a parent — "
        "re-point the newer one at the other so the chain stays linear. "
        "See this module's docstring."
    )


def test_every_parent_exists() -> None:
    """A typo'd down_revision is the other way to break `upgrade head`."""
    revs, downs = _chain()
    missing = sorted(downs - revs)
    assert not missing, f"down_revision points at unknown revisions: {missing}"
