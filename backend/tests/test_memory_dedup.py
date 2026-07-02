"""Phase 5 — MSY-02 hash + cosine dedup: full test bodies (Plan 05-05).

Bodies replace the Wave-0 xfail stubs:
- ``test_identical_hash_skipped``    : POST /knowledge with identical content
                                       returns the existing row (no duplicate
                                       written + INFO log).
- ``test_cosine_high_sets_merge_candidate`` : when Qdrant.query returns a hit
                                       with score >= settings.memory_merge_threshold
                                       (default 0.9), the new row gets
                                       merge_candidate_id pointing at it.
- ``test_cosine_low_no_merge_candidate``    : when score < threshold, the new
                                       row's merge_candidate_id stays NULL.

Patch sites:
- ``app.services.embedding_service.embedding_service.embed`` — return a fake
  768-dim vector so index_memory progresses past the embed step.
- ``app.services.qdrant_service.qdrant_service.query`` — return controlled
  hits to drive the cosine threshold branches.
- ``app.services.qdrant_service.qdrant_service.upsert`` — no-op so the test
  doesn't actually push to a real Qdrant instance.
"""
import uuid

import pytest
from unittest.mock import AsyncMock, patch
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.models.memory import BoardMemory
from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_identical_hash_skipped(auth_client):
    """MSY-02 D-05: POST /knowledge with identical title+content twice
    returns the existing entry on the second call (silent dedup) — no
    duplicate row is written.
    """
    payload = {
        "content": "duplicate content here",
        "title": "Same",
        "memory_type": "knowledge",
    }
    r1 = await auth_client.post("/api/v1/knowledge", json=payload)
    assert r1.status_code in (200, 201), r1.text
    first_id = r1.json()["id"]

    r2 = await auth_client.post("/api/v1/knowledge", json=payload)
    assert r2.status_code in (200, 201), r2.text
    # Silent dedup: the second POST returns the existing entry's id.
    assert r2.json()["id"] == first_id

    # Verify only ONE row exists in the DB
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (
            await s.exec(select(BoardMemory).where(BoardMemory.title == "Same"))
        ).all()
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"


@pytest.mark.asyncio
async def test_cosine_high_sets_merge_candidate(auth_client):
    """MSY-02 D-06: when Qdrant.query returns a top hit with score >=
    settings.memory_merge_threshold (default 0.9), the new entry's
    merge_candidate_id is populated with the hit's memory_id.
    """
    # Seed an existing entry that will be the cosine-near-duplicate target.
    existing_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(
            BoardMemory(
                id=existing_id,
                content="seed for similarity",
                title="Seed",
                source="user",
                memory_type="knowledge",
                content_hash="seed-fake-hash",  # any non-colliding string
            ),
        )
        await s.commit()

    fake_hits = [{"memory_id": str(existing_id), "score": 0.95, "payload": {}}]
    # Patch app.database.engine → test_engine so the fresh AsyncSession opened
    # inside index_memory's merge_candidate-flagging branch uses the SQLite
    # test DB (otherwise it would try to connect to the real PostgreSQL via
    # the conftest's placeholder ``postgresql+asyncpg://test:test@.../test``
    # URL and fail with role-not-exist). Same pattern as
    # test_embedding_retry_queue.py:118.
    with patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(return_value=[0.1] * 768),
    ), patch(
        "app.services.qdrant_service.qdrant_service.query",
        new=AsyncMock(return_value=fake_hits),
    ), patch(
        "app.services.qdrant_service.qdrant_service.upsert",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.database.engine",
        test_engine,
    ):
        resp = await auth_client.post(
            "/api/v1/knowledge",
            json={
                "content": "different but topically similar text",
                "title": "Sim",
                "memory_type": "knowledge",
            },
        )
    assert resp.status_code in (200, 201), resp.text
    new_id = resp.json()["id"]
    assert new_id != str(existing_id), "new entry must NOT silently dedup; cosine path"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        new_entry = await s.get(BoardMemory, uuid.UUID(new_id))
    assert new_entry is not None
    assert new_entry.merge_candidate_id is not None, (
        "expected merge_candidate_id to be set when cosine ≥ threshold"
    )
    assert str(new_entry.merge_candidate_id) == str(existing_id)


@pytest.mark.asyncio
async def test_cosine_low_no_merge_candidate(auth_client):
    """MSY-02 D-06: when Qdrant.query returns a top hit with score below
    settings.memory_merge_threshold, the new entry's merge_candidate_id
    stays NULL — the cosine signal is too weak to flag.
    """
    fake_hits = [
        {"memory_id": str(uuid.uuid4()), "score": 0.5, "payload": {}}
    ]
    with patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(return_value=[0.1] * 768),
    ), patch(
        "app.services.qdrant_service.qdrant_service.query",
        new=AsyncMock(return_value=fake_hits),
    ), patch(
        "app.services.qdrant_service.qdrant_service.upsert",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.database.engine",
        test_engine,
    ):
        resp = await auth_client.post(
            "/api/v1/knowledge",
            json={
                "content": "totally unique content xyz",
                "title": "Unique",
                "memory_type": "knowledge",
            },
        )
    assert resp.status_code in (200, 201), resp.text
    new_id = resp.json()["id"]

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        new_entry = await s.get(BoardMemory, uuid.UUID(new_id))
    assert new_entry is not None
    assert new_entry.merge_candidate_id is None, (
        "expected merge_candidate_id to remain NULL when cosine < threshold"
    )
