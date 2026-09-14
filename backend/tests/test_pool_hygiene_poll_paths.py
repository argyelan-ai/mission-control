"""Pool hygiene on the poll path — regression tests for the 2026-09-14
incident (29/30 pool connections pinned in open transactions, API unresponsive,
HTTP 000 after 8s, pool timeout never got a chance to bite).

Contract under test:
  1. A poll request must NOT hold an open DB transaction across an `await`
     that is not itself database work (embedding/Qdrant HTTP). Artificial
     delay inside that await → the pool connection must already be released.
  2. Gegenrichtung: a slow-but-healthy semantic-memory call (longer than any
     normal one, far below the pool-timeout dimension) still completes — the
     fix releases the transaction, it does not cut requests off.
  3. Observability: a session returned with an OPEN transaction after the
     warn threshold is logged with endpoint + duration (the leak signature).
  4. Pool timeout: when no connection frees within db_pool_timeout, the
     request fails with an error (logged, with duration) instead of binding
     a connection forever.
"""
import asyncio
import logging
import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.config import settings
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from tests.conftest import test_engine


async def _make_board_and_agent(session: AsyncSession):
    board = Board(name=f"B-{uuid.uuid4().hex[:6]}", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Poller-{uuid.uuid4().hex[:6]}",
        agent_runtime="host",
        agent_token_hash=token_hash,
        board_id=board.id,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return board, agent, raw_token


async def _make_inbox_task(session: AsyncSession, *, board: Board, agent: Agent) -> Task:
    task = Task(
        board_id=board.id,
        assigned_agent_id=agent.id,
        title=f"Pool hygiene probe {uuid.uuid4().hex[:6]}",
        status="inbox",
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


class _PoolProbe:
    """Tracks how many pool connections are currently checked out."""

    def __init__(self, engine):
        self.held = 0
        event.listen(engine.sync_engine, "checkout", self._on_checkout)
        event.listen(engine.sync_engine, "checkin", self._on_checkin)

    def _on_checkout(self, *args, **kwargs):
        self.held += 1

    def _on_checkin(self, *args, **kwargs):
        self.held -= 1

    def detach(self, engine):
        event.remove(engine.sync_engine, "checkout", self._on_checkout)
        event.remove(engine.sync_engine, "checkin", self._on_checkin)


class _ListHandler(logging.Handler):
    def __init__(self, records):
        super().__init__()
        self.records = records

    def emit(self, record):
        self.records.append(record)


def _capture(logger_name: str, level=logging.ERROR):
    """Context manager capturing log records for one logger."""
    logger = logging.getLogger(logger_name)
    records: list = []
    handler = _ListHandler(records)
    handler.setLevel(level)

    class _Ctx:
        def __enter__(self):
            logger.addHandler(handler)
            return records

        def __exit__(self, *exc):
            logger.removeHandler(handler)
            return False

    return _Ctx()


@pytest.mark.asyncio
async def test_poll_releases_connection_during_semantic_memory_await(
    client: AsyncClient, async_session
):
    """Incident reproduction: artificial delay in the semantic-memory await
    (embedding + Qdrant — NOT database work) must find the pool connection
    already released. Sabotage: drop the `session.commit()` in
    _load_dispatch_context → the read transaction is still open here → red."""
    board, agent, token = await _make_board_and_agent(async_session)
    await _make_inbox_task(async_session, board=board, agent=agent)

    probe = _PoolProbe(test_engine)
    observed: dict = {}

    async def _slow_semantic_query(*args, **kwargs):
        # The artificial "await that is not database work". By the time we
        # run, the poll path must have committed (released) its read txn.
        observed["held_during_await"] = probe.held
        await asyncio.sleep(0.2)
        return {"results": {}, "fallback": True}

    try:
        with patch(
            "app.services.memory_query.run_memory_query",
            side_effect=_slow_semantic_query,
        ):
            resp = await client.get(
                "/api/v1/agent/me/poll",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        probe.detach(test_engine)

    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "new_task", resp.json()
    assert "held_during_await" in observed, "semantic memory query was never called"
    assert observed["held_during_await"] == 0, (
        "pool connection still checked out (transaction open) during the "
        f"non-DB await — held={observed['held_during_await']}"
    )


@pytest.mark.asyncio
async def test_slow_but_healthy_semantic_memory_still_completes(
    client: AsyncClient, async_session
):
    """Gegenrichtung: the fix must not cut off slow-but-healthy calls. A
    semantic-memory query taking 1.5s (well beyond normal, still far below
    the 5s pool-timeout dimension) flows through and the task is delivered."""
    board, agent, token = await _make_board_and_agent(async_session)
    task = await _make_inbox_task(async_session, board=board, agent=agent)

    async def _slow_semantic_query(*args, **kwargs):
        await asyncio.sleep(1.5)
        return {"results": {}, "fallback": True}

    with patch(
        "app.services.memory_query.run_memory_query",
        side_effect=_slow_semantic_query,
    ):
        resp = await client.get(
            "/api/v1/agent/me/poll",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(task.id)
    assert body["task"]["prompt"]


@pytest.mark.asyncio
async def test_open_transaction_on_return_is_logged_with_endpoint_and_duration(
    client: AsyncClient, async_session, caplog
):
    """Observability DoD: a connection returned with an open transaction after
    the warn threshold must appear in the log with endpoint + duration — the
    next incident is visible without psql."""
    board, agent, token = await _make_board_and_agent(async_session)

    # Idle agent → poll does read-only work, commits nothing → the request
    # session is returned with an open transaction (the leak signature).
    mp = pytest.MonkeyPatch()
    mp.setattr(settings, "db_session_hold_warn_seconds", 0.0)
    try:
        with caplog.at_level(logging.INFO, logger="mc.database"):
            resp = await client.get(
                "/api/v1/agent/me/poll",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        mp.undo()

    assert resp.status_code == 200
    leak_records = [
        r for r in caplog.records if "OPEN transaction" in r.getMessage()
    ]
    assert leak_records, "no OPEN-transaction warning logged"
    msg = leak_records[-1].getMessage()
    assert "/api/v1/agent/me/poll" in msg, msg
    assert "after" in msg, msg  # duration present


@pytest.mark.asyncio
async def test_pool_checkout_timeout_fails_fast_and_logs():
    """DoD: a hanging request must end in an ERROR, not bind a connection
    forever. With a 1-connection pool and the only connection held, a
    managed_session user hits the checkout timeout (logged with duration)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import AsyncAdaptedQueuePool

    from app.database import managed_session

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        holder = AsyncSession(engine, expire_on_commit=False)
        await holder.exec(text("SELECT 1"))  # holds the only connection

        victim = AsyncSession(engine, expire_on_commit=False)
        with _capture("mc.database", logging.ERROR) as records:
            with pytest.raises(SQLAlchemyPoolTimeoutError):
                async with managed_session(victim, route="/test/pool-timeout"):
                    await victim.exec(text("SELECT 1"))
        await holder.close()
        timeout_logs = [r for r in records if "TIMEOUT" in r.getMessage()]
        assert timeout_logs, "pool checkout timeout was not logged"
        assert "/test/pool-timeout" in timeout_logs[0].getMessage()
        assert "after" in timeout_logs[0].getMessage()
    finally:
        await engine.dispose()


def test_engine_uses_configured_pool_timeout():
    """The engine must actually carry the configured pool timeout — the
    guard from the incident is only real if the pool enforces it."""
    from app.database import engine

    assert engine.pool.timeout() == pytest.approx(settings.db_pool_timeout)
    assert settings.db_pool_timeout == pytest.approx(5.0)
