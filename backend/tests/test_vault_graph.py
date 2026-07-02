"""Tests for vault_graph service — node/edge/cluster builder.

Patterns mirror test_vault_index.py (real VaultIndex over a tmp_path) +
test_vault_embeddings.py (MagicMock for Qdrant scroll).
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from app.services.vault_graph import build_graph
from app.services.vault_index import VaultIndex


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def index(tmp_path):
    db_path = tmp_path / "test_index.db"
    return VaultIndex(db_path=db_path, vault_path=tmp_path)


def _make_note(vault: Path, rel_path: str, content: str = "body", **meta) -> Path:
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(content, **meta)
    full.write_text(frontmatter.dumps(post))
    return full


def _seed(index, vault: Path, rel: str, content: str, **meta) -> Path:
    f = _make_note(vault, rel, content, **meta)
    post = frontmatter.load(str(f))
    index.upsert(f, post)
    return f


@pytest.fixture
def activity_no_views():
    """VaultActivity stub returning no views."""
    a = MagicMock()
    a.top_n_views = AsyncMock(return_value=[])
    return a


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_builds_nodes_from_index(index, tmp_path, activity_no_views):
    """Three notes with different types/agents → three correctly-shaped nodes."""
    _seed(
        index, tmp_path, "agents/sparky/lessons/a.md",
        content="content a",
        id="1", type="lesson", agent="sparky", date="2026-05-14T15:00:00Z",
        tags=["api", "xai"],
    )
    _seed(
        index, tmp_path, "agents/cody/references/b.md",
        content="content b",
        id="2", type="reference", agent="cody", date="2026-05-14T15:00:00Z",
        tags=["frontend"],
    )
    _seed(
        index, tmp_path, "global/c.md",
        content="content c",
        id="3", type="reference", agent="henry", date="2026-05-14T15:00:00Z",
        tags=[],
    )

    graph = await build_graph(index, None, activity_no_views, cluster=False)

    assert graph["stats"]["nodes"] == 3
    nodes_by_label = {n["label"]: n for n in graph["nodes"]}
    assert nodes_by_label["a"]["agent"] == "sparky"
    assert nodes_by_label["a"]["type"] == "lesson"
    assert "api" in nodes_by_label["a"]["tags"]
    assert nodes_by_label["b"]["type"] == "reference"
    assert nodes_by_label["c"]["agent"] == "henry"
    # IDs should be the vault-relative path.
    assert nodes_by_label["a"]["id"] == "agents/sparky/lessons/a.md"
    # No clustering → cluster_id stays None.
    assert all(n["cluster_id"] is None for n in graph["nodes"])
    assert graph["clusters"] == []
    # built_at should be UTC ISO8601 ending in Z.
    assert graph["built_at"].endswith("Z")


@pytest.mark.asyncio
async def test_graph_extracts_wikilinks_as_edges(index, tmp_path, activity_no_views):
    """Wikilinks [[noteB]] and [[noteC]] in note A produce 2 edges A→B, A→C."""
    _seed(
        index, tmp_path, "noteA.md",
        content="Linked to [[noteB]] and [[noteC]] for context.",
        id="a", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )
    _seed(
        index, tmp_path, "noteB.md",
        content="other body",
        id="b", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )
    _seed(
        index, tmp_path, "noteC.md",
        content="other body",
        id="c", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )

    graph = await build_graph(index, None, activity_no_views, cluster=False)

    edges = graph["edges"]
    assert len(edges) == 2
    pairs = {(e["source"], e["target"], e["weight"]) for e in edges}
    assert ("noteA.md", "noteB.md", 1) in pairs
    assert ("noteA.md", "noteC.md", 1) in pairs


@pytest.mark.asyncio
async def test_graph_dedupes_repeated_wikilinks(index, tmp_path, activity_no_views):
    """[[noteB]] appearing 3 times → 1 edge, weight=1 (extractor dedups before
    counting). Self-edge [[noteA]] in noteA dropped."""
    _seed(
        index, tmp_path, "noteA.md",
        content="See [[noteB]]. Also [[noteB]]. And [[noteB]]. And [[noteA]] (self).",
        id="a", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )
    _seed(
        index, tmp_path, "noteB.md",
        content="body",
        id="b", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )

    graph = await build_graph(index, None, activity_no_views, cluster=False)

    assert len(graph["edges"]) == 1
    e = graph["edges"][0]
    assert e["source"] == "noteA.md"
    assert e["target"] == "noteB.md"
    # Wikilink extractor dedups per-note, so a re-mentioned link contributes
    # one edge weight. (Cross-note repetitions would accumulate.)
    assert e["weight"] == 1


@pytest.mark.asyncio
async def test_graph_drops_unresolved_wikilinks(index, tmp_path, activity_no_views):
    """[[doesNotExist]] in a note should not appear as an edge."""
    _seed(
        index, tmp_path, "noteA.md",
        content="dangling [[doesNotExist]] link",
        id="a", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )

    graph = await build_graph(index, None, activity_no_views, cluster=False)
    assert graph["edges"] == []


@pytest.mark.asyncio
async def test_graph_includes_viewcount_from_activity(index, tmp_path):
    """Heatmap data from VaultActivity populates node.viewCount."""
    _seed(
        index, tmp_path, "x.md",
        content="body",
        id="x", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )
    _seed(
        index, tmp_path, "y.md",
        content="body",
        id="y", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )

    activity = MagicMock()
    activity.top_n_views = AsyncMock(return_value=[{"path": "x.md", "score": 5}])

    graph = await build_graph(index, None, activity, cluster=False, heatmap="30d")

    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    assert nodes_by_id["x.md"]["viewCount"] == 5
    assert nodes_by_id["y.md"]["viewCount"] == 0
    activity.top_n_views.assert_awaited_once_with(limit=1000, window="30d")


@pytest.mark.asyncio
async def test_graph_skips_clusters_when_embeddings_unavailable(index, tmp_path, activity_no_views):
    """vault_embeddings=None → clusters=[], cluster_id stays None on nodes."""
    for i in range(3):
        _seed(
            index, tmp_path, f"n{i}.md",
            content=f"body {i}",
            id=str(i), type="note", agent="sparky", date="2026-05-14T15:00:00Z",
        )

    graph = await build_graph(index, None, activity_no_views, cluster=True)

    assert graph["clusters"] == []
    assert all(n["cluster_id"] is None for n in graph["nodes"])


@pytest.mark.asyncio
async def test_graph_skips_clusters_when_qdrant_scroll_raises(index, tmp_path, activity_no_views):
    """Qdrant scroll throws → cluster fail-soft to empty clusters."""
    for i in range(3):
        _seed(
            index, tmp_path, f"n{i}.md",
            content=f"body {i}",
            id=str(i), type="note", agent="sparky", date="2026-05-14T15:00:00Z",
        )

    qdrant = MagicMock()
    qdrant.scroll = AsyncMock(side_effect=RuntimeError("Qdrant down"))
    embeddings = SimpleNamespace(qdrant=qdrant, collection="memory_vault")

    graph = await build_graph(index, embeddings, activity_no_views, cluster=True)

    assert graph["clusters"] == []
    assert all(n["cluster_id"] is None for n in graph["nodes"])


@pytest.mark.asyncio
async def test_graph_clusters_when_embeddings_available(index, tmp_path, activity_no_views):
    """Six nodes with two obvious clusters in vector-space → k-means picks k≥2
    and assigns cluster_id to every node. Two cluster groups present."""
    paths = [f"cluster{i}.md" for i in range(6)]
    for p in paths:
        _seed(
            index, tmp_path, p,
            content="body",
            id=p, type="note", agent="sparky", date="2026-05-14T15:00:00Z",
        )

    # Two well-separated 8-d clusters: first 3 around 0, last 3 around 10.
    vectors = [
        [0.0] * 8,
        [0.1] * 8,
        [0.0, 0.1, 0.0, 0.1, 0.0, 0.1, 0.0, 0.1],
        [10.0] * 8,
        [10.1] * 8,
        [10.0, 10.1, 10.0, 10.1, 10.0, 10.1, 10.0, 10.1],
    ]

    # Mock Qdrant scroll: single page of 6 records, then empty (or
    # next_offset=None to terminate the loop).
    records = [
        SimpleNamespace(id=p, payload={"path": p}, vector=v)
        for p, v in zip(paths, vectors)
    ]
    qdrant = MagicMock()
    qdrant.scroll = AsyncMock(return_value=(records, None))
    embeddings = SimpleNamespace(qdrant=qdrant, collection="memory_vault")

    graph = await build_graph(index, embeddings, activity_no_views, cluster=True)

    assert len(graph["clusters"]) >= 2  # two obvious groups
    assert all(n["cluster_id"] is not None for n in graph["nodes"])
    # Cluster IDs across the 3-first-paths and 3-last-paths should differ.
    cid_by_path = {n["id"]: n["cluster_id"] for n in graph["nodes"]}
    first_group = {cid_by_path[p] for p in paths[:3]}
    second_group = {cid_by_path[p] for p in paths[3:]}
    assert first_group.isdisjoint(second_group)
    # Each cluster entry has centroid + member_paths.
    for c in graph["clusters"]:
        assert "centroid" in c and isinstance(c["centroid"], list)
        assert "member_paths" in c and len(c["member_paths"]) > 0


@pytest.mark.asyncio
async def test_graph_stats_and_built_at_present(index, tmp_path, activity_no_views):
    """stats dict includes nodes/edges/clusters/build_ms; built_at is ISO8601 Z."""
    _seed(
        index, tmp_path, "a.md",
        content="body",
        id="a", type="note", agent="sparky", date="2026-05-14T15:00:00Z",
    )

    graph = await build_graph(index, None, activity_no_views, cluster=False)
    stats = graph["stats"]
    assert stats["nodes"] == 1
    assert stats["edges"] == 0
    assert stats["clusters"] == 0
    assert isinstance(stats["build_ms"], int)
    assert stats["build_ms"] >= 0
    assert "T" in graph["built_at"] and graph["built_at"].endswith("Z")
