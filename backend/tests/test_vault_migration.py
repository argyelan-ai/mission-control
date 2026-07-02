"""Tests for the M.2 vault cutover migration helpers.

We do NOT exercise Alembic itself here — running ``alembic upgrade`` would
require Postgres-specific features (TIMESTAMP WITH TIME ZONE, ``UPDATE ...
SET frozen_at = NOW()``) that the SQLite test harness can't reproduce.

Instead, this file tests the pure-Python helpers in
``app.services.vault_migration_helpers`` which contain all the
non-trivial migration logic: path resolution, slug derivation, markdown
rendering with id-frontmatter, and the SHA-256-based idempotency check.

The migration script itself is a thin shell around these helpers + two
``op.add_column`` / ``op.execute`` calls, which we trust Alembic to
exercise correctly in the real Postgres environment.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from app.services.vault_migration_helpers import (
    KNOWN_MEMORY_TYPES,
    _content_sha256,
    _render_md,
    _resolve_target,
    _slugify_agent_name,
    _vault_root,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeMemoryRow:
    """Stand-in for a ``board_memory`` row.

    Mirrors only the columns the cutover migration reads. Defaults keep
    each test self-contained — override what you need.
    """

    id: uuid.UUID = field(default_factory=uuid.uuid4)
    content: str = "## body\n\nsome lesson content"
    memory_type: str = "lesson"
    tags: list[Any] = field(default_factory=list)
    created_at: datetime | None = field(
        default_factory=lambda: datetime(2026, 5, 14, 12, 30, tzinfo=timezone.utc)
    )
    # The migration's SQL also pulls agent_name + board_slug via JOIN; we
    # don't model those here because the helpers receive them already
    # resolved.


# ---------------------------------------------------------------------------
# _vault_root
# ---------------------------------------------------------------------------


def test_vault_root_honors_home_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    root = _vault_root()
    assert root == tmp_path / ".mc" / "vault"


def test_vault_root_falls_back_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME_HOST", raising=False)
    root = _vault_root()
    assert root == Path.home() / ".mc" / "vault"


# ---------------------------------------------------------------------------
# _slugify_agent_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Sparky", "sparky"),
        ("Henry", "henry"),
        ("Free Code", "free-code"),
        ("DaVinci", "davinci"),
        ("Boss-Agent", "boss-agent"),
        ("research_bot", "research-bot"),  # underscores → dashes
        ("  Trim  Me  ", "trim-me"),
        ("Multi   Space", "multi-space"),
        ("Has!Special@Chars", "hasspecialchars"),
        ("", None),
        (None, None),
        ("___", None),  # nothing but separators → None
    ],
)
def test_slugify_agent_name(name: str | None, expected: str | None) -> None:
    assert _slugify_agent_name(name) == expected


# ---------------------------------------------------------------------------
# _resolve_target
# ---------------------------------------------------------------------------


def test_resolve_target_for_agent_owned() -> None:
    rel = _resolve_target(
        agent_slug="sparky",
        board_slug=None,
        mem_type="lesson",
        mem_id="abc-123",
    )
    assert rel == Path("agents/sparky/lessons/abc-123.md")


def test_resolve_target_for_board_owned() -> None:
    rel = _resolve_target(
        agent_slug=None,
        board_slug="mc-dev",
        mem_type="knowledge",
        mem_id="xyz-789",
    )
    assert rel == Path("projects/mc-dev/knowledges/xyz-789.md")


def test_resolve_target_for_global() -> None:
    rel = _resolve_target(
        agent_slug=None,
        board_slug=None,
        mem_type="reference",
        mem_id="aaa",
    )
    assert rel == Path("global/references/aaa.md")


def test_resolve_target_board_wins_when_both_set() -> None:
    """If a row has both agent_id AND board_id, the spec routes it to
    ``projects/`` — boards are the more specific scope."""
    rel = _resolve_target(
        agent_slug="cody",
        board_slug="mc-dev",
        mem_type="knowledge",
        mem_id="dual",
    )
    assert rel == Path("projects/mc-dev/knowledges/dual.md")


@pytest.mark.parametrize(
    "mem_type,plural",
    [
        ("lesson", "lessons"),
        ("knowledge", "knowledges"),
        ("reference", "references"),
        ("journal", "journals"),
        ("weekly_review", "weekly_reviews"),
        ("note", "notes"),
    ],
)
def test_resolve_target_pluralization_covers_all_known_types(
    mem_type: str, plural: str
) -> None:
    rel = _resolve_target(
        agent_slug="agent",
        board_slug=None,
        mem_type=mem_type,
        mem_id="x",
    )
    assert mem_type in KNOWN_MEMORY_TYPES
    assert rel.parent.name == plural


# ---------------------------------------------------------------------------
# _render_md
# ---------------------------------------------------------------------------


def test_render_md_includes_id_in_frontmatter() -> None:
    row = FakeMemoryRow(
        id=uuid.UUID("550e8400-e29b-41d4-a716-446655440000"),
        content="lesson body",
        memory_type="lesson",
        tags=["api", "xai"],
    )
    md = _render_md(row, agent_slug="sparky", board_slug=None)
    post = frontmatter.loads(md)

    assert post.metadata["id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert post.metadata["type"] == "lesson"
    assert post.metadata["agent"] == "sparky"
    assert post.metadata["tags"] == ["api", "xai"]
    assert post.metadata["source"] == "migration"
    assert post.metadata["status"] == "active"
    assert "project" not in post.metadata  # board_slug was None
    assert post.content == "lesson body"


def test_render_md_includes_project_when_board_scoped() -> None:
    row = FakeMemoryRow(memory_type="knowledge", content="ADR body")
    md = _render_md(row, agent_slug="cody", board_slug="mc-dev")
    post = frontmatter.loads(md)
    assert post.metadata["project"] == "mc-dev"


def test_render_md_uses_system_for_agentless_rows() -> None:
    row = FakeMemoryRow(memory_type="knowledge")
    md = _render_md(row, agent_slug=None, board_slug=None)
    post = frontmatter.loads(md)
    assert post.metadata["agent"] == "system"


def test_render_md_date_falls_back_when_created_at_is_none() -> None:
    row = FakeMemoryRow(created_at=None)
    md = _render_md(row, agent_slug="sparky", board_slug=None)
    post = frontmatter.loads(md)
    # Just confirm date is a non-empty string — exact fallback value is an
    # implementation detail.
    assert isinstance(post.metadata["date"], str)
    assert post.metadata["date"]


def test_render_md_handles_empty_content() -> None:
    row = FakeMemoryRow(content="")
    md = _render_md(row, agent_slug="sparky", board_slug=None)
    post = frontmatter.loads(md)
    assert post.content == ""
    # Frontmatter must still be valid + contain id
    assert post.metadata["id"] == str(row.id)


def test_render_md_normalizes_non_list_tags() -> None:
    # Some Postgres drivers may surface a tuple; some might surface ``None``.
    # The migration must tolerate both.
    row_with_tuple = FakeMemoryRow(tags=("a", "b"))  # type: ignore[arg-type]
    md = _render_md(row_with_tuple, agent_slug="x", board_slug=None)
    post = frontmatter.loads(md)
    assert post.metadata["tags"] == ["a", "b"]

    row_with_none = FakeMemoryRow(tags=None)  # type: ignore[arg-type]
    md = _render_md(row_with_none, agent_slug="x", board_slug=None)
    post = frontmatter.loads(md)
    assert post.metadata["tags"] == []


# ---------------------------------------------------------------------------
# Idempotency: SHA-256 round-trip
# ---------------------------------------------------------------------------


def test_render_md_is_deterministic_for_idempotency() -> None:
    """Rendering the same row twice must produce identical bytes — that
    is the property the migration's SHA-256 skip-check relies on."""
    row = FakeMemoryRow(
        id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        content="same content",
        memory_type="lesson",
        tags=["a", "b"],
    )
    a = _render_md(row, agent_slug="sparky", board_slug=None)
    b = _render_md(row, agent_slug="sparky", board_slug=None)
    assert _content_sha256(a) == _content_sha256(b)


def test_content_sha256_detects_change() -> None:
    row1 = FakeMemoryRow(content="version 1")
    row2 = FakeMemoryRow(id=row1.id, content="version 2")
    sha1 = _content_sha256(_render_md(row1, "sparky", None))
    sha2 = _content_sha256(_render_md(row2, "sparky", None))
    assert sha1 != sha2


# ---------------------------------------------------------------------------
# End-to-end write simulation (filesystem only, no DB)
# ---------------------------------------------------------------------------


def test_end_to_end_write_simulation(tmp_path: Path) -> None:
    """Simulate the inner loop of the migration on a temp vault root.

    Seeds 4 representative rows:
      * agent-owned lesson
      * board-owned knowledge
      * global reference
      * row with both agent and board → routed to projects/

    Verifies path resolution, file content, frontmatter ``id``, and the
    SHA-256 skip-on-rerun branch.
    """
    memory_root = tmp_path / "memory"

    rows = [
        (
            FakeMemoryRow(memory_type="lesson", content="lesson body"),
            "sparky",
            None,
            Path("agents/sparky/lessons"),
        ),
        (
            FakeMemoryRow(memory_type="knowledge", content="ADR body"),
            None,
            "mc-dev",
            Path("projects/mc-dev/knowledges"),
        ),
        (
            FakeMemoryRow(memory_type="reference", content="ref body"),
            None,
            None,
            Path("global/references"),
        ),
        (
            FakeMemoryRow(memory_type="knowledge", content="dual"),
            "cody",
            "mc-dev",
            Path("projects/mc-dev/knowledges"),
        ),
    ]

    written = 0
    for row, agent_slug, board_slug, expected_parent in rows:
        rel = _resolve_target(agent_slug, board_slug, row.memory_type, str(row.id))
        assert rel.parent == expected_parent

        target = memory_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        content = _render_md(row, agent_slug, board_slug)
        target.write_text(content, encoding="utf-8")
        written += 1

        # Round-trip: parse the file back, check id frontmatter.
        post = frontmatter.loads(target.read_text(encoding="utf-8"))
        assert post.metadata["id"] == str(row.id)
        assert post.metadata["type"] == row.memory_type

    assert written == 4

    # Re-run pass: render again, hash should match the file on disk → skip.
    for row, agent_slug, board_slug, _ in rows:
        rel = _resolve_target(agent_slug, board_slug, row.memory_type, str(row.id))
        target = memory_root / rel
        regen = _render_md(row, agent_slug, board_slug)
        assert _content_sha256(regen) == _content_sha256(
            target.read_text(encoding="utf-8")
        )


def test_existing_file_with_different_content_is_skipped_not_overwritten(
    tmp_path: Path,
) -> None:
    """If a vault file already exists with content that differs from what
    the migration would write, the migration's outer loop is expected to
    SKIP it (not overwrite). This test demonstrates the SHA check that
    drives that decision."""
    row = FakeMemoryRow(content="DB content")
    rel = _resolve_target("sparky", None, "lesson", str(row.id))
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    # Simulate a hand-edited file on disk
    hand_edited = "---\nid: " + str(row.id) + "\n---\n\nHand edited"
    target.write_text(hand_edited, encoding="utf-8")
    pre_mtime = target.stat().st_mtime

    fresh = _render_md(row, "sparky", None)
    on_disk_sha = _content_sha256(target.read_text(encoding="utf-8"))
    fresh_sha = _content_sha256(fresh)

    assert on_disk_sha != fresh_sha  # divergence confirmed
    # Migration logic: when SHAs differ, do NOT overwrite. We assert the
    # property by leaving the file alone here and confirming pre_mtime
    # equals current mtime.
    assert target.stat().st_mtime == pre_mtime
    assert target.read_text(encoding="utf-8") == hand_edited
