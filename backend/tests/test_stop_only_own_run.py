"""The stop API ends only the run of the card it is called for (round-4 K5,
r1 N2, review SHOULD-2).

The card hides "Stop run" on a review card that is not the agent's live run
(TaskActions.tsx). The API did not: ``POST /boards/{b}/tasks/B/stop`` on any
``review`` card set the agent idle and ``run_control=stopped``, and the poll's
``stopped`` state made the bridge end the session — the run of card A.

Now a review card is stopped only when the agent's run is provably this card
(``current_task_id`` or the bridge's run record, ``acp-task-state.json``).
And for every card, the agent is set idle only when it does not provably run
a DIFFERENT card.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.fixture
def home(tmp_path, monkeypatch):
    from app.services import omp_chat

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    return tmp_path


def _write_run_record(home, slug: str, task_id: str, phase: str = "working") -> None:
    root = home / ".mc" / "agents" / slug / "omp-sessions"
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    (root / "acp-task-state.json").write_text(json.dumps({
        "version": 1, "phase": phase, "taskId": task_id, "since": now, "updatedAt": now,
    }))


async def _reviewer(make_agent, board_id, slug: str, **kw):
    return await make_agent(
        name=f"Rev {slug}", slug=slug, board_id=board_id, agent_runtime="cli-bridge",
        harness="omp", status="working", run_state="running", **kw,
    )


async def _stop(task_id):
    from app.services.operations import stop_task_run

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            result = await stop_task_run(s, task_id, "user-1", "test")
            await s.commit()
            return result


async def _reload(model, obj_id):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await s.get(model, obj_id)


@pytest.mark.asyncio
async def test_review_card_of_another_run_is_refused(make_board, make_agent, make_task, home):
    from app.models.agent import Agent
    from app.models.task import Task

    board = await make_board(name="K5 stop", slug="k5-stop-a")
    rev = await _reviewer(make_agent, board.id, "k5-rev-a")
    card_a = await make_task(board_id=board.id, title="A", status="review", assigned_agent_id=rev.id,
                             dispatched_at=datetime.utcnow())
    card_b = await make_task(board_id=board.id, title="B", status="review", assigned_agent_id=rev.id,
                             dispatched_at=datetime.utcnow())
    _write_run_record(home, "k5-rev-a", str(card_a.id))

    with pytest.raises(HTTPException) as exc:
        await _stop(card_b.id)
    assert exc.value.status_code == 409

    b = await _reload(Task, card_b.id)
    assert b.status == "review" and b.run_control != "stopped"
    agent = await _reload(Agent, rev.id)
    assert agent.run_state == "running"


@pytest.mark.asyncio
async def test_review_card_without_any_run_proof_is_refused(make_board, make_agent, make_task, home):
    board = await make_board(name="K5 stop", slug="k5-stop-b")
    rev = await _reviewer(make_agent, board.id, "k5-rev-b")
    card = await make_task(board_id=board.id, title="B", status="review", assigned_agent_id=rev.id,
                           dispatched_at=datetime.utcnow())
    with pytest.raises(HTTPException) as exc:
        await _stop(card.id)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_review_card_named_by_the_run_record_is_stopped(make_board, make_agent, make_task, home):
    from app.models.agent import Agent

    board = await make_board(name="K5 stop", slug="k5-stop-c")
    rev = await _reviewer(make_agent, board.id, "k5-rev-c")
    card = await make_task(board_id=board.id, title="A", status="review", assigned_agent_id=rev.id,
                           dispatched_at=datetime.utcnow())
    _write_run_record(home, "k5-rev-c", str(card.id), phase="delivered")

    stopped = await _stop(card.id)
    assert stopped.run_control == "stopped" and stopped.status == "blocked"
    agent = await _reload(Agent, rev.id)
    assert agent.run_state == "idle"


@pytest.mark.asyncio
async def test_review_card_with_the_agent_pointer_is_stopped(make_board, make_agent, make_task, home):
    board = await make_board(name="K5 stop", slug="k5-stop-d")
    rev = await _reviewer(make_agent, board.id, "k5-rev-d")
    card = await make_task(board_id=board.id, title="A", status="review", assigned_agent_id=rev.id,
                           dispatched_at=datetime.utcnow())
    from app.models.agent import Agent
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, rev.id)
        a.current_task_id = card.id
        s.add(a)
        await s.commit()

    stopped = await _stop(card.id)
    assert stopped.run_control == "stopped"
    agent = await _reload(Agent, rev.id)
    assert agent.run_state == "idle" and agent.current_task_id is None


@pytest.mark.asyncio
async def test_stopping_a_card_keeps_the_agent_running_when_it_runs_another(make_board, make_agent, make_task, home):
    """An in_progress/dispatched card is still stoppable, but the agent is not
    set idle while it provably runs a different card."""
    from app.models.agent import Agent

    board = await make_board(name="K5 stop", slug="k5-stop-e")
    rev = await _reviewer(make_agent, board.id, "k5-rev-e")
    running = await make_task(board_id=board.id, title="Running", status="in_progress", assigned_agent_id=rev.id)
    queued = await make_task(board_id=board.id, title="Queued", status="inbox", assigned_agent_id=rev.id,
                             dispatched_at=datetime.utcnow())
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, rev.id)
        a.current_task_id = running.id
        s.add(a)
        await s.commit()

    stopped = await _stop(queued.id)
    assert stopped.run_control == "stopped"
    agent = await _reload(Agent, rev.id)
    assert agent.run_state == "running"
    assert agent.current_task_id == running.id


@pytest.mark.asyncio
async def test_stopping_the_running_card_still_sets_the_agent_idle(make_board, make_agent, make_task, home):
    from app.models.agent import Agent

    board = await make_board(name="K5 stop", slug="k5-stop-f")
    rev = await _reviewer(make_agent, board.id, "k5-rev-f")
    running = await make_task(board_id=board.id, title="Running", status="in_progress", assigned_agent_id=rev.id)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        a = await s.get(Agent, rev.id)
        a.current_task_id = running.id
        s.add(a)
        await s.commit()

    await _stop(running.id)
    agent = await _reload(Agent, rev.id)
    assert agent.run_state == "idle" and agent.current_task_id is None


@pytest.mark.asyncio
async def test_endpoint_returns_409_for_another_runs_review_card(auth_client, make_board, make_agent, make_task, home):
    board = await make_board(name="K5 stop", slug="k5-stop-g")
    rev = await _reviewer(make_agent, board.id, "k5-rev-g")
    card_a = await make_task(board_id=board.id, title="A", status="review", assigned_agent_id=rev.id,
                             dispatched_at=datetime.utcnow())
    card_b = await make_task(board_id=board.id, title="B", status="review", assigned_agent_id=rev.id,
                             dispatched_at=datetime.utcnow())
    _write_run_record(home, "k5-rev-g", str(card_a.id))

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await auth_client.post(f"/api/v1/boards/{board.id}/tasks/{card_b.id}/stop", json={})
    assert resp.status_code == 409, resp.text
