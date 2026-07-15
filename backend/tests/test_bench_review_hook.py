"""bench_studio task_review_hook — bench agent tasks skip human review and
finalize review -> done immediately (operator decision 2026-07-15).

Covers:
  - on_task_review finalizes a bench task straight to done + runs the
    task_done hook (artifact collection) right away.
  - on_task_review is a silent no-op for non-bench tasks.
  - the hook registry (run_task_review_hooks) swallows a raising hook —
    task stays in review, no crash.
  - handle_human_review_handoff defers to the hook first: for a bench task
    it skips the "wait for Mark" telegram/comment plumbing entirely.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("app.verticals.bench_studio")

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry
from app.models.task import Task, TaskComment
from app.verticals import hooks
from app.verticals.bench_studio import orchestrator
from tests.conftest import test_engine


@pytest.fixture
def _bench_hooks_registered():
    """Ensure bench_studio's task_review_hooks + task_done_hooks entries are
    on the (process-global) registry for this test.

    In the real app these get appended once at import time (app.main module
    level -> app.verticals.register_all()). Whether that has already run
    depends on test collection order / which other test files happened to
    import app.main first — fine for the app, but flaky for a test file run
    in isolation. Registering explicitly here makes this file's tests
    deterministic regardless of what ran before it, without double-
    registering (register(app)'s own `if hook not in list` guard mirrored
    here) if app.main already did.
    """
    added_review = orchestrator.on_task_review not in hooks.task_review_hooks
    added_done = orchestrator.on_task_done not in hooks.task_done_hooks
    if added_review:
        hooks.task_review_hooks.append(orchestrator.on_task_review)
    if added_done:
        hooks.task_done_hooks.append(orchestrator.on_task_done)
    yield
    if added_review:
        hooks.task_review_hooks.remove(orchestrator.on_task_review)
    if added_done:
        hooks.task_done_hooks.remove(orchestrator.on_task_done)


async def _seed_challenge(session, *, entry_specs=None):
    ch = BenchChallenge(title="T", prompt_text="p")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    entries = []
    for spec in entry_specs or []:
        e = BenchEntry(challenge_id=ch.id, **spec)
        session.add(e)
        entries.append(e)
    await session.commit()
    for e in entries:
        await session.refresh(e)
    return ch, entries


@pytest.mark.asyncio
async def test_on_task_review_finalizes_bench_task_and_collects_artifact(
    session, tmp_path, monkeypatch, make_board, make_task, _bench_hooks_registered
):
    """A bench task landing in review must end up done, with the task_done
    hook (artifact collection) already having run — not sitting in review
    waiting for `mc approve`."""
    from app.models.deliverable import TaskDeliverable

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(
        board.id, title="[Bench] one-shot", status="review",
        assigned_agent_id=None, human_review_required=True,
    )

    src = tmp_path / "agent-out" / "index.html"
    src.parent.mkdir(parents=True)
    src.write_text("<html><body>agent</body></html>")
    session.add(TaskDeliverable(task_id=task.id, deliverable_type="file",
                                title="index.html", path=str(src)))

    ch, entries = await _seed_challenge(
        session,
        entry_specs=[
            {"model_label": "Claude", "source_kind": "agent",
             "status": "generating", "task_id": task.id},
        ],
    )
    monkeypatch.setattr(orchestrator, "record_entry",
                        AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"}))
    monkeypatch.setattr(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4"))

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.utils.create_tracked_task"):
        handled = await orchestrator.on_task_review(session, task)

    assert handled is True
    # task came from make_task's own (separate) session — re-fetch instead
    # of refresh() to avoid a "not persistent within this Session" error.
    updated_task = await session.get(Task, task.id)
    assert updated_task.status == "done"
    assert updated_task.completed_at is not None
    assert updated_task.dispatch_intent == "root"

    entry = entries[0]
    await session.refresh(entry)
    assert entry.artifact_path == str(tmp_path / f"bench-{ch.id}" / "Claude" / "index.html")


@pytest.mark.asyncio
async def test_on_task_review_ignores_non_bench_tasks(session, make_board, make_task):
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="normal task", status="review")

    handled = await orchestrator.on_task_review(session, task)

    assert handled is False
    assert task.status == "review"  # untouched — no_task_review_hooks path never wrote anything


@pytest.mark.asyncio
async def test_run_task_review_hooks_swallows_error_task_stays_review(session, make_board, make_task):
    """A broken hook must not crash the caller and must not count as
    'handled' — the task falls back to the normal human-review flow."""
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="normal task", status="review")

    async def _boom(_session, _task):
        raise RuntimeError("boom")

    hooks.task_review_hooks.append(_boom)
    try:
        handled = await hooks.run_task_review_hooks(session, task)
    finally:
        hooks.task_review_hooks.remove(_boom)

    assert handled is False
    assert task.status == "review"  # untouched — no hook wrote anything


@pytest.mark.asyncio
async def test_handle_human_review_handoff_bench_task_skips_wait_for_mark(
    tmp_path, monkeypatch, _bench_hooks_registered,
):
    """Integration: PATCH-style call into handle_human_review_handoff for a
    bench task must be intercepted by the bench_studio review-hook — no
    telegram ping, no 'wartet auf Mark' comment, task ends done."""
    from app.models.board import Board
    from app.models.agent import Agent

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Bench HR Board", slug=f"bhr-{board_id.hex[:8]}"))
        developer = Agent(
            id=dev_id, name="Cody", role="developer", board_id=board_id,
            agent_token_hash="x", current_task_id=task_id,
        )
        s.add(developer)
        s.add(Task(
            id=task_id, board_id=board_id, title="[Bench] hr-skip",
            status="review", assigned_agent_id=dev_id, human_review_required=True,
        ))
        await s.commit()

        from app.models.deliverable import TaskDeliverable
        src = tmp_path / "agent-out" / "index.html"
        src.parent.mkdir(parents=True)
        src.write_text("<html><body>agent</body></html>")
        s.add(TaskDeliverable(task_id=task_id, deliverable_type="file",
                              title="index.html", path=str(src)))
        ch = BenchChallenge(title="T", prompt_text="p")
        s.add(ch)
        await s.commit()
        await s.refresh(ch)
        s.add(BenchEntry(challenge_id=ch.id, model_label="Claude", source_kind="agent",
                          status="generating", task_id=task_id))
        await s.commit()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.utils.create_tracked_task"), \
             patch.object(orchestrator, "record_entry",
                          AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"})), \
             patch.object(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4")), \
             patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
            from app.services.task_lifecycle import handle_human_review_handoff
            task = await s.get(Task, task_id)
            dev = await s.get(Agent, dev_id)
            await handle_human_review_handoff(s, task, board_id, developer=dev)

    mock_telegram.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated_task = await s.get(Task, task_id)
        updated_dev = await s.get(Agent, dev_id)
        assert updated_task.status == "done"
        assert updated_dev.current_task_id is None

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        handoff_comments = [c for c in comments if c.comment_type == "handoff" and c.author_type == "system"]
        assert not handoff_comments, "must not create the human-review 'wait for Mark' comment"
