"""Tests: optional human review gate (human_review_required).

When a task has human_review_required=True, the in_progress→review
transition must NOT hand off to an agent reviewer — the task stays in
`review` with assigned_agent_id=None (surfaces in Mark's Inbox) and Mark
gets pinged via Telegram. Falsy/unset keeps the existing agent-reviewer
behavior unchanged.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_workflow_data(*, human_review_required=None):
    """Board + developer + reviewer + in_progress task with evidence comments."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Human Review Board", slug=f"hr-{board_id.hex[:8]}")
        s.add(board)

        dev_token_raw, dev_token_hash = generate_agent_token()
        developer = Agent(
            id=dev_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=dev_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        )
        s.add(developer)

        reviewer_token_raw, reviewer_token_hash = generate_agent_token()
        reviewer = Agent(
            id=reviewer_id,
            name="Rex",
            role="reviewer",
            board_id=board_id,
            agent_token_hash=reviewer_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(reviewer)

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Implement feature Y",
            status="in_progress",
            assigned_agent_id=dev_id,
            human_review_required=human_review_required,
        )
        s.add(task)
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=dev_id,
            comment_type="progress", content="Implementation complete",
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=dev_id,
            comment_type="reflection",
            content=(
                "## Was wurde gemacht\nFeature Y implementiert\n\n"
                "## Was hat funktioniert\nTDD\n\n"
                "## Was war unklar\nNichts\n\n"
                "## Lesson fuer Agent-Memory\nKeine."
            ),
        ))
        await s.commit()
        await s.refresh(board)
        await s.refresh(developer)
        await s.refresh(reviewer)
        await s.refresh(task)

    return {
        "board": board,
        "developer": developer,
        "reviewer": reviewer,
        "task": task,
        "dev_token": dev_token_raw,
        "reviewer_token": reviewer_token_raw,
    }


@pytest.mark.asyncio
async def test_human_review_required_skips_agent_reviewer(client, fake_redis):
    """human_review_required=True → no reviewer dispatch, task stays in review."""
    data = await _create_workflow_data(human_review_required=True)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff:
                with patch("app.services.telegram_bot.settings.telegram_bot_token", "test-token"), \
                     patch("app.services.telegram_bot.settings.telegram_chat_id", "test-chat"), \
                     patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
                    resp = await client.patch(
                        f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                        json={"status": "review"},
                        headers={"Authorization": f"Bearer {data['dev_token']}"},
                    )

    assert resp.status_code == 200, resp.text
    mock_agent_handoff.assert_not_called()
    mock_telegram.assert_called_once()
    assert "Human-Review" in mock_telegram.call_args.args[0]

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "review"
        assert updated.assigned_agent_id is None


@pytest.mark.asyncio
async def test_human_review_falsy_keeps_agent_reviewer_dispatch(client, fake_redis):
    """human_review_required falsy (None) → existing agent-reviewer path unchanged."""
    data = await _create_workflow_data(human_review_required=None)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff:
                with patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
                    resp = await client.patch(
                        f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                        json={"status": "review"},
                        headers={"Authorization": f"Bearer {data['dev_token']}"},
                    )

    assert resp.status_code == 200, resp.text
    mock_agent_handoff.assert_called_once()
    mock_telegram.assert_not_called()


@pytest.mark.asyncio
async def test_human_review_handoff_pings_telegram_and_creates_comment():
    """Direct unit test of handle_human_review_handoff: comment + event + telegram."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task, TaskComment
    from sqlmodel import select

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="HR Direct", slug=f"hrd-{board_id.hex[:8]}"))
        developer = Agent(
            id=dev_id, name="Cody", role="developer", board_id=board_id,
            agent_token_hash="x", current_task_id=task_id,
        )
        s.add(developer)
        s.add(Task(
            id=task_id, board_id=board_id, title="HR Task",
            status="review", assigned_agent_id=dev_id, human_review_required=True,
        ))
        await s.commit()
        await s.refresh(developer)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock) as mock_event:
            with patch("app.services.telegram_bot.settings.telegram_bot_token", "test-token"), \
                 patch("app.services.telegram_bot.settings.telegram_chat_id", "test-chat"), \
                 patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
                from app.services.task_lifecycle import handle_human_review_handoff
                task = await s.get(Task, task_id)
                dev = await s.get(Agent, dev_id)
                await handle_human_review_handoff(s, task, board_id, developer=dev)

    mock_event.assert_called_once()
    mock_telegram.assert_called_once()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated_task = await s.get(Task, task_id)
        updated_dev = await s.get(Agent, dev_id)
        assert updated_task.assigned_agent_id is None
        assert updated_task.status == "review"
        assert updated_dev.current_task_id is None

        comments = (await s.exec(
            select(TaskComment).where(TaskComment.task_id == task_id)
        )).all()
        handoff_comments = [c for c in comments if c.comment_type == "handoff" and c.author_type == "system"]
        assert handoff_comments, "expected a system handoff comment"


def test_agent_task_create_does_not_default_human_review_required():
    """Loop-safety: AgentTaskCreate has no human_review_required field at all,
    so agent/loop-created subtasks can never end up with it set to True."""
    from app.routers.agent_task_status import AgentTaskCreate

    assert "human_review_required" not in AgentTaskCreate.model_fields


@pytest.mark.asyncio
async def test_operator_patch_review_transition_respects_human_review_gate(client, fake_redis):
    """C1 regression: the User/UI PATCH route (update_task, tasks.py) must
    mirror the agent route's human_review_required gate. Before the fix this
    path unconditionally called handle_review_handoff, dispatching Rex even
    for a task explicitly routed to Mark."""
    from app.models.board import Board
    from app.models.user import User
    from app.models.task import Task
    from app.auth import create_access_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="HR Operator Board", slug=f"hro-{board_id.hex[:8]}"))
        s.add(User(id=user_id, email="mark2@example.com", name="Mark", role="admin", is_active=True))
        s.add(Task(
            id=task_id, board_id=board_id, title="Operator-driven task",
            status="in_progress", assigned_agent_id=None, human_review_required=True,
        ))
        await s.commit()

    token = create_access_token(str(user_id), "admin")

    with patch("app.routers.tasks.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff, \
         patch("app.services.telegram_bot.settings.telegram_bot_token", "test-token"), \
         patch("app.services.telegram_bot.settings.telegram_chat_id", "test-chat"), \
         patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
        resp = await client.patch(
            f"/api/v1/boards/{board_id}/tasks/{task_id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    mock_agent_handoff.assert_not_called()
    mock_telegram.assert_called_once()
    assert "Human-Review" in mock_telegram.call_args.args[0]

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "review"
        assert updated.assigned_agent_id is None


@pytest.mark.asyncio
async def test_operator_patch_review_transition_falsy_keeps_agent_handoff(client, fake_redis):
    """Regression guard: falsy human_review_required on the operator route
    still dispatches the agent reviewer (no change to existing behavior)."""
    from app.models.board import Board
    from app.models.user import User
    from app.models.task import Task
    from app.auth import create_access_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="HR Operator Board 2", slug=f"hro2-{board_id.hex[:8]}"))
        s.add(User(id=user_id, email="mark3@example.com", name="Mark", role="admin", is_active=True))
        s.add(Task(
            id=task_id, board_id=board_id, title="Operator-driven task 2",
            status="in_progress", assigned_agent_id=None, human_review_required=None,
        ))
        await s.commit()

    token = create_access_token(str(user_id), "admin")

    with patch("app.routers.tasks.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_agent_handoff, \
         patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock) as mock_telegram:
        resp = await client.patch(
            f"/api/v1/boards/{board_id}/tasks/{task_id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text
    mock_agent_handoff.assert_called_once()
    mock_telegram.assert_not_called()


@pytest.mark.asyncio
async def test_direct_done_blocked_for_human_review_task_even_without_board_flag(client, fake_redis, make_board, make_task):
    """M1 regression: human_review_required must be a HARD GATE on done,
    independent of the board's require_review_before_done flag. On a board
    with the flag OFF, a direct in_progress→done PATCH on a human-review task
    must still be blocked (task stays in_progress) — otherwise Mark never
    sees it."""
    from app.models.user import User
    from app.models.task import Task
    from app.auth import create_access_token

    board = await make_board(
        name="HR Done-Gate Board", slug=f"hrdg-{uuid.uuid4().hex[:8]}",
        require_review_before_done=False,
    )
    task = await make_task(
        board_id=board.id, title="Direct-done attempt",
        status="in_progress", human_review_required=True,
    )
    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email="mark4@example.com", name="Mark", role="admin", is_active=True))
        await s.commit()
    token = create_access_token(str(user_id), "admin")

    with patch("app.routers.tasks.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 400, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "in_progress"


@pytest.mark.asyncio
async def test_agent_direct_done_blocked_for_human_review_task(client, fake_redis, make_board, make_task):
    """M1 regression on the agent-scoped path (work_context.enforce_board_rules_agent):
    an agent must not be able to push a human_review_required task straight to
    done, even with require_review_before_done=False on the board."""
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board = await make_board(
        name="HR Agent Done-Gate Board", slug=f"hradg-{uuid.uuid4().hex[:8]}",
        require_review_before_done=False,
    )
    task = await make_task(
        board_id=board.id, title="Agent direct-done attempt",
        status="in_progress", human_review_required=True,
    )

    dev_token_raw, dev_token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        dev_id = uuid.uuid4()
        s.add(Agent(
            id=dev_id, name="Cody2", role="developer", board_id=board.id,
            agent_token_hash=dev_token_hash, is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        ))
        t = await s.get(Task, task.id)
        t.assigned_agent_id = dev_id
        s.add(t)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {dev_token_raw}"},
        )

    assert resp.status_code == 400, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task.id)
        assert updated.status == "in_progress"


@pytest.mark.asyncio
async def test_review_endpoint_approve_on_human_review_task(client, fake_redis):
    """POST .../review approve on a human-review task (assigned_agent_id=None,
    status=review) still works unchanged — Mark can approve directly."""
    from app.models.board import Board
    from app.models.user import User
    from app.models.task import Task
    from app.auth import create_access_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    user_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="HR Approve Board", slug=f"hra-{board_id.hex[:8]}"))
        s.add(User(id=user_id, email="mark@example.com", name="Mark", role="admin", is_active=True))
        s.add(Task(
            id=task_id, board_id=board_id, title="Needs Mark",
            status="review", assigned_agent_id=None, human_review_required=True,
        ))
        await s.commit()

    token = create_access_token(str(user_id), "admin")

    with patch("app.routers.tasks.emit_event", new_callable=AsyncMock), \
         patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/boards/{board_id}/tasks/{task_id}/review",
            json={"decision": "approve", "comment": "Sieht gut aus"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200, resp.text

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        updated = await s.get(Task, task_id)
        assert updated.status == "done"


@pytest.mark.asyncio
async def test_watchdog_skips_human_review_task_no_nudge(fake_redis, make_board, make_task):
    """m1 regression: _check_review_tasks must not nudge/escalate a
    human_review_required task — it is deliberately waiting on Mark, not
    stuck on a missing agent reviewer. Backdate updated_at well past the
    60min nudge threshold; before the fix this fired task.review_nudge."""
    from datetime import datetime, timedelta
    from app.models.task import Task
    from app.models.approval import Approval
    from sqlmodel import select

    def _naive_utcnow() -> datetime:
        return datetime.utcnow()

    board = await make_board(name="HR Watchdog Board", slug=f"hrw-{uuid.uuid4().hex[:8]}")
    task = await make_task(
        board_id=board.id, title="Waiting on Mark",
        status="review", assigned_agent_id=None, human_review_required=True,
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task.id)
        t.updated_at = _naive_utcnow() - timedelta(minutes=200)  # past nudge + approval stage
        s.add(t)
        await s.commit()

    from app.services.watchdog.core import WatchdogService

    with patch("app.services.watchdog.task_monitor.get_redis",
               AsyncMock(return_value=fake_redis)), \
         patch("app.services.watchdog.task_monitor.utcnow", _naive_utcnow), \
         patch("app.services.watchdog.task_monitor.emit_event",
               new_callable=AsyncMock) as emit:
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            svc = WatchdogService()
            await svc._check_review_tasks(s)

    nudge_events = [c for c in emit.call_args_list
                    if len(c.args) > 1 and c.args[1] in ("task.review_nudge", "task.review_stuck")]
    assert not nudge_events, "human_review_required task must not trigger reviewer-nudge escalation"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (await s.exec(
            select(Approval).where(
                Approval.task_id == task.id,
                Approval.action_type == "review_stuck",
            )
        )).all()
        assert not approvals, "human_review_required task must not create a review_stuck approval"
