"""Phase 5 — MSY-04 embedding retry queue + outage resilience.

Test bodies replace the Wave-0 stubs once Plan 05-02 lands the production
modules (`app.services.embedding_retry`, `_enqueue_embedding_retry` in
`memory_indexing.py`, `RedisKeys.embedding_retry`, `embedding_retry_interval`
in Settings, and `is_available` rename on `embedding_service`).

Acceptance for Plan 05-02:
- ``test_outage_enqueues_retry``     — embedding outage during write enqueues
                                       a retry payload; fail-soft preserved.
- ``test_drain_processes_ready_items`` — _drain_once consumes ready items.
- ``test_max_attempts_drop``         — drop counter increments after attempt 8.
- ``test_queue_length_cap``          — MAX_QUEUE_LEN=1000 prevents accumulation.
- ``test_dispatch_unaffected_by_outage`` — Roadmap success criterion 4: dispatch
                                       (here: index_memory) returns within 1s
                                       even when embed() raises.

All tests use the conftest fakeredis fixture (autouse via ``client`` indirectly;
direct tests re-patch ``app.services.embedding_retry.get_redis`` so the module
sees the same fake server) plus AsyncMock to mock embedding_service.embed.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.board import Board
from app.models.memory import BoardMemory
from app.redis_client import RedisKeys
from tests.conftest import test_engine


async def _seed_memory(content: str = "test") -> BoardMemory:
    """Create a Board + BoardMemory(memory_type=knowledge) so it routes to the
    semantic layer in ``layer_for(...)``. Returns the persisted BoardMemory.
    """
    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(
            id=board_id,
            name=f"t-{board_id.hex[:8]}",
            slug=f"t-{board_id.hex[:8]}",
            require_review_before_done=False,
        ))
        await s.commit()
        m = BoardMemory(
            board_id=board_id,
            content=content,
            title="t",
            source="user",
            memory_type="knowledge",
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        return m


@pytest.mark.asyncio
async def test_outage_enqueues_retry(fake_redis):
    """MSY-04 D-17/D-18: embed() raises during BoardMemory write → row lands,
    retry payload appended to mc:embeddings:retry, no exception bubbles up.
    """
    from app.services.memory_indexing import index_memory

    memory = await _seed_memory("retry-please")

    # Patch get_redis at every site that imports it (memory_indexing reaches it
    # via embedding_retry.enqueue -> app.services.embedding_retry.get_redis).
    with patch(
        "app.services.embedding_retry.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(side_effect=ConnectionError("Spark down")),
    ):
        result = await index_memory(memory)

    # index_memory returns None on fail-soft
    assert result is None
    qlen = await fake_redis.llen(RedisKeys.embedding_retry())
    assert qlen == 1, f"Expected 1 enqueued retry, got {qlen}"
    raw = await fake_redis.lindex(RedisKeys.embedding_retry(), 0)
    payload = json.loads(raw)
    assert payload["memory_id"] == str(memory.id)
    assert payload["attempt"] == 1
    assert payload["next_at"] >= int(time.time())  # scheduled in future


@pytest.mark.asyncio
async def test_drain_processes_ready_items(fake_redis):
    """MSY-04 D-18/D-20: ``_drain_once`` consumes a ready item, calls
    ``embedding_service.embed`` + ``qdrant_service.upsert``, removes the item.
    """
    from app.services.embedding_retry import EmbeddingRetryLoop

    memory = await _seed_memory("drain-me")
    payload = json.dumps({
        "memory_id": str(memory.id),
        "attempt": 1,
        "next_at": int(time.time()) - 10,  # ready
    })
    await fake_redis.rpush(RedisKeys.embedding_retry(), payload)

    loop = EmbeddingRetryLoop(interval=99999)  # never auto-fires (Pitfall 4)

    # Patch:
    # - embedding_retry.get_redis            (drain reads/writes the LIST)
    # - embedding_service.is_available       (probe returns True)
    # - embedding_service.embed              (returns a fake vector)
    # - qdrant_service.upsert                (no-op success)
    # - app.database.engine                  (so the AsyncSession in
    #                                         _process_one uses the test engine)
    with patch(
        "app.services.embedding_retry.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.embedding_service.embedding_service.is_available",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(return_value=[0.1] * 768),
    ), patch(
        "app.services.qdrant_service.qdrant_service.upsert",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.database.engine",
        test_engine,
    ):
        processed = await loop._drain_once()

    assert processed == 1, f"Expected 1 processed item, got {processed}"
    assert await fake_redis.llen(RedisKeys.embedding_retry()) == 0


@pytest.mark.asyncio
async def test_max_attempts_drop(fake_redis):
    """MSY-04 D-18: at attempt = MAX_ATTEMPTS the next failure drops with an
    ERROR log and the dropped counter increments by one. The item does NOT
    re-appear on the LIST (no infinite retry).
    """
    from app.services.embedding_retry import (
        EmbeddingRetryLoop,
        MAX_ATTEMPTS,
        get_dropped_total,
    )

    memory = await _seed_memory("never-embeds")
    payload = json.dumps({
        "memory_id": str(memory.id),
        "attempt": MAX_ATTEMPTS,
        "next_at": int(time.time()) - 10,  # ready
    })
    await fake_redis.rpush(RedisKeys.embedding_retry(), payload)
    pre_drop = get_dropped_total()

    loop = EmbeddingRetryLoop(interval=99999)
    with patch(
        "app.services.embedding_retry.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.embedding_service.embedding_service.is_available",
        new=AsyncMock(return_value=True),
    ), patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(side_effect=ConnectionError("still down")),
    ), patch(
        "app.database.engine",
        test_engine,
    ):
        await loop._drain_once()

    assert get_dropped_total() == pre_drop + 1, (
        f"dropped counter must increment by 1; was {pre_drop}, now {get_dropped_total()}"
    )
    assert await fake_redis.llen(RedisKeys.embedding_retry()) == 0, (
        "max-attempts payload must not be re-enqueued — it is dropped"
    )


@pytest.mark.asyncio
async def test_queue_length_cap(fake_redis):
    """Pitfall 3: queue cap at MAX_QUEUE_LEN prevents unbounded growth during
    sustained outage. After cap, ``enqueue`` returns False + LLEN unchanged.
    """
    from app.services.embedding_retry import enqueue, MAX_QUEUE_LEN

    # Fast-prefill: pipeline MAX_QUEUE_LEN dummy entries
    pipe = fake_redis.pipeline()
    for _ in range(MAX_QUEUE_LEN):
        pipe.rpush(
            RedisKeys.embedding_retry(),
            json.dumps({
                "memory_id": str(uuid.uuid4()),
                "attempt": 1,
                "next_at": 0,
            }),
        )
    await pipe.execute()
    assert await fake_redis.llen(RedisKeys.embedding_retry()) == MAX_QUEUE_LEN

    # Patch get_redis so enqueue() sees our pre-filled fake_redis
    with patch(
        "app.services.embedding_retry.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ):
        enqueued = await enqueue(uuid.uuid4(), attempt=1)

    assert enqueued is False, "enqueue must reject when LLEN >= MAX_QUEUE_LEN"
    assert await fake_redis.llen(RedisKeys.embedding_retry()) == MAX_QUEUE_LEN, (
        "LLEN must stay at the cap after rejected enqueue"
    )


@pytest.mark.asyncio
async def test_dispatch_unaffected_by_outage(fake_redis):
    """Roadmap Success Criterion 4 (D-17): the dispatch path NEVER blocks on
    embedding. Concretely: ``index_memory(memory)`` must return within 1s even
    when ``embed()`` raises — the BoardMemory row stays in DB, retry enqueued.
    """
    from app.services.memory_indexing import index_memory

    memory = await _seed_memory("dispatch-while-down")

    with patch(
        "app.services.embedding_retry.get_redis",
        new=AsyncMock(return_value=fake_redis),
    ), patch(
        "app.services.embedding_service.embedding_service.embed",
        new=AsyncMock(side_effect=ConnectionError("down")),
    ):
        # index_memory must complete in well under 1 second even with embed
        # raising. wait_for raises asyncio.TimeoutError if it doesn't.
        result = await asyncio.wait_for(index_memory(memory), timeout=1.0)

    assert result is None  # fail-soft return
    # The BoardMemory row is still in DB (was persisted in _seed_memory; the
    # contract is that index_memory does not delete or mutate it on failure).
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rehydrated = await s.get(BoardMemory, memory.id)
        assert rehydrated is not None
        assert rehydrated.content == "dispatch-while-down"
