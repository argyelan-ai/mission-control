"""A paused agent gets no card, not even through its own poll.

Pause is the operator's stop for one agent. Push dispatch has always refused
a paused agent (rule R-paused-no-dispatch). Since ADR-089 Nachtrag
2026-11-13 the agent's own poll and the undispatched-card watcher do too
(rule R-paused-holds-poll); before, a paused agent's poll still claimed any
card assigned to it by name — and a worker over MCP on a machine MC does not
manage could not be stopped by "Pause".

These tests pin the line:

- push refuses, the poll holds (``idle`` + ``agent_paused``), and the card
  stays in the inbox, still assigned and undelivered;
- the first poll after the operator sets the agent active delivers it;
- the hold covers every card the poll hands out: a phase approval and a
  dispatched card whose prompt never arrived, not only a fresh one;
- a card already in flight (delivered, no ACK yet) keeps its answer;
- running work (an ACKed card) is still reported as ``working``;
- the restart of an orphaned run waits (the poll keeps saying ``working``);
- pull dispatch answers 204;
- the ACK and pending ladders neither retry nor escalate a waiting card;
- under DRAINING the answer names the pause;
- the undispatched-card watcher does not prepare a paused agent's card;
- the poll never takes an unassigned or foreign card.

If one of them goes red, the behaviour changed and the Nachtrag in
docs/decisions/089-lead-agent-with-hands.md has to change with it.
"""

import datetime
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from tests.conftest import test_engine
from tests.test_poll_respects_system_halt import (  # noqa: F401 — _no_update_drain is an autouse fixture
    _agent,
    _card,
    _deliver,
    _no_update_drain,
    _poll,
    _seed_agent,
    _session,
    _set_mode,
)
from tests.test_system_mode_holds_every_path import _ago, _events, _set, _tick


async def _set_paused(agent_id, paused: bool) -> None:
    from app.models.agent import Agent

    async with _session() as s:
        row = await s.get(Agent, agent_id)
        row.operational_mode = "paused" if paused else "active"
        s.add(row)
        await s.commit()


async def _push_dispatch(task_id, board_id) -> None:
    """The push path `auto_dispatch_task`: a new card, and the operator's
    re-assign on the card in the app."""
    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task_id, board_id)


def _assert_paused_hold(body: dict, agent_name: str) -> None:
    assert body["state"] == "idle", f"the poll handed a card to a paused agent: {body}"
    assert body.get("agent_paused") is True, body
    assert body.get("detail") == f"Agent {agent_name} PAUSED", body
    assert "task" not in body, body
    # Same response family as the other holds: comments still arrive.
    assert "new_comments" in body and "my_agent_id" in body, body


@pytest.mark.asyncio
async def test_paused_agent_gets_no_card_until_set_active(
    client, make_board, make_agent, make_task,
):
    """The operator's flow: pause the agent, and nothing reaches it — neither
    by push nor by its own poll. Active again: the next poll delivers."""
    from app.models.task import Task

    board, agent, token = await _seed_agent(
        make_board, make_agent, operational_mode="paused", agent_runtime="cli-bridge",
    )
    await _set_board_auto_dispatch(board.id)
    card = await make_task(
        board_id=board.id, title="Card for the paused agent", status="inbox",
        assigned_agent_id=agent.id,
    )

    await _push_dispatch(card.id, board.id)
    body = await _poll(client, token)
    _assert_paused_hold(body, agent.name)

    async with _session() as s:
        row = await s.get(Task, card.id)
        assert row.status == "inbox"
        assert row.assigned_agent_id == agent.id, "the hold must leave the card assigned"
        assert row.dispatched_at is None and row.dispatch_attempt_id is None
    assert (await _agent(agent.id)).current_task_id is None

    await _set_paused(agent.id, False)
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body
    assert (await _card(card.id)).dispatched_at is not None


async def _set_board_auto_dispatch(board_id) -> None:
    from app.models.board import Board

    async with _session() as s:
        row = await s.get(Board, board_id)
        row.auto_dispatch_enabled = True
        s.add(row)
        await s.commit()


@pytest.mark.asyncio
async def test_paused_poll_holds_a_phase_approval_card(
    client, make_board, make_agent, make_task,
):
    """The phase-approval branch picks its card before the inbox branch —
    the pause must hold it too."""
    board, lead, token = await _seed_agent(
        make_board, make_agent, is_board_lead=True, operational_mode="paused",
    )
    approval = await make_task(
        board_id=board.id, title="Phase Approval: parent", status="inbox",
        assigned_agent_id=lead.id, delegation_type="phase_approval",
    )

    _assert_paused_hold(await _poll(client, token), lead.name)
    assert (await _card(approval.id)).dispatched_at is None


@pytest.mark.asyncio
async def test_paused_poll_holds_an_undelivered_active_card(
    client, make_board, make_agent, make_task,
):
    """A card dispatched past the inbox whose prompt never reached the agent
    (ack_at empty) is not delivered while the agent is paused."""
    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    card = await make_task(
        board_id=board.id, title="direct dispatch", status="in_progress",
        assigned_agent_id=agent.id,
        dispatched_at=datetime.datetime.now(tz=datetime.timezone.utc),
    )

    _assert_paused_hold(await _poll(client, token), agent.name)
    assert (await _card(card.id)).ack_at is None

    await _set_paused(agent.id, False)
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body


@pytest.mark.asyncio
async def test_a_card_in_flight_keeps_its_answer_when_paused(
    client, make_board, make_agent, make_task,
):
    """Paused between delivery and ACK: the prompt is already in the session.
    An ``idle`` answer would make the bridges drop the card and paste it a
    second time later (see the HALTED twin in test_poll_respects_system_halt).
    The poll keeps the same ``new_task`` with the same attempt."""
    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="in flight", status="inbox", assigned_agent_id=agent.id,
    )
    attempt = await _deliver(client, token, card.id)

    await _set_paused(agent.id, True)
    body = await _poll(client, token)
    assert body["state"] == "new_task", f"an in-flight card was answered {body}"
    assert body["task"]["dispatch_attempt_id"] == attempt, body
    assert "agent_paused" not in body, body


@pytest.mark.asyncio
async def test_paused_agent_still_reports_its_running_work(
    client, make_board, make_agent, make_task,
):
    """Pause stops new cards, not the card the agent already accepted."""
    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    card = await make_task(
        board_id=board.id, title="accepted before the pause", status="in_progress",
        assigned_agent_id=agent.id, dispatched_at=now, ack_at=now,
    )
    async with _session() as s:
        from app.models.agent import Agent
        row = await s.get(Agent, agent.id)
        row.current_task_id = card.id
        row.last_task_activity_at = now
        s.add(row)
        await s.commit()

    body = await _poll(client, token)
    assert body["state"] == "working" and body["task_id"] == str(card.id), body


@pytest.mark.asyncio
async def test_paused_agent_poll_takes_only_cards_assigned_to_it(
    client, make_board, make_agent, make_task,
):
    """The poll is not a routing path: an unassigned card and a card for
    another agent on the same board stay where they are."""
    from app.models.task import Task

    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    other = await make_agent(name="other-agent", role="developer", board_id=board.id)
    unassigned = await make_task(board_id=board.id, title="Nobody's card", status="inbox")
    foreign = await make_task(
        board_id=board.id, title="Someone else's card", status="inbox",
        assigned_agent_id=other.id,
    )

    body = await _poll(client, token)
    assert body["state"] == "idle" and "task" not in body, body

    async with _session() as s:
        for card_id, owner in ((unassigned.id, None), (foreign.id, other.id)):
            row = await s.get(Task, card_id)
            assert row.assigned_agent_id == owner
            assert row.dispatched_at is None


@pytest.mark.asyncio
async def test_undispatched_watcher_leaves_a_paused_agents_card(
    client, make_board, make_agent, make_task,
):
    """`_check_undispatched_tasks` prepares an assigned card that has no
    `dispatched_at` — but not while its agent is paused: that would show
    the agent as running. Active again, the next pass prepares it."""
    from app.models.task import Task
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.utils import utcnow

    board, agent, token = await _seed_agent(
        make_board, make_agent, operational_mode="paused", agent_runtime="cli-bridge",
    )
    card = await make_task(
        board_id=board.id, title="Assigned card", status="inbox", assigned_agent_id=agent.id,
    )
    # Past the watcher's 5 s grace window.
    async with _session() as s:
        row = await s.get(Task, card.id)
        row.updated_at = utcnow() - timedelta(minutes=5)
        s.add(row)
        await s.commit()

    async def _watch() -> AsyncMock:
        prepared = AsyncMock(return_value=True)
        async with _session() as s:
            with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge", prepared), \
                 patch("app.services.dispatch._build_dispatch_message",
                       AsyncMock(return_value="brief")):
                await TaskMonitorMixin()._check_undispatched_tasks(s)
        return prepared

    prepared = await _watch()
    assert prepared.await_count == 0, "the watcher prepared a paused agent's card"
    assert (await _card(card.id)).dispatched_at is None
    assert (await _agent(agent.id)).run_state != "running"

    await _set_paused(agent.id, False)
    prepared = await _watch()
    assert prepared.await_count == 1
    assert (await _card(card.id)).dispatched_at is not None


@pytest.mark.asyncio
async def test_paused_poll_does_not_restart_an_orphaned_run(
    client, make_board, make_agent, make_task,
):
    """A restart hands the full prompt out again: new work. While paused
    the poll keeps reporting ``working`` and changes nothing; active again,
    the next poll restarts the run."""
    stale = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=900)
    board, agent, token = await _seed_agent(
        make_board, make_agent, operational_mode="paused", last_task_activity_at=stale,
    )
    card = await make_task(
        board_id=board.id, title="orphaned run", status="in_progress",
        assigned_agent_id=agent.id, ack_at=stale, dispatched_at=stale,
        dispatch_attempt_id="attempt-before-pause",
    )

    body = await _poll(client, token)
    assert body["state"] == "working" and body["task_id"] == str(card.id), body
    held = await _card(card.id)
    assert held.ack_at is not None and held.dispatch_attempt_id == "attempt-before-pause"

    await _set_paused(agent.id, False)
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body.get("orphaned_run_redispatched") is True, body


@pytest.mark.asyncio
async def test_pull_dispatch_hands_a_paused_agent_nothing(
    client, make_board, make_agent, make_task,
):
    """``GET /agent/boards/{id}/tasks/next`` is in every TOOLS.md with
    tasks:read — it must not be a way around the pause. (A lead takes root
    cards by pull; a worker only continuation flows.)"""
    board, agent, token = await _seed_agent(
        make_board, make_agent, operational_mode="paused", is_board_lead=True,
    )
    card = await make_task(
        board_id=board.id, title="pull me", status="inbox", assigned_agent_id=agent.id,
    )

    async def _pull():
        return await client.get(
            f"/api/v1/agent/boards/{board.id}/tasks/next",
            headers={"Authorization": f"Bearer {token}"},
        )

    resp = await _pull()
    assert resp.status_code == 204, f"pull dispatch handed a paused agent a card: {resp.text}"
    row = await _card(card.id)
    assert row.status == "inbox" and row.ack_at is None

    await _set_paused(agent.id, False)
    resp = await _pull()
    assert resp.status_code == 200 and resp.json()["task"]["id"] == str(card.id), resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [False, True], ids=["pending", "ack"])
async def test_ladders_do_not_escalate_a_card_waiting_on_a_paused_agent(
    make_board, make_agent, make_task, stamped,
):
    """The card waits because the operator paused the agent, not because the
    agent fails to react: no "has not reacted" escalation (an approval and a
    phone push), no silent retry, no model-wait return."""
    board = await make_board(name="Ladder", slug=f"ladder-{__import__('uuid').uuid4().hex[:8]}")
    agent = await make_agent(
        name="paused-dev", board_id=board.id, role="developer",
        agent_runtime="cli-bridge", operational_mode="paused",
    )
    card = await make_task(
        board_id=board.id, title="waits on a paused agent", status="inbox",
        assigned_agent_id=agent.id,
        dispatched_at=_ago(16) if stamped else None,
        dispatch_attempt_id="attempt-before-pause" if stamped else None,
    )
    await _set(card.id, updated_at=_ago(30))

    escalate, model_wait = await _tick()

    assert escalate.await_count == 0, "operator escalation for a card that waits on a pause"
    assert model_wait.await_count == 0
    assert await _events(card.id, "task.dispatch_pending") == []
    assert (await _card(card.id)).dispatch_attempt_id == (
        "attempt-before-pause" if stamped else None
    ), "the ladder rotated the attempt of a card that waits on a pause"


@pytest.mark.asyncio
async def test_under_draining_the_pause_is_named(
    client, make_board, make_agent, make_task,
):
    """Both hold a fresh root card. The fresh-card gates ask HALTED, then the
    pause, then DRAINING last (its gate sits at the end of the poll): the
    answer names the pause, the operator's own switch for this agent."""
    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    await make_task(
        board_id=board.id, title="root work", status="inbox", assigned_agent_id=agent.id,
    )

    await _set_mode("draining")
    try:
        body = await _poll(client, token)
    finally:
        await _set_mode("active")
    _assert_paused_hold(body, agent.name)
    assert "system_draining" not in body, body


@pytest.mark.asyncio
async def test_paused_reviewer_gets_no_undelivered_review_card(
    client, make_board, make_agent, make_task,
):
    """An unacked review card is handed out once to its reviewer — not while
    the reviewer is paused."""
    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    card = await make_task(
        board_id=board.id, title="please review", status="review", assigned_agent_id=agent.id,
    )

    _assert_paused_hold(await _poll(client, token), agent.name)
    assert (await _card(card.id)).ack_at is None
