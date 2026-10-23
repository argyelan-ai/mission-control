"""Memory graph speed (round-3 user test R4 / t4 S2).

Two causes made the Graph tab show "Building graph…" for ~78 s after every
new note:

1. Similarity ("ghost") edges were found with one Qdrant query per note —
   ~1,900 sequential round trips. The vectors are already in memory after
   the scroll, so the neighbours are now computed locally in one pass.
2. Concurrent requests for the same vault version each built the graph on
   their own (the backend log showed five cold builds of one version).
   The endpoint now builds once per (version, params) and every waiting
   request gets that one result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest



@pytest.mark.asyncio
async def test_build_graph_does_not_query_qdrant_per_note(tmp_path):
    """build_graph must not issue one Qdrant query per note any more — the
    scroll already delivered every vector."""
    import frontmatter

    from app.services.vault_graph import build_graph
    from app.services.vault_index import VaultIndex

    index = VaultIndex(db_path=tmp_path / "i.db", vault_path=tmp_path)
    paths = ["a.md", "b.md", "c.md"]
    for p in paths:
        f = tmp_path / p
        f.write_text(frontmatter.dumps(frontmatter.Post("body", id=p, type="note", agent="x")))
        index.upsert(f, frontmatter.load(str(f)))

    vectors = {"a.md": [1.0, 0.0], "b.md": [0.99, 0.1], "c.md": [0.0, 1.0]}
    records = [SimpleNamespace(id=p, payload={"path": p}, vector=v) for p, v in vectors.items()]
    qdrant = MagicMock()
    qdrant.scroll = AsyncMock(return_value=(records, None))
    qdrant.query_points = AsyncMock(side_effect=AssertionError("per-note query"))
    embeddings = SimpleNamespace(qdrant=qdrant, collection="memory_vault")
    activity = MagicMock()
    activity.top_n_views = AsyncMock(return_value=[])

    graph = await build_graph(index, embeddings, activity, cluster=False, similarity_edges=True)

    qdrant.query_points.assert_not_called()
    sim = [e for e in graph["edges"] if e.get("kind") == "similarity"]
    assert [(e["source"], e["target"]) for e in sim] == [("a.md", "b.md")]


# ── Single flight in the endpoint ─────────────────────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _request():
    state = SimpleNamespace(
        vault_index=object(), vault_embeddings=None, vault_activity=object()
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_five_concurrent_graph_requests_build_once(monkeypatch):
    from app.routers import vault as vault_router

    redis = _FakeRedis()
    monkeypatch.setattr(vault_router, "get_redis", AsyncMock(return_value=redis))
    calls = 0

    async def slow_build(*_a, **_kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"nodes": [{"id": "a.md"}], "edges": [], "clusters": [], "stats": {"build_ms": 50}}

    monkeypatch.setattr(vault_router, "build_graph", slow_build)

    results = await asyncio.gather(*[
        vault_router.get_graph(
            _request(), cluster=False, heatmap="30d", similarity_edges=True, no_cache=False
        )
        for _ in range(5)
    ])

    assert calls == 1
    assert all(r["nodes"] == [{"id": "a.md"}] for r in results)
    # The single build was cached for the next visitor.
    assert len(redis.store) == 1


@pytest.mark.asyncio
async def test_failed_build_is_not_shared_with_the_next_request(monkeypatch):
    """A crash in one build must not poison later requests for that version."""
    from app.routers import vault as vault_router

    redis = _FakeRedis()
    monkeypatch.setattr(vault_router, "get_redis", AsyncMock(return_value=redis))
    attempts = 0

    async def flaky_build(*_a, **_kw):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        return {"nodes": [], "edges": [], "clusters": [], "stats": {"build_ms": 1}}

    monkeypatch.setattr(vault_router, "build_graph", flaky_build)

    with pytest.raises(RuntimeError):
        await vault_router.get_graph(
            _request(), cluster=False, heatmap="30d", similarity_edges=True, no_cache=False
        )
    ok = await vault_router.get_graph(
        _request(), cluster=False, heatmap="30d", similarity_edges=True, no_cache=False
    )
    assert ok["nodes"] == []
    assert attempts == 2


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_the_shared_build(monkeypatch):
    """A browser that navigates away cancels its request. The build keeps
    running for the other waiters and still lands in the cache."""
    from app.routers import vault as vault_router

    redis = _FakeRedis()
    monkeypatch.setattr(vault_router, "get_redis", AsyncMock(return_value=redis))
    calls = 0

    async def slow_build(*_a, **_kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"nodes": [{"id": "x.md"}], "edges": [], "clusters": [], "stats": {"build_ms": 50}}

    monkeypatch.setattr(vault_router, "build_graph", slow_build)

    def call():
        return vault_router.get_graph(
            _request(), cluster=False, heatmap="30d", similarity_edges=True, no_cache=False
        )

    first = asyncio.ensure_future(call())
    second = asyncio.ensure_future(call())
    await asyncio.sleep(0.01)
    first.cancel()
    result = await second
    assert result["nodes"] == [{"id": "x.md"}]
    assert calls == 1
    assert len(redis.store) == 1
