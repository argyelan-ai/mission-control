"""Guards the timezone-aware datetime sweep (sqlmodel>=0.0.45 requirement).

sqlmodel 0.0.45 (2026-09-21) rejects naive datetimes in process_bind_param.
Every datetime the backend stores in or compares against the DB must be
timezone-aware. This file has two guards:

1. An AST scan over backend/app: no datetime.utcnow (call or bare reference,
   e.g. ``default_factory=datetime.utcnow``) and no zero-argument
   ``datetime.now()`` may remain. Comments are invisible to the AST, so the
   documented ``# NICHT datetime.now() verwenden`` warning in chat_outbound
   does not trip this test.
2. A behavioral check: vault_cleanup.archive_batch must write a tz-aware
   ``archived_at`` into board_memory.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

BACKEND_APP = Path(__file__).resolve().parent.parent / "app"


def _naive_datetime_sites() -> list[tuple[Path, int, str]]:
    """All (file, line, snippet) where a naive UTC datetime is produced."""
    sites: list[tuple[Path, int, str]] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "utcnow":
                # Catches both datetime.utcnow() and bare datetime.utcnow
                # (default_factory / onupdate references).
                sites.append((path, node.lineno, f"datetime.utcnow @ line {node.lineno}"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "now"
                and not node.args
                and not node.keywords
            ):
                sites.append((path, node.lineno, f"datetime.now() @ line {node.lineno}"))
    return sites


def test_no_naive_utcnow_or_bare_now_in_backend_app():
    sites = _naive_datetime_sites()
    assert sites == [], (
        "Naive datetime producers found in backend/app — sqlmodel>=0.0.45 "
        "rejects naive datetimes on bind:\n"
        + "\n".join(f"  {p.relative_to(BACKEND_APP)}:{n} {s}" for p, n, s in sites)
    )


@pytest.mark.asyncio
async def test_archive_batch_writes_tz_aware_archived_at(tmp_path, session):
    """archive_batch must store archived_at as timezone-aware UTC."""
    from app.models.memory import BoardMemory
    from app.services.vault_cleanup import archive_batch

    vault = tmp_path / "vault"
    archive = tmp_path / "archive" / "tz"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "a.md").write_text("---\nid: 0000-tz\nagent: system\n---\nbody")

    bm = BoardMemory(
        id=uuid.UUID("00000000-0000-0000-0000-00000000aa01"),
        agent_id=None,
        board_id=None,
        memory_type="journal",
        content="body",
        source="system",
    )
    session.add(bm)
    await session.commit()

    result = await archive_batch(session, vault, archive, [("memory/a.md", bm.id, "H1")])
    assert result.moved == 1

    refreshed = await session.get(BoardMemory, bm.id)
    assert refreshed.archived_at is not None
    assert refreshed.archived_at.tzinfo is not None, (
        "archived_at is naive — violates the tz-aware datetime contract"
    )
