"""ADR-096 §1.1 / §2 on real Postgres: the membership writer is serialised by a
row lock on the runtime, and the startup healer re-reads under that lock.

Why Postgres only: SQLite has no ``SELECT … FOR UPDATE`` and the SQLite lane
shares one connection between sessions — two real transactions, and with
them the race from the PR #893 review, only exist here.

The race (review on #893): the healer loaded every runtime once (stale
``topology`` with the legacy ``worker_host_id`` = W1) and read the member rows
fresh. An operator start of the same duo with a NEW worker W2 committed
``topology`` without the legacy key and wrote (head, W2) in between. The
healer then wrote (head, W1) back over it — DB says W1, the engine runs on W2.

Test data: box-a / box-b / box-c, RFC 5737 addresses only. No network.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services import runtime_membership as rm
from tests.conftest import test_engine
from tests.test_recipe_switcher_p3 import _host, _runtime

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


def _new_session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


async def _rows(runtime_id) -> list[tuple[str, int, object]]:
    async with _new_session() as s:
        rows = (
            await s.exec(select(RuntimeHost).where(RuntimeHost.runtime_id == runtime_id))
        ).all()
        return sorted((r.role, r.node_rank, r.host_id) for r in rows)


async def _boxes(session):
    a = await _host(session, "box-a")
    b = await _host(session, "box-b", ssh_host="192.0.2.11")
    c = await _host(session, "box-c", ssh_host="192.0.2.12")
    return a, b, c


async def test_set_members_holds_a_row_lock_on_the_runtime_until_commit(session):
    """``FOR NO KEY UPDATE NOWAIT`` conflicts with ``FOR UPDATE`` but NOT with
    the ``FOR KEY SHARE`` an FK insert into ``runtime_hosts`` takes on its
    own — so this only fails when ``set_members`` really locks the row."""
    a, b, _ = await _boxes(session)
    rt = await _runtime(session, "duo", a, topology={"nodes": 2})

    async with _new_session() as writer, _new_session() as observer:
        runtime = await writer.get(Runtime, rt.id)
        assert await rm.set_members(
            writer, runtime, rm.duo(a.id, b.id), running=False, commit=False
        )
        with pytest.raises(DBAPIError) as exc:
            await observer.exec(
                text("SELECT id FROM runtimes WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
                params={"id": rt.id},
            )
        assert "lock" in str(exc.value).lower()
        await observer.rollback()

        await writer.commit()
        await observer.exec(
            text("SELECT id FROM runtimes WHERE id = :id FOR NO KEY UPDATE NOWAIT"),
            params={"id": rt.id},
        )
        await observer.rollback()


async def test_two_writers_of_one_runtime_are_serialised_not_colliding(session):
    """Second writer waits for the first, then sees ITS rows and replaces them.

    Without the lock the second writer reads the (still empty) committed set,
    inserts rank 0, blocks on the unique index and dies with an
    IntegrityError the moment the first commits."""
    a, b, c = await _boxes(session)
    rt = await _runtime(session, "duo", a, topology={"nodes": 2})

    async with _new_session() as first, _new_session() as second:
        rt_first = await first.get(Runtime, rt.id)
        rt_second = await second.get(Runtime, rt.id)
        await rm.set_members(first, rt_first, rm.duo(a.id, b.id), running=False, commit=False)

        pending = asyncio.create_task(
            rm.set_members(second, rt_second, rm.duo(a.id, c.id), running=False)
        )
        done, _ = await asyncio.wait({pending}, timeout=0.5)
        assert not done, "second writer did not wait for the first one's lock"

        await first.commit()
        assert await asyncio.wait_for(pending, timeout=10) is True

    assert await _rows(rt.id) == [("head", 0, a.id), ("worker", 1, c.id)]


async def test_startup_audit_does_not_write_back_a_stale_worker_over_a_new_start(session):
    """The review race, replayed. The audit has already loaded the legacy duo
    (topology says W1, no rows). While it works, an operator start of the same
    duo with worker W2 commits ``topology`` without the legacy key and then
    writes (head, W2). The audit must not end with W1 in the table.

    The start is injected right after the audit's first read (``_recipe_for``,
    which the old code called between reading the rows and writing them). It
    runs as its own task: with the fix the audit may already hold the row lock
    — then the start simply waits (bounded here) and runs after the audit."""
    a, b, c = await _boxes(session)
    rt = await _runtime(
        session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )

    async def operator_start() -> None:
        async with _new_session() as s:
            runtime = await s.get(Runtime, rt.id)
            runtime.topology = rm.without_legacy_worker(dict(runtime.topology))
            s.add(runtime)
            await s.commit()
            await rm.set_members(s, runtime, rm.duo(a.id, c.id), running=False)

    start_task: list[asyncio.Task] = []
    real_recipe_for = rm._recipe_for

    async def recipe_for_with_race(sess, runtime):
        if not start_task:
            start_task.append(asyncio.create_task(operator_start()))
            await asyncio.wait({start_task[0]}, timeout=0.5)
        return await real_recipe_for(sess, runtime)

    async with _new_session() as audit_session:
        with (
            patch.object(rm, "_recipe_for", recipe_for_with_race),
            patch.object(rm, "is_running", AsyncMock(return_value=False)),
        ):
            await rm.membership_audit(audit_session, repair=True)
            assert start_task, "the race was never injected"
            await asyncio.wait_for(start_task[0], timeout=10)

    assert await _rows(rt.id) == [("head", 0, a.id), ("worker", 1, c.id)]


async def test_the_repair_decides_on_the_row_it_locked_not_on_its_first_read(session):
    """The topology half of the race: between the audit's first read and its
    lock, a start of the same duo drops the legacy worker field (its member
    rows come later). The repair must decide on the locked, re-read row —
    no legacy field any more → nothing to fill, no W1 rows, no event saying
    box-b is now in use."""
    from app.models.activity import ActivityEvent

    a, b, _ = await _boxes(session)
    rt = await _runtime(
        session, "legacy-duo", a, topology={"nodes": 2, "worker_host_id": str(b.id)}
    )

    async def drop_legacy_field() -> None:
        async with _new_session() as s:
            runtime = await s.get(Runtime, rt.id)
            runtime.topology = rm.without_legacy_worker(dict(runtime.topology))
            s.add(runtime)
            await s.commit()

    injected: list[bool] = []
    real_recipe_for = rm._recipe_for

    async def recipe_for_with_race(sess, runtime):
        if not injected:
            injected.append(True)
            await drop_legacy_field()  # no lock held yet on the first read
        return await real_recipe_for(sess, runtime)

    async with _new_session() as audit_session:
        with patch.object(rm, "_recipe_for", recipe_for_with_race):
            findings = await rm.membership_audit(audit_session, repair=True)

    assert injected
    assert [f.code for f in findings] == ["members_incomplete"]
    assert await _rows(rt.id) == []
    async with _new_session() as s:
        events = (
            await s.exec(
                select(ActivityEvent).where(
                    ActivityEvent.event_type == "runtime.members_repaired"
                )
            )
        ).all()
    assert events == []
