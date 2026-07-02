"""Tests for vault_title_backfill — W2.1 generate_title_for_note + W2.2 backfill_titles."""

import pytest
from pathlib import Path
from unittest.mock import AsyncMock

import frontmatter

from app.services.vault_title_backfill import generate_title_for_note, backfill_titles
from app.services.vault_cleanup_state import VaultCleanupState


# ── Task 2.1 — generate_title_for_note ────────────────────────────────────────


@pytest.mark.asyncio
async def test_generates_short_title_from_content():
    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="Force-Directed Layout Defaults")
    title = await generate_title_for_note(
        spark, content="The force-directed layout needs charge=-850..."
    )
    assert title == "Force-Directed Layout Defaults"
    spark.complete.assert_awaited_once()
    args, kwargs = spark.complete.call_args
    assert kwargs["max_tokens"] <= 40
    assert kwargs["temperature"] <= 0.3


@pytest.mark.asyncio
async def test_strips_quotes_and_periods():
    spark = AsyncMock()
    spark.complete = AsyncMock(return_value='"Vault Cleanup Plan."')
    title = await generate_title_for_note(spark, content="x")
    assert title == "Vault Cleanup Plan"


@pytest.mark.asyncio
async def test_truncates_overly_long_response():
    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="x " * 100)
    title = await generate_title_for_note(spark, content="x")
    assert len(title) <= 80


# ── Task 2.2 — backfill_titles ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_titles_writes_frontmatter(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "a.md").write_text(
        "---\nagent: hermes\ntype: knowledge\n---\nthe charge=-850 setting works"
    )
    (vault / "memory" / "b.md").write_text(
        "---\nagent: hermes\ntype: knowledge\ntitle: Already Has Title\n---\nbody"
    )
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()

    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="Synthetic Title For Note")

    result = await backfill_titles(spark, vault, state)
    assert result.processed == 1
    assert result.skipped == 1

    a = frontmatter.load(vault / "memory" / "a.md")
    assert a.metadata["title"] == "Synthetic Title For Note"
    b = frontmatter.load(vault / "memory" / "b.md")
    assert b.metadata["title"] == "Already Has Title"


@pytest.mark.asyncio
async def test_backfill_resumes_from_checkpoint(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    for i in range(5):
        (vault / "memory" / f"n{i}.md").write_text(
            f"---\nagent: hermes\ntype: knowledge\n---\nbody {i}"
        )
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()
    state.set_checkpoint("title-backfill", "memory/n2.md")

    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="Generated Title")

    result = await backfill_titles(spark, vault, state)
    # Should skip n0, n1, n2 and process n3, n4
    assert result.processed == 2


@pytest.mark.asyncio
async def test_backfill_skips_inbox_and_rejected(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "a.md").write_text("---\nagent: hermes\n---\nbody")
    (vault / "_inbox").mkdir()
    (vault / "_inbox" / "x.md").write_text("---\nagent: hermes\n---\nstub")
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()
    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="Title")
    result = await backfill_titles(spark, vault, state)
    assert result.processed == 1  # _inbox skipped
