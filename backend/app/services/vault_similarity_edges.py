"""W3-A — build ghost-edges from Qdrant top-K similarity.

Used by /vault/graph to augment the wikilink-derived edges with implicit
similarity edges. Rendered dashed at lower opacity in the frontend.

Idempotent + cheap — no LLM calls, just Qdrant search per node."""

from __future__ import annotations

import asyncio
import inspect


def build_similarity_edges(
    qdrant_client,
    nodes: list[dict],
    *,
    top_k: int = 3,
    min_score: float = 0.72,
    collection: str = "memory_vault",
) -> list[dict]:
    """For each node, fetch top_k Qdrant neighbours and emit edges with
    score >= min_score.

    Canonicalises (source, target) lexicographically and deduplicates so a↔b
    appears once. Picks the max score across reciprocal searches.

    Supports both sync (MagicMock in tests) and async (AsyncQdrantClient)
    clients via coroutine detection — matches the pattern in W3-B's
    vault_wikilink_backfill.fetch_top_k_candidates."""
    edges: dict[tuple[str, str], float] = {}
    for n in nodes:
        node_id = n["id"]
        emb = n["embedding"]
        hits = qdrant_client.search(
            collection_name=collection,
            query_vector=emb,
            limit=top_k + 1,
        )
        if inspect.iscoroutine(hits):
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    hits = _await_in_running_loop(hits)
                else:
                    hits = loop.run_until_complete(hits)
            except RuntimeError:
                hits = asyncio.run(hits)
        for hit in hits:
            payload = hit.payload or {}
            target = payload.get("path", "")
            if not target or target == node_id:
                continue
            if hit.score < min_score:
                continue
            a, b = sorted([node_id, target])
            edges[(a, b)] = max(edges.get((a, b), 0.0), hit.score)
    return [
        {"source": a, "target": b, "weight": w, "kind": "similarity"}
        for (a, b), w in edges.items()
    ]


def _await_in_running_loop(coro):
    """Helper for running-loop edge case — only used when called from inside
    an existing async context. Production callers should prefer the async
    variant build_similarity_edges_async."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(asyncio.run, coro)
        return future.result()


async def build_similarity_edges_async(
    qdrant_client,
    nodes: list[dict],
    *,
    top_k: int = 3,
    min_score: float = 0.72,
    collection: str = "memory_vault",
) -> list[dict]:
    """Async variant — production callers use this when qdrant_client is
    AsyncQdrantClient. Sync variant is for tests with MagicMock."""
    edges: dict[tuple[str, str], float] = {}
    for n in nodes:
        node_id = n["id"]
        emb = n["embedding"]
        response = await qdrant_client.query_points(
            collection_name=collection,
            query=emb,
            limit=top_k + 1,
            with_payload=True,
        )
        hits = response.points
        for hit in hits:
            payload = hit.payload or {}
            target = payload.get("path", "")
            if not target or target == node_id:
                continue
            if hit.score < min_score:
                continue
            a, b = sorted([node_id, target])
            edges[(a, b)] = max(edges.get((a, b), 0.0), hit.score)
    return [
        {"source": a, "target": b, "weight": w, "kind": "similarity"}
        for (a, b), w in edges.items()
    ]
