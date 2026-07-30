"""The migration chain must stay deployable: one head, valid parents, ids ≤ 32.

Both failure modes here broke real deploys on 2026-07-28:

1. **Two heads.** #183 and #184 each added a migration on top of the same
   parent. Every branch was green on its own — the collision only existed once
   both were on main, where ``alembic upgrade head`` refuses to run:
   ``Multiple head revisions are present``. Resolved back then with the merge
   revision ``0169_merge_heads``; these tests catch the NEXT one in the PR that
   creates it instead of at deploy time. Merge revisions (tuple
   ``down_revision``) are the legitimate fix and parse as multiple parents.

2. **Revision id longer than 32 chars.** ``alembic_version.version_num`` is
   ``varchar(32)``. ``0167_runtime_display_name_derived`` (33 chars) passed
   every local run — a local database already holds a row — and only a FRESH
   database exploded: ``StringDataRightTruncationError``. The fresh-boot E2E
   caught it; this catches it before CI even builds an image.

No database needed: the files are parsed directly, so the failure lands in the
offending PR.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
LIMIT = 32


def _parse() -> list[tuple[str, str, tuple[str, ...]]]:
    """(filename, revision, parents) per migration — via ast, not regex.

    A merge revision declares ``down_revision = ("a", "b")``; a regex built for
    the single-string form silently mangles that tuple. ast handles both.
    """
    out = []
    for f in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        rev: str | None = None
        parents: tuple[str, ...] = ()
        for node in tree.body:
            # Both spellings exist in this tree: `down_revision = "x"` and the
            # annotated `down_revision: Union[str, None] = "x"` (e.g. 0102).
            if isinstance(node, ast.AnnAssign):
                targets = [node.target] if node.value is not None else []
            elif isinstance(node, ast.Assign):
                targets = node.targets
            else:
                continue
            for target in targets:
                name = getattr(target, "id", None)
                if name == "revision":
                    rev = ast.literal_eval(node.value)
                elif name == "down_revision":
                    val = ast.literal_eval(node.value)
                    if val is None:
                        parents = ()
                    elif isinstance(val, str):
                        parents = (val,)
                    else:
                        parents = tuple(val)
        if rev is not None:
            out.append((f.name, rev, parents))
    return out


def test_chain_is_not_empty() -> None:
    """Guard against a vacuously green suite if the glob ever misses."""
    assert len(_parse()) > 50


def test_exactly_one_head() -> None:
    entries = _parse()
    revs = {r for _, r, _ in entries}
    referenced = {p for _, _, parents in entries for p in parents}
    heads = sorted(revs - referenced)
    assert len(heads) == 1, (
        f"{len(heads)} alembic heads: {heads}. Two migrations share a parent — "
        "either re-point the newer one or add a merge revision "
        "(like 0169_merge_heads). See this module's docstring."
    )


def test_every_parent_exists() -> None:
    entries = _parse()
    revs = {r for _, r, _ in entries}
    missing = sorted(
        {p for _, _, parents in entries for p in parents} - revs
    )
    assert not missing, f"down_revision points at unknown revisions: {missing}"


@pytest.mark.parametrize(
    "name,rev,parents", _parse(), ids=[n for n, _, _ in _parse()]
)
def test_revision_ids_fit_the_version_column(
    name: str, rev: str, parents: tuple[str, ...]
) -> None:
    assert len(rev) <= LIMIT, (
        f"{name}: revision id {rev!r} is {len(rev)} chars, limit is {LIMIT} "
        f"(alembic_version.version_num is varchar(32); a fresh database cannot "
        "record it — see this module's docstring)."
    )
