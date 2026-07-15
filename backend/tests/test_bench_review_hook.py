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


# ── Adversarial-review fixes (2026-07-15) ──────────────────────────────────
# Three other call sites transition a task into "review" without going
# through the PATCH routers (tasks.py / agent_task_status.py), which had the
# human_review_required branch already. All three needed the same fix.


@pytest.mark.asyncio
async def test_stale_check_auto_promote_bench_task_finalizes_to_done(
    fake_redis, tmp_path, monkeypatch, _bench_hooks_registered
):
    """Critical 1: task_runner's stale-check "resolution comment" auto-
    promote (Phase 8 BUG-01 Path B) sets task.status="review" directly and
    never called any handoff — a human_review_required bench task landing
    in review there sat there forever (watchdog skips human_review_required
    tasks on purpose, and no hook ever got a chance to fire). Must now end
    done, same as the PATCH-router path."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task, TaskComment
    from app.services.task_runner import TaskRunnerService
    from app.utils import utcnow

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Stale Bench Board", slug=f"sb-{board_id.hex[:8]}"))
        _raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="StaleWorker", board_id=board_id,
            agent_token_hash=token_hash, is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            auto_promote_on_resolution=True,
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="[Bench] stale one-shot",
            status="in_progress", assigned_agent_id=agent_id,
            human_review_required=True,
        ))
        await s.commit()

        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=agent_id,
            content="Fertig.", comment_type="resolution", created_at=utcnow(),
        ))
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

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
             patch("app.utils.create_tracked_task"), \
             patch.object(orchestrator, "record_entry",
                          AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"})), \
             patch.object(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4")):
            await runner._check_stale_in_progress(s)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "done", (
            f"bench task with human_review_required must auto-finalize to "
            f"done via the stale-check promote path too; got {updated.status}"
        )
        assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_stale_check_auto_promote_non_bench_task_unchanged(fake_redis):
    """Behavior-neutral guard: this branch never called handle_review_handoff
    before the fix (for non-human-review tasks) — it still doesn't. Only
    the human_review_required branch gained a handoff call."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task, TaskComment
    from app.services.task_runner import TaskRunnerService
    from app.utils import utcnow

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Stale Plain Board", slug=f"sp-{board_id.hex[:8]}"))
        _raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="PlainWorker", board_id=board_id,
            agent_token_hash=token_hash, is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
            auto_promote_on_resolution=True,
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Plain task",
            status="in_progress", assigned_agent_id=agent_id,
        ))
        await s.commit()
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=agent_id,
            content="Fertig.", comment_type="resolution", created_at=utcnow(),
        ))
        await s.commit()

    runner = TaskRunnerService()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_runner.emit_event", new_callable=AsyncMock), \
             patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff, \
             patch("app.services.task_lifecycle.handle_human_review_handoff", new_callable=AsyncMock) as mock_hr_handoff:
            await runner._check_stale_in_progress(s)

    mock_handoff.assert_not_called()
    mock_hr_handoff.assert_not_called()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "review"


async def _create_resolution_promote_data(session, *, human_review_required=None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    session.add(Board(id=board_id, name="Resolution Board", slug=f"rp-{board_id.hex[:8]}"))
    raw_token, token_hash = generate_agent_token()
    session.add(Agent(
        id=agent_id, name="Cody", board_id=board_id, agent_token_hash=token_hash,
        is_board_lead=False, scopes=["tasks:read", "tasks:write", "tasks:create"],
    ))
    session.add(Task(
        id=task_id, board_id=board_id, title="[Bench] resolution one-shot",
        status="in_progress", assigned_agent_id=agent_id,
        human_review_required=human_review_required,
    ))
    await session.commit()
    return board_id, agent_id, task_id, raw_token


@pytest.mark.asyncio
async def test_resolution_comment_auto_promote_bench_task_finalizes_done(
    client, fake_redis, tmp_path, monkeypatch, _bench_hooks_registered,
):
    """Critical 2: agent_comments.py's resolution auto-promote unconditionally
    called handle_review_handoff, dispatching an agent reviewer for bench
    tasks (forbidden frontier-token burn) and never giving the review-hook a
    chance to fire. Must now finalize straight to done with no reviewer
    dispatch, same as the PATCH-router path."""
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, agent_id, task_id, token = await _create_resolution_promote_data(
            s, human_review_required=True,
        )
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

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff, \
         patch("app.utils.create_tracked_task"), \
         patch.object(orchestrator, "record_entry",
                      AsyncMock(return_value={"video_path": "/v.mp4", "screenshot_path": "/s.png"})), \
         patch.object(orchestrator, "compose_challenge", AsyncMock(return_value="/g.mp4")):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"content": "Fertig.", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text
    mock_agent_handoff.assert_not_called()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "done"


@pytest.mark.asyncio
async def test_resolution_comment_auto_promote_non_bench_human_review_uses_human_path(
    client, fake_redis,
):
    """Non-bench human_review_required task via the resolution auto-promote
    path: must route to handle_human_review_handoff (Mark), never dispatch
    an agent reviewer."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, agent_id, task_id, token = await _create_resolution_promote_data(
            s, human_review_required=True,
        )

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff, \
         patch("app.services.telegram_bot.settings.telegram_bot_token", "test-token"), \
         patch("app.services.telegram_bot.settings.telegram_chat_id", "test-chat"), \
         patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"content": "Fertig.", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text
    mock_agent_handoff.assert_not_called()
    mock_telegram.assert_called_once()

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "review"
        assert updated.assigned_agent_id is None


@pytest.mark.asyncio
async def test_resolution_comment_auto_promote_falsy_keeps_agent_reviewer(client, fake_redis):
    """Regression guard: falsy human_review_required still dispatches the
    agent reviewer as before (unchanged behavior)."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_id, agent_id, task_id, token = await _create_resolution_promote_data(
            s, human_review_required=None,
        )

    with patch("app.routers.agent_comments.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff:
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            json={"content": "Fertig.", "comment_type": "resolution"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201, resp.text
    mock_agent_handoff.assert_called_once()


@pytest.mark.asyncio
async def test_system_finalize_task_done_survives_post_commit_failure(
    session, make_board, make_task,
):
    """Critical 3a: everything after the initial done-commit is best-effort.
    A raising emit_event must not propagate — the task is already done and
    committed by that point, and the caller must be able to treat this as
    success."""
    from app.services.task_lifecycle import system_finalize_task_done

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(board.id, title="finalize-partial-fail", status="review")

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock,
               side_effect=RuntimeError("redis down")), \
         patch("app.utils.create_tracked_task"):
        await system_finalize_task_done(session, task, board.id, old_status="review")

    updated = await session.get(Task, task.id)
    assert updated.status == "done"
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_handle_human_review_handoff_skips_fallback_if_already_finalized(
    session, make_board, make_task,
):
    """Critical 3b: a hook that finalizes the task to done and THEN raises
    is reported as "not handled" by run_task_review_hooks (error swallowed)
    — the wait-for-Mark fallback must detect the task is no longer in
    'review' and skip its mutations instead of stomping a done task."""
    from app.utils import utcnow
    from app.services.task_lifecycle import handle_human_review_handoff

    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(
        board.id, title="late-failure hook", status="review",
        assigned_agent_id=None, human_review_required=True,
    )

    async def _finalize_then_boom(sess, t):
        db_task = await sess.get(Task, t.id)
        db_task.status = "done"
        db_task.completed_at = utcnow()
        sess.add(db_task)
        await sess.commit()
        raise RuntimeError("late failure after finalize")

    hooks.task_review_hooks.append(_finalize_then_boom)
    try:
        with patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
            db_task = await session.get(Task, task.id)
            await handle_human_review_handoff(session, db_task, board.id)
    finally:
        hooks.task_review_hooks.remove(_finalize_then_boom)

    mock_telegram.assert_not_called()
    updated = await session.get(Task, task.id)
    assert updated.status == "done"
    assert updated.completed_at is not None

    comments = (await session.exec(
        select(TaskComment).where(TaskComment.task_id == task.id)
    )).all()
    handoff_comments = [c for c in comments if c.comment_type == "handoff" and c.author_type == "system"]
    assert not handoff_comments, "fallback must not run once the task is already done"


@pytest.mark.asyncio
async def test_on_task_review_idempotent_second_call_is_noop(
    session, tmp_path, monkeypatch, make_board, make_task, _bench_hooks_registered,
):
    """Important 4: on_task_review must not re-finalize an already-done
    task (double-fire safety — e.g. a hook retry after a transient error
    upstream)."""
    from app.models.deliverable import TaskDeliverable

    monkeypatch.setattr(orchestrator, "SHARED_DELIVERABLES", tmp_path)
    board = await make_board(slug=f"b-{uuid.uuid4().hex[:6]}")
    task = await make_task(
        board.id, title="[Bench] idempotent", status="review",
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
        first = await orchestrator.on_task_review(session, task)
        first_task = await session.get(Task, task.id)
        first_completed_at = first_task.completed_at

        second = await orchestrator.on_task_review(session, first_task)

    assert first is True
    assert second is False
    second_task = await session.get(Task, task.id)
    assert second_task.completed_at == first_completed_at
