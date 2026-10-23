"""W3-A — build ghost-edges from top-K embedding similarity.

Used by /vault/graph to augment the wikilink-derived edges with implicit
similarity edges. Rendered dashed at lower opacity in the frontend.

``similarity_edges_from_vectors`` is what the graph uses: the graph build
already holds every note vector (one Qdrant scroll), so the neighbours are
computed locally in one matrix pass — milliseconds instead of one Qdrant
round trip per note (~78 s for ~1,900 notes, round-3 user test t4 S2).
"""

from __future__ import annotations

from collections import Counter

# Rows per matrix block. Bounds memory at block x n floats (512 x 10k notes
# = ~20 MB) so a much larger vault cannot blow up the backend.
_BLOCK_ROWS = 512


def similarity_edges_from_vectors(
    vectors_by_path: dict[str, list[float]],
    *,
    top_k: int = 3,
    min_score: float = 0.72,
) -> list[dict]:
    """Top-K cosine neighbours per note, from vectors already in memory.

    Same rules as a Qdrant top-K search on memory_vault (cosine distance):
    for each note its ``top_k`` nearest other notes with
    score >= ``min_score``; a↔b is canonicalised lexicographically and keeps
    the higher score. Zero vectors and vectors with a different length than
    the majority are skipped instead of failing the graph.
    """
    if top_k <= 0 or len(vectors_by_path) < 2:
        return []
    import numpy as np

    dims = Counter(len(v) for v in vectors_by_path.values() if v)
    if not dims:
        return []
    dim = dims.most_common(1)[0][0]
    paths = [p for p, v in vectors_by_path.items() if v and len(v) == dim]
    if len(paths) < 2:
        return []
    mat = np.asarray([vectors_by_path[p] for p in paths], dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1)
    keep = norms > 0
    if not keep.all():
        paths = [p for p, k in zip(paths, keep) if k]
        mat, norms = mat[keep], norms[keep]
    n = len(paths)
    if n < 2:
        return []
    mat = mat / norms[:, None]
    k = min(top_k, n - 1)

    edges: dict[tuple[str, str], float] = {}
    for start in range(0, n, _BLOCK_ROWS):
        block = mat[start:start + _BLOCK_ROWS] @ mat.T  # (rows, n) cosine
        rows = np.arange(block.shape[0])
        block[rows, rows + start] = -np.inf  # never your own neighbour
        top = np.argpartition(-block, k - 1, axis=1)[:, :k]
        for r in range(block.shape[0]):
            src = paths[start + r]
            for c in top[r]:
                score = float(block[r, c])
                if score < min_score:
                    continue
                a, b = sorted((src, paths[int(c)]))
                if score > edges.get((a, b), 0.0):
                    edges[(a, b)] = score
    return [
        {"source": a, "target": b, "weight": w, "kind": "similarity"}
        for (a, b), w in edges.items()
    ]

