"""W0.11 — a finished bench task must NOT wake the board lead.

Live finding 22.10.2026 (lead transcript, 21.10. 21:56 UTC): the
generic completion callback (`_notify_lead_on_completion`) sent the lead a
"TASK ERLEDIGT — klassifiziere das Ergebnis" DM for `[Bench] Shot Sequence`.
The lead woke up, ran 3 cloud-model turns (78k fresh cache tokens) and answered
"Task only" — 0.71 USD of cloud cost on a run the bench shows as "local".
The bench result is judged by the operator in the Bench Studio, never by the
lead, so the callback is pure cost.

Fix: the bench layer claims the follow-up of its own tasks through a core
hook (`completion_callback_skip_hooks`). Core routing stays unchanged for
every other task.

Covers:
  - bench task (linked BenchEntry) -> no lead DM, no system_notify comment,
    no `owner.completion_callback` event, one `..._skipped` event instead.
  - bench task whose entry was re-pointed by a retry -> still skipped
    (recognized by the auto_reason the bench layer writes on creation).
  - normal task on the same board -> lead DM as before (guard is narrow).
  - a raising skip hook -> callback runs as before (fail-open to old path).
  - the real path: on_task_review -> system_finalize_task_done -> notify.
  - register(app) puts the bench predicate on the registry.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.bench import BenchChallenge, BenchEntry
from app.models.board import Board
from app.models.task import Task, TaskComment
from app.models.thread import Message
from app.verticals import hooks
from tests.conftest import test_engine


@pytest.fixture
def _bench_skip_hook_registered():
    """Process-global registry: register the bench predicate for this test
    only (mirrors register(app)'s `not in` guard, removes what it added)."""
    from app.verticals.bench_studio.callbacks import owns_completion_followup

    added = owns_completion_followup not in hooks.completion_callback_skip_hooks
    if added:
        hooks.completion_callback_skip_hooks.append(owns_completion_followup)
    yield owns_completion_followup
    if added:
        hooks.completion_callback_skip_hooks.remove(owns_completion_followup)


async def _seed_board_with_lead(s: AsyncSession, tag: str):
    board_id, lead_id, worker_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    s.add(Board(id=board_id, name=f"W011-{tag}", slug=f"w011-{uuid.uuid4().hex[:6]}"))
    s.add(Agent(
        id=lead_id, name="Lead", role="orchestrator", board_id=board_id,
        agent_token_hash=generate_agent_token()[1], is_board_lead=True,
        scopes=["tasks:read"],
    ))
    s.add(Agent(
        id=worker_id, name="Worker", role="developer", board_id=board_id,
        agent_token_hash=generate_agent_token()[1], scopes=["tasks:read"],
    ))
    await s.commit()
    return board_id, lead_id, worker_id


async def _add_task(s, board_id, worker_id, *, title, status="done", auto_reason=None):
    task = Task(
        id=uuid.uuid4(), board_id=board_id, title=title, status=status,
        assigned_agent_id=worker_id, is_auto_created=auto_reason is not None,
        auto_reason=auto_reason, human_review_required=True,
    )
    s.add(task)
    await s.commit()
    return task


async def _link_entry(s, task_id):
    ch = BenchChallenge(title="Shot Sequence", prompt_text="p")
    s.add(ch)
    await s.commit()
    await s.refresh(ch)
    entry = BenchEntry(
        challenge_id=ch.id, model_label="DeepSeek", source_kind="agent",
        status="generating", task_id=task_id,
    )
    s.add(entry)
    await s.commit()
    return ch, entry


async def _lead_dms(s: AsyncSession, lead_id) -> list[Message]:
    from app.services.messaging import ensure_dm_thread
    lead = await s.get(Agent, lead_id)
    thread = await ensure_dm_thread(s, lead)
    rows = await s.exec(select(Message).where(Message.thread_id == thread.id))
    return list(rows.all())


async def _notify_comments(s: AsyncSession, task_id) -> list[TaskComment]:
    rows = await s.exec(select(TaskComment).where(
        TaskComment.task_id == task_id, TaskComment.comment_type == "system_notify",
    ))
    return list(rows.all())


async def _run_notify(s, task, board_id):
    from app.services.task_lifecycle import _notify_lead_on_completion
    with patch("app.database.engine", test_engine), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock) as emit:
        await _notify_lead_on_completion(s, task, board_id, "System")
    return [c.args[1] for c in emit.await_args_list]


@pytest.mark.asyncio
async def test_bench_task_does_not_wake_the_lead(_bench_skip_hook_registered):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id = await _seed_board_with_lead(s, "bench")
        task = await _add_task(
            s, board_id, worker_id, title="[Bench] Shot Sequence — DeepSeek",
            auto_reason="bench_studio challenge abc",
        )
        await _link_entry(s, task.id)

        events = await _run_notify(s, task, board_id)

        assert await _lead_dms(s, lead_id) == [], "bench completion must not DM the lead"
        assert await _notify_comments(s, task.id) == []
        assert "owner.completion_callback" not in events
        assert events == ["owner.completion_callback_skipped"]


@pytest.mark.asyncio
async def test_retried_bench_task_is_still_skipped(_bench_skip_hook_registered):
    """A retry re-points entry.task_id at a NEW task — the old task finishing
    late has no entry any more but is still a bench task (auto_reason)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id = await _seed_board_with_lead(s, "retry")
        task = await _add_task(
            s, board_id, worker_id, title="[Bench] Pagoda — old attempt",
            auto_reason="bench_studio challenge def",
        )

        events = await _run_notify(s, task, board_id)

        assert await _lead_dms(s, lead_id) == []
        assert events == ["owner.completion_callback_skipped"]


@pytest.mark.asyncio
async def test_normal_task_still_calls_back_the_lead(_bench_skip_hook_registered):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id = await _seed_board_with_lead(s, "normal")
        task = await _add_task(s, board_id, worker_id, title="Fix login button")

        events = await _run_notify(s, task, board_id)

        dms = await _lead_dms(s, lead_id)
        assert len(dms) == 1 and "TASK ERLEDIGT: Fix login button" in dms[0].body
        assert len(await _notify_comments(s, task.id)) == 1
        assert events == ["owner.completion_callback"]


@pytest.mark.asyncio
async def test_title_alone_does_not_skip(_bench_skip_hook_registered):
    """Only the bench layer's own markers count — an operator card that
    happens to start with "[Bench]" still reaches the lead."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id = await _seed_board_with_lead(s, "title")
        task = await _add_task(s, board_id, worker_id, title="[Bench] plan next wave")

        events = await _run_notify(s, task, board_id)

        assert len(await _lead_dms(s, lead_id)) == 1
        assert events == ["owner.completion_callback"]


@pytest.mark.asyncio
async def test_raising_skip_hook_fails_open_to_the_old_callback():
    async def _broken(session, task):
        raise RuntimeError("boom")

    hooks.completion_callback_skip_hooks.append(_broken)
    try:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board_id, lead_id, worker_id = await _seed_board_with_lead(s, "broken")
            task = await _add_task(s, board_id, worker_id, title="Any task")
            events = await _run_notify(s, task, board_id)
            assert len(await _lead_dms(s, lead_id)) == 1
            assert events == ["owner.completion_callback"]
    finally:
        hooks.completion_callback_skip_hooks.remove(_broken)


@pytest.mark.asyncio
async def test_skip_hook_db_error_cannot_poison_the_callback():
    """Fail-open must also hold for a DATABASE error inside a predicate.

    A failed flush leaves a SQLAlchemy session in "needs rollback" state
    (on Postgres any failed statement aborts the transaction the same way).
    If the predicate shared the callback's session, the fallback callback's
    own queries would then raise and the lead would get nothing — the
    opposite of fail-open. The predicate must run on a session of its own.
    """
    lead_ref = {}

    async def _db_broken(session, task):
        # Duplicate primary key -> IntegrityError on flush.
        session.add(Agent(
            id=lead_ref["id"], name="Dup", role="orchestrator",
            agent_token_hash=generate_agent_token()[1], scopes=[],
        ))
        await session.flush()
        return True  # never reached

    hooks.completion_callback_skip_hooks.append(_db_broken)
    try:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board_id, lead_id, worker_id = await _seed_board_with_lead(s, "dberr")
            lead_ref["id"] = lead_id
            task = await _add_task(s, board_id, worker_id, title="Any task, db error")
            events = await _run_notify(s, task, board_id)
            assert len(await _lead_dms(s, lead_id)) == 1, "DB error in a skip hook swallowed the callback"
            assert len(await _notify_comments(s, task.id)) == 1
            assert events == ["owner.completion_callback"]
    finally:
        hooks.completion_callback_skip_hooks.remove(_db_broken)


@pytest.mark.asyncio
async def test_real_path_review_finalize_does_not_wake_the_lead(
    monkeypatch, tmp_path, _bench_skip_hook_registered,
):
    """The path a live bench task takes: agent sets review -> bench
    on_task_review -> system_finalize_task_done -> _notify_lead_on_completion
    (scheduled via create_tracked_task — captured and awaited here)."""
    from app.verticals.bench_studio import orchestrator

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    monkeypatch.setattr(orchestrator, "maybe_advance", AsyncMock(), raising=False)
    scheduled = []

    def _capture(coro, name=None):
        scheduled.append(coro)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, lead_id, worker_id = await _seed_board_with_lead(s, "real")
        task = await _add_task(
            s, board_id, worker_id, title="[Bench] Shot Sequence — real path",
            status="review", auto_reason="bench_studio challenge real",
        )
        await _link_entry(s, task.id)

        with patch("app.utils.create_tracked_task", side_effect=_capture), \
             patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.services.activity.broadcast", new_callable=AsyncMock):
            handled = await orchestrator.on_task_review(s, task)
        assert handled is True

        notify = [c for c in scheduled if c.cr_code.co_name == "_notify_lead_on_completion"]
        others = [c for c in scheduled if c not in notify]
        for c in others:
            c.close()
        assert len(notify) == 1, "finalize must still schedule the completion step"
        with patch("app.database.engine", test_engine), \
             patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock) as emit:
            await notify[0]
        assert await _lead_dms(s, lead_id) == []
        assert [c.args[1] for c in emit.await_args_list] == ["owner.completion_callback_skipped"]


def test_register_adds_the_bench_predicate():
    from fastapi import FastAPI

    from app.verticals import bench_studio
    from app.verticals.bench_studio.callbacks import owns_completion_followup

    lists = (hooks.completion_callback_skip_hooks, hooks.task_review_hooks,
             hooks.task_done_hooks, hooks.x_post_resolved_hooks)
    before = [list(lst) for lst in lists]
    try:
        bench_studio.register(FastAPI())
        bench_studio.register(FastAPI())  # idempotent
        assert hooks.completion_callback_skip_hooks.count(owns_completion_followup) == 1
    finally:
        for lst, snap in zip(lists, before):
            lst[:] = snap
