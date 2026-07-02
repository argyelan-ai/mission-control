"""Tests for W3-B vault wikilink backfill (all mocked — no live network)."""

import json
import pytest
import frontmatter
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.services.vault_wikilink_backfill import (
    generate_wikilinks,
    fetch_top_k_candidates,
    backfill_wikilinks,
)
from app.services.vault_cleanup_state import VaultCleanupState


# ── generate_wikilinks ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generates_2_to_4_wikilinks_from_candidates():
    spark = AsyncMock()
    spark.complete = AsyncMock(
        return_value=json.dumps([
            {"slug": "cand-2", "relation": "supersedes"},
            {"slug": "cand-5", "relation": "refines"},
        ])
    )
    candidates = [
        {"slug": "cand-0", "title": "Note 0", "excerpt": "..."},
        {"slug": "cand-2", "title": "Older Approach", "excerpt": "..."},
        {"slug": "cand-5", "title": "Related", "excerpt": "..."},
        {"slug": "cand-9", "title": "Unrelated", "excerpt": "..."},
    ]
    result = await generate_wikilinks(
        spark, "My New Take", "Updated thinking on...", candidates
    )
    assert result == [("cand-2", "supersedes"), ("cand-5", "refines")]


@pytest.mark.asyncio
async def test_falls_back_to_top_2_when_llm_returns_garbage():
    spark = AsyncMock()
    spark.complete = AsyncMock(return_value="this is not json")
    candidates = [
        {"slug": "c0", "title": "T0", "excerpt": "..."},
        {"slug": "c1", "title": "T1", "excerpt": "..."},
        {"slug": "c2", "title": "T2", "excerpt": "..."},
    ]
    result = await generate_wikilinks(spark, "T", "x", candidates)
    assert result == [("c0", "related-to"), ("c1", "related-to")]


@pytest.mark.asyncio
async def test_invalid_relation_falls_back_to_related_to():
    spark = AsyncMock()
    spark.complete = AsyncMock(
        return_value=json.dumps([
            {"slug": "c0", "relation": "invented-relation-type"},
            {"slug": "c1", "relation": "refines"},
        ])
    )
    candidates = [
        {"slug": "c0", "title": "T0", "excerpt": "..."},
        {"slug": "c1", "title": "T1", "excerpt": "..."},
    ]
    result = await generate_wikilinks(spark, "T", "x", candidates)
    assert result == [("c0", "related-to"), ("c1", "refines")]


@pytest.mark.asyncio
async def test_strips_markdown_code_fences():
    spark = AsyncMock()
    spark.complete = AsyncMock(
        return_value=(
            "```json\n"
            '[{"slug": "c0", "relation": "refines"}, '
            '{"slug": "c1", "relation": "related-to"}]'
            "\n```"
        )
    )
    candidates = [
        {"slug": "c0", "title": "T0", "excerpt": "..."},
        {"slug": "c1", "title": "T1", "excerpt": "..."},
    ]
    result = await generate_wikilinks(spark, "T", "x", candidates)
    assert result == [("c0", "refines"), ("c1", "related-to")]


# ── fetch_top_k_candidates ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetcher_returns_top_k_excluding_self():
    """Slug derived from payload `path` stem; self-slug excluded."""
    qdrant = MagicMock()
    # Synchronous return (MagicMock is not a coroutine) — fetch_top_k_candidates
    # handles both sync (test mocks) and async (AsyncQdrantClient).
    qdrant.search = MagicMock(
        return_value=[
            MagicMock(id="x1", score=1.0, payload={"path": "memory/self-slug.md"}),
            MagicMock(id="x2", score=0.81, payload={"path": "memory/c1.md"}),
            MagicMock(id="x3", score=0.76, payload={"path": "memory/c2.md"}),
            MagicMock(id="x4", score=0.71, payload={"path": "memory/c3.md"}),
        ]
    )
    embedding = [0.1] * 768
    result = await fetch_top_k_candidates(
        qdrant, embedding, exclude_slug="self-slug", k=8
    )
    slugs = [c["slug"] for c in result]
    assert "self-slug" not in slugs
    assert slugs == ["c1", "c2", "c3"]


@pytest.mark.asyncio
async def test_fetcher_respects_k_limit():
    qdrant = MagicMock()
    qdrant.search = MagicMock(
        return_value=[
            MagicMock(id=f"x{i}", score=0.9 - i * 0.01, payload={"path": f"memory/c{i}.md"})
            for i in range(10)
        ]
    )
    result = await fetch_top_k_candidates(
        qdrant, [0.1] * 768, exclude_slug="none", k=3
    )
    assert len(result) == 3


# ── backfill_wikilinks ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backfill_writes_related_frontmatter_and_inline(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "note.md").write_text(
        "---\nagent: hermes\ntype: knowledge\nslug: note\ntitle: Test\n---\n"
        "Note body about charge=-850."
    )
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()

    spark = AsyncMock()
    spark.embed = AsyncMock(return_value=[0.1] * 768)
    # LLM returns only 1 valid pick → falls back to top-2 from candidates
    spark.complete = AsyncMock(
        return_value='[{"slug":"target-a","relation":"refines"}]'
    )
    qdrant = MagicMock()
    qdrant.search = MagicMock(
        return_value=[
            MagicMock(id="t-a", score=0.8, payload={"path": "memory/target-a.md"}),
            MagicMock(id="t-b", score=0.7, payload={"path": "memory/target-b.md"}),
        ]
    )

    result = await backfill_wikilinks(spark, qdrant, vault, state)
    assert result.processed == 1

    post = frontmatter.load(vault / "memory" / "note.md")
    # generate_wikilinks falls back to top-2 (only 1 LLM pick → fallback engaged)
    assert post.metadata["related"] == ["[[target-a]]", "[[target-b]]"]
    assert post.metadata["relations"] == {"target-a": "related-to", "target-b": "related-to"}
    assert "## Verwandt" in post.content
    assert "[[target-a]]" in post.content


@pytest.mark.asyncio
async def test_backfill_skips_notes_with_existing_related(tmp_path):
    vault = tmp_path / "vault"
    (vault / "memory").mkdir(parents=True)
    (vault / "memory" / "a.md").write_text(
        "---\nslug: a\ntitle: A\nrelated:\n- '[[x]]'\n- '[[y]]'\n---\nbody"
    )
    state = VaultCleanupState(root=tmp_path / "state")
    state.ensure()

    spark = AsyncMock()
    spark.embed = AsyncMock()
    qdrant = MagicMock()

    result = await backfill_wikilinks(spark, qdrant, vault, state)
    assert result.processed == 0
    assert result.skipped == 1
    spark.embed.assert_not_called()
