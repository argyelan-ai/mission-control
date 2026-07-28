"""Alembic revision ids must fit the version table.

``alembic_version.version_num`` is ``varchar(32)``. A longer id passes every
local run — the table already holds a row there — and only explodes on a FRESH
database, i.e. in the Fresh-boot E2E or on a new installation:

    StringDataRightTruncationError: value too long for type character varying(32)

That is exactly what happened with ``0167_runtime_display_name_derived`` (33
chars). Cheap to prevent, expensive to debug.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
LIMIT = 32

_REV = re.compile(r'^revision(?::\s*str)?\s*=\s*["\'](.+?)["\']', re.M)
_DOWN = re.compile(r'^down_revision(?::\s*[^=]+)?\s*=\s*["\'](.+?)["\']', re.M)


def _ids() -> list[tuple[str, str, str]]:
    out = []
    for f in sorted(VERSIONS.glob("*.py")):
        text = f.read_text(encoding="utf-8")
        rev = _REV.search(text)
        down = _DOWN.search(text)
        if rev:
            out.append((f.name, rev.group(1), down.group(1) if down else ""))
    return out


def test_versions_dir_is_not_empty() -> None:
    """Guard against a vacuously green suite if the glob ever misses."""
    assert len(_ids()) > 50


@pytest.mark.parametrize("name,rev,down", _ids(), ids=lambda v: v if isinstance(v, str) else "")
def test_revision_ids_fit_the_version_column(name: str, rev: str, down: str) -> None:
    assert len(rev) <= LIMIT, (
        f"{name}: revision id {rev!r} is {len(rev)} chars, limit is {LIMIT}. "
        "A fresh database cannot record it — see this module's docstring."
    )
    if down:
        assert len(down) <= LIMIT, (
            f"{name}: down_revision {down!r} is {len(down)} chars, limit is {LIMIT}."
        )
