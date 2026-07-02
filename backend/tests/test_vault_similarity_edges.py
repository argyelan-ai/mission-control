import pytest
from unittest.mock import MagicMock
from app.services.vault_similarity_edges import build_similarity_edges


def test_builds_ghost_edges_above_threshold():
    """Top-K neighbours with score >= min_score produce ghost edges,
    deduplicated lexicographically and excluding the source from its own list."""
    qdrant = MagicMock()
    # Each search() call gets back a fixed set of hits — but for a 2-node sim
    # the searches happen twice (once per node).
    # Hit 1: node "a"'s neighbours — sees self + b above threshold + c above
    # Hit 2: node "b"'s neighbours — sees self + a above threshold + c below
    qdrant.search = MagicMock(side_effect=[
        [
            MagicMock(id="self-A", score=1.0, payload={"path": "a"}),
            MagicMock(id="other-B", score=0.85, payload={"path": "b"}),
            MagicMock(id="other-C", score=0.72, payload={"path": "c"}),
        ],
        [
            MagicMock(id="self-B", score=1.0, payload={"path": "b"}),
            MagicMock(id="other-A", score=0.85, payload={"path": "a"}),
            MagicMock(id="other-C", score=0.65, payload={"path": "c"}),  # below threshold
        ],
    ])
    nodes = [
        {"id": "a", "embedding": [0.1] * 768},
        {"id": "b", "embedding": [0.2] * 768},
    ]
    edges = build_similarity_edges(qdrant, nodes, top_k=3, min_score=0.7)
    pairs = {(e["source"], e["target"]) for e in edges}
    assert ("a", "b") in pairs
    assert ("a", "c") in pairs
    # No duplicate of a↔b
    assert len([e for e in edges if {e["source"], e["target"]} == {"a", "b"}]) == 1
    # All marked similarity
    assert all(e["kind"] == "similarity" for e in edges)
    # Weight comes from score
    ab = next(e for e in edges if {e["source"], e["target"]} == {"a", "b"})
    assert ab["weight"] == pytest.approx(0.85)


def test_no_edges_when_all_below_threshold():
    qdrant = MagicMock()
    qdrant.search = MagicMock(return_value=[
        MagicMock(id="other", score=0.5, payload={"path": "other"}),
    ])
    nodes = [{"id": "a", "embedding": [0.1] * 768}]
    edges = build_similarity_edges(qdrant, nodes, top_k=3, min_score=0.7)
    assert edges == []


def test_self_edge_dropped():
    qdrant = MagicMock()
    qdrant.search = MagicMock(return_value=[
        MagicMock(id="self", score=1.0, payload={"path": "a"}),  # same as source
    ])
    nodes = [{"id": "a", "embedding": [0.1] * 768}]
    edges = build_similarity_edges(qdrant, nodes, top_k=3, min_score=0.7)
    assert edges == []


def test_higher_score_wins_when_seen_twice():
    """If a↔b appears in both searches with different scores, keep the max."""
    qdrant = MagicMock()
    qdrant.search = MagicMock(side_effect=[
        [
            MagicMock(id="b", score=0.80, payload={"path": "b"}),
        ],
        [
            MagicMock(id="a", score=0.92, payload={"path": "a"}),
        ],
    ])
    nodes = [
        {"id": "a", "embedding": [0.1] * 768},
        {"id": "b", "embedding": [0.2] * 768},
    ]
    edges = build_similarity_edges(qdrant, nodes, top_k=3, min_score=0.7)
    assert len(edges) == 1
    assert edges[0]["weight"] == pytest.approx(0.92)
