"""Similarity ("ghost") edges for the memory graph (W3-A).

The graph build already holds every note vector after one Qdrant scroll, so
the top-K cosine neighbours are computed locally in one matrix pass. The
earlier one-Qdrant-query-per-note path took ~78 s for ~1,900 notes (round-3
user test t4 S2) and is gone.
"""

from __future__ import annotations

import math
import random
import time

import pytest

from app.services.vault_similarity_edges import similarity_edges_from_vectors


# ── Local similarity edges ───────────────────────────────────────────────────


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def test_local_edges_match_cosine_top_k_and_threshold():
    """a≈b (cos≈0.99), c is orthogonal → one a↔b edge, nothing to c."""
    vectors = {
        "a.md": [1.0, 0.0, 0.0],
        "b.md": [0.99, 0.14, 0.0],
        "c.md": [0.0, 0.0, 1.0],
    }
    edges = similarity_edges_from_vectors(vectors, top_k=3, min_score=0.72)
    assert len(edges) == 1
    e = edges[0]
    assert (e["source"], e["target"]) == ("a.md", "b.md")
    assert e["kind"] == "similarity"
    expected = sum(x * y for x, y in zip(_unit(vectors["a.md"]), _unit(vectors["b.md"])))
    assert e["weight"] == pytest.approx(expected, abs=1e-4)


def test_local_edges_respect_top_k_per_note():
    """Five near-identical notes, top_k=1 → each note keeps only its single
    best neighbour; no note gets more than (its own 1 + incoming) edges and
    the total is far below the full 10-pair clique."""
    base = [1.0, 0.0, 0.0, 0.0]
    vectors = {
        f"n{i}.md": [1.0, 0.01 * i, 0.0, 0.0] for i in range(5)
    }
    vectors["n0.md"] = base
    edges = similarity_edges_from_vectors(vectors, top_k=1, min_score=0.5)
    assert 1 <= len(edges) <= 5
    assert all(e["source"] < e["target"] for e in edges)


def test_local_edges_empty_and_single():
    assert similarity_edges_from_vectors({}, top_k=3, min_score=0.7) == []
    assert similarity_edges_from_vectors({"a.md": [1.0, 0.0]}, top_k=3, min_score=0.7) == []


def test_local_edges_ignore_zero_and_ragged_vectors():
    """A zero vector or one with the wrong length must not crash the build."""
    vectors = {
        "a.md": [1.0, 0.0],
        "b.md": [1.0, 0.01],
        "zero.md": [0.0, 0.0],
        "ragged.md": [1.0, 0.0, 0.0],
    }
    edges = similarity_edges_from_vectors(vectors, top_k=3, min_score=0.72)
    pairs = {(e["source"], e["target"]) for e in edges}
    assert pairs == {("a.md", "b.md")}


def test_local_edges_2000_notes_under_two_seconds():
    """The live vault has ~1,900 notes at 768 dims. The old path needed
    ~78 s; the local pass must stay well under 2 s."""
    rng = random.Random(7)
    vectors = {
        f"note-{i}.md": [rng.uniform(-1, 1) for _ in range(768)] for i in range(2000)
    }
    t0 = time.perf_counter()
    similarity_edges_from_vectors(vectors, top_k=3, min_score=0.72)
    assert time.perf_counter() - t0 < 2.0


def test_higher_score_kept_once_per_pair():
    """a↔b is found from both sides — one edge, canonical order, no self-edge."""
    vectors = {"b.md": [1.0, 0.05], "a.md": [1.0, 0.0]}
    edges = similarity_edges_from_vectors(vectors, top_k=3, min_score=0.7)
    assert [(e["source"], e["target"]) for e in edges] == [("a.md", "b.md")]
    assert all(e["source"] != e["target"] for e in edges)
