"""Round-2 Q5: a task in review keeps who delivered it, and the
"review decision missing" reminder stops instead of repeating forever.

1. The hand-off to review (operator review AND agent reviewer) clears or
   replaces ``assigned_agent_id`` on purpose — the inbox and the review
   routing read "nobody assigned" as "the operator's review". The task core
   is frozen (PRINCIPLES "no new task columns"), so the agent that built the
   card is read from the status history instead and sent along as
   ``delivered_by_agent_id`` / ``delivered_at`` — the task list, the agent
   filter and the detail can still name it.
2. ``task.review_decision_missing`` nudged the assigned agent every 30 min,
   also after the review had been escalated (live: 12x in 5.5 h). Now: at
   most ``REVIEW_DECISION_NUDGE_CAP`` nudges per review round, none after an
   escalation in that round.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _seed_status_events(task_status, events, *, assigned=None):
    """Board + two agents + one task, plus `task.status_changed` activity
    rows (old, new, agent_key, minutes_ago)."""
    from app.models.activity import ActivityEvent
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.task import Task

    board_id, task_id = uuid.uuid4(), uuid.uuid4()
    agents = {"builder": uuid.uuid4(), "checker": uuid.uuid4()}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Deliverer Board", slug=f"dlv-{board_id.hex[:8]}"))
        for name, aid in agents.items():
            s.add(Agent(id=aid, name=name, role="developer", board_id=board_id, agent_token_hash=name))
        await s.flush()
        s.add(Task(id=task_id, board_id=board_id, title="Deliver me", status=task_status,
                   assigned_agent_id=agents.get(assigned) if assigned else None))
        await s.flush()
        for old, new, who, mins in events:
            s.add(ActivityEvent(
                event_type="task.status_changed", title="status", board_id=board_id, task_id=task_id,
                agent_id=agents.get(who) if who else None,
                detail={"old_status": old, "new_status": new},
                created_at=datetime.utcnow() - timedelta(minutes=mins),
            ))
        await s.commit()
    return board_id, task_id, agents


@pytest.mark.asyncio
async def test_deliverer_is_the_agent_that_last_moved_the_card_to_review():
    from app.models.task import Task
    from app.services.task_delivery import find_deliverers

    _, task_id, agents = await _seed_status_events("review", [
        ("inbox", "in_progress", "builder", 60),
        ("in_progress", "review", "checker", 50),   # an older round
        ("review", "in_progress", None, 40),         # operator asked for changes
        ("in_progress", "review", "builder", 10),    # the latest delivery
        ("in_progress", "review", None, 5),          # unattributed: ignored
    ])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        got = await find_deliverers(s, [await s.get(Task, task_id)])
    agent_id, at = got[task_id]
    assert agent_id == agents["builder"]
    assert at is not None


@pytest.mark.asyncio
async def test_no_deliverer_for_a_card_that_never_reached_review():
    from app.models.task import Task
    from app.services.task_delivery import find_deliverers

    _, task_id, _ = await _seed_status_events("in_progress", [
        ("inbox", "in_progress", "builder", 60),
    ], assigned="builder")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await find_deliverers(s, [await s.get(Task, task_id)]) == {}


@pytest.mark.asyncio
async def test_operator_review_handoff_names_the_deliverer_in_the_list(auth_client, fake_redis):
    """The real path: the agent hands its card to the operator's review
    (human_review_required). The hand-off clears assigned_agent_id on purpose
    (the inbox reads that as "your review"); the list still names who
    delivered it."""
    from tests.test_human_review_gate import _create_workflow_data

    data = await _create_workflow_data(human_review_required=True)
    with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock), \
         patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock):
        resp = await auth_client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )
    assert resp.status_code == 200, resp.text

    listed = await auth_client.get(f"/api/v1/boards/{data['board'].id}/tasks")
    assert listed.status_code == 200, listed.text
    row = next(r for r in listed.json() if r["id"] == str(data["task"].id))
    assert row["status"] == "review"
    assert row["assigned_agent_id"] is None  # unchanged: the operator's review
    assert row["delivered_by_agent_id"] == str(data["developer"].id)
    assert row["delivered_at"]

    one = await auth_client.get(f"/api/v1/boards/{data['board'].id}/tasks/{data['task'].id}")
    assert one.status_code == 200, one.text
    assert one.json()["delivered_by_agent_id"] == str(data["developer"].id)


# ── 2. the decision-missing nudge has a lid ──────────────────────────────


async def _review_task_with_stale_comment(make_board, make_agent, make_task):
    from app.models.task import TaskComment, TaskEvent

    board = await make_board(name="Lid Board", slug=f"lid-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(name="Quiet", board_id=board.id, is_board_lead=False)
    task = await make_task(board_id=board.id, title="Waiting for a decision",
                           status="review", assigned_agent_id=agent.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        # The card entered review 5 h ago; the agent commented 4 h ago.
        s.add(TaskEvent(task_id=task.id, from_status="in_progress", to_status="review",
                        changed_by="agent", agent_id=agent.id,
                        created_at=datetime.utcnow() - timedelta(hours=5)))
        s.add(TaskComment(task_id=task.id, author_agent_id=agent.id, author_type="agent",
                          content="Had a look.", created_at=datetime.utcnow() - timedelta(hours=4)))
        await s.commit()
    return task


async def _nudge_count(task_id) -> int:
    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        rows = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == task_id, TaskComment.comment_type == "watchdog_notify",
        ))).all()
    return len(rows)


async def _tick(task_id, fake_redis):
    from app.models.task import Task
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    await fake_redis.delete(f"mc:watchdog:review_decision_missing:{task_id}")  # cooldown over
    with patch("app.services.watchdog.task_monitor.get_redis", return_value=fake_redis), \
         patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            live = await s.get(Task, task_id)
            await TaskMonitorMixin()._check_review_decision_missing(s, live, datetime.utcnow(), fake_redis)


@pytest.mark.asyncio
async def test_decision_missing_nudge_stops_at_the_cap(fake_redis, make_board, make_agent, make_task):
    from app.services.watchdog.task_monitor import REVIEW_DECISION_NUDGE_CAP

    task = await _review_task_with_stale_comment(make_board, make_agent, make_task)
    for _ in range(REVIEW_DECISION_NUDGE_CAP + 3):
        await _tick(task.id, fake_redis)
    assert await _nudge_count(task.id) == REVIEW_DECISION_NUDGE_CAP


@pytest.mark.asyncio
async def test_decision_missing_nudge_stops_after_escalation(fake_redis, make_board, make_agent, make_task):
    from app.models.activity import ActivityEvent

    task = await _review_task_with_stale_comment(make_board, make_agent, make_task)
    await _tick(task.id, fake_redis)
    assert await _nudge_count(task.id) == 1

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(event_type="task.review_escalation", title="Review-Eskalation",
                            board_id=task.board_id, task_id=task.id, severity="warning"))
        await s.commit()

    await _tick(task.id, fake_redis)
    await _tick(task.id, fake_redis)
    assert await _nudge_count(task.id) == 1


@pytest.mark.asyncio
async def test_new_review_round_starts_counting_again(fake_redis, make_board, make_agent, make_task):
    """Nudges and escalations of an EARLIER review round do not silence the
    next one — the lid is per round, not forever."""
    from app.models.activity import ActivityEvent
    from app.models.task import TaskEvent

    task = await _review_task_with_stale_comment(make_board, make_agent, make_task)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        old = datetime.utcnow() - timedelta(hours=6)
        s.add(ActivityEvent(event_type="task.review_escalation", title="old round",
                            board_id=task.board_id, task_id=task.id, created_at=old))
        for _ in range(5):
            s.add(ActivityEvent(event_type="task.review_decision_missing", title="old round",
                                board_id=task.board_id, task_id=task.id, created_at=old))
        await s.commit()

    await _tick(task.id, fake_redis)
    assert await _nudge_count(task.id) == 1


@pytest.mark.asyncio
async def test_timeline_shows_the_notice_title_without_its_ascii_report_body(auth_client):
    """The operator notice's body is the report-channel text, ASCII on purpose
    ("Review fuer … haengt … noetig"). The operator's timeline shows the
    notice by its title only."""
    from app.models.activity import ActivityEvent

    board_id, task_id, _ = await _seed_status_events("review", [])
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(ActivityEvent(
            event_type="operator.notice", title="Review STUCK: 'Deliver me' (Quiet)",
            board_id=board_id, task_id=task_id, severity="warning",
            detail={"action_type": "review_stuck", "title": "Review STUCK: 'Deliver me' (Quiet)",
                    "body": "Review fuer 'Deliver me' haengt seit 301 Min. Reviewer: Quiet. Manuelle Pruefung noetig."},
        ))
        await s.commit()

    resp = await auth_client.get(f"/api/v1/boards/{board_id}/tasks/{task_id}/timeline")
    assert resp.status_code == 200, resp.text
    notice = next(e for e in resp.json()["entries"] if (e.get("meta") or {}).get("event_type") == "operator.notice")
    assert notice["title"] == "Review STUCK: 'Deliver me' (Quiet)"
    assert notice["detail"] is None
    assert "haengt" not in str(notice["title"])
