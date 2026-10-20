"""A paused agent still takes a card assigned to it by name, through its own
poll; automatic delivery (push dispatch) stays off.

ADR-089 §4 says automatic delivery of jobs to the local omp agent resumes
only after the security step B4. The Nachtrag of 2026-10-20 states what that
covers. The paths that ask `check_dispatch_allowed` (rule
R-paused-no-dispatch) refuse a paused agent. `/agent/me/poll` does not read
`operational_mode` (rule R-paused-poll-takes-assigned), so a card assigned
to the agent by name is still delivered when the agent polls, whoever
assigned it. The undispatched-card watcher does not read the mode either: it
prepares the workspace of a card already assigned to a paused cli-bridge
agent. The line holds for every paused agent whose poller runs, not only for
the local omp agent.

These tests pin that line, so no side can flip quietly:

- push refuses, and the card stays in the inbox, still assigned;
- the agent's own poll then delivers exactly that card;
- the poll never takes a card that is unassigned or assigned to someone else;
- the undispatched-card watcher prepares an assigned card for the paused
  agent, and the poll still delivers it afterwards.

If one of them goes red, the behaviour changed and the Nachtrag in
docs/decisions/089-lead-agent-with-hands.md has to change with it.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _seed_paused_agent(make_board, make_agent):
    """Board with auto-dispatch on and one paused poll-based agent."""
    from app.auth import generate_agent_token

    board = await make_board(
        name=f"Paused-{uuid.uuid4().hex[:8]}",
        slug=f"paused-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    token_raw, token_hash = generate_agent_token()
    agent = await make_agent(
        name="local-omp",
        role="developer",
        board_id=board.id,
        agent_runtime="cli-bridge",
        operational_mode="paused",
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned",
        # The probe card is ad-hoc; no git workspace setup is involved in
        # what these tests check.
        requires_git_workflow=False,
    )
    return board, agent, token_raw


async def _push_dispatch(task_id: uuid.UUID, board_id: uuid.UUID) -> None:
    """The push path `auto_dispatch_task`: a new card, and the operator's
    re-assign on the card in the app. `mc reassign` and the undispatched-card
    watcher do not go through it (see the watcher test below)."""
    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task_id, board_id)


async def _poll(client, token: str) -> dict:
    resp = await client.get(
        "/api/v1/agent/me/poll",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_paused_agent_gets_its_assigned_card_by_poll_after_push_refused(
    client, fake_redis, make_board, make_agent, make_task,
):
    """The operator's flow: a card assigned to the paused agent by name.

    Push dispatch refuses (the agent is paused) and leaves the card in the
    inbox, still assigned and undelivered. The agent's next poll then
    delivers that card.
    """
    from app.models.agent import Agent
    from app.models.task import Task

    board, agent, token = await _seed_paused_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id,
        title="Small job for the paused agent",
        status="inbox",
        assigned_agent_id=agent.id,
    )

    # 1. Push dispatch: refused for a paused agent.
    await _push_dispatch(card.id, board.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        after_push = await s.get(Task, card.id)
        assert after_push.status == "inbox"
        assert after_push.assigned_agent_id == agent.id, (
            "a refused push must leave the card assigned to the paused agent"
        )
        assert after_push.dispatched_at is None, (
            "push dispatch delivered a card to a paused agent — automatic "
            "delivery must stay off until B4 (ADR-089 §4 Nachtrag 2026-10-20)"
        )
        assert after_push.dispatch_attempt_id is None

    # 2. The agent's own poll: delivers the explicitly assigned card.
    body = await _poll(client, token)
    assert body["state"] == "new_task", (
        f"a paused agent must still get a card assigned to it by poll "
        f"(ADR-089 §4 Nachtrag 2026-10-20); got state={body['state']!r}"
    )
    assert body["task"]["id"] == str(card.id)
    assert body["task"]["assigned_agent_id"] == str(agent.id)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        claimed = await s.get(Task, card.id)
        # Claim semantics are the normal ones: delivered, not yet acked.
        assert claimed.status == "inbox"
        assert claimed.dispatched_at is not None
        assert claimed.dispatch_attempt_id is not None
        polled_agent = await s.get(Agent, agent.id)
        # Delivery does not unpause the agent.
        assert polled_agent.operational_mode == "paused"
        assert polled_agent.current_task_id == card.id


@pytest.mark.asyncio
async def test_paused_agent_poll_takes_only_cards_assigned_to_it(
    client, fake_redis, make_board, make_agent, make_task,
):
    """The poll is not a routing path: an unassigned card and a card for
    another agent on the same board stay where they are."""
    from app.models.task import Task

    board, agent, token = await _seed_paused_agent(make_board, make_agent)
    other = await make_agent(
        name="other-agent", role="developer", board_id=board.id,
    )
    unassigned = await make_task(
        board_id=board.id, title="Nobody's card", status="inbox",
    )
    foreign = await make_task(
        board_id=board.id, title="Someone else's card", status="inbox",
        assigned_agent_id=other.id,
    )

    body = await _poll(client, token)
    assert body["state"] == "idle", (
        f"the paused agent's poll took a card that was not assigned to it: {body}"
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for card_id, owner in ((unassigned.id, None), (foreign.id, other.id)):
            row = await s.get(Task, card_id)
            assert row.assigned_agent_id == owner
            assert row.dispatched_at is None


@pytest.mark.asyncio
async def test_undispatched_watcher_prepares_assigned_card_of_paused_agent(
    client, fake_redis, make_board, make_agent, make_task,
):
    """The second path to the agent does not read the mode.

    `_check_undispatched_tasks` re-dispatches a card that is assigned but has
    no `dispatched_at`. For a cli-bridge agent that means: prepare the
    workspace (`dispatch_to_cli_bridge`), stamp `dispatched_at` and show the
    agent as running. It does this for a paused agent too. It only acts on
    cards that are already assigned, and the poll still delivers the card
    afterwards. The agent stays paused.
    """
    from datetime import timedelta

    from app.models.agent import Agent
    from app.models.task import Task
    from app.services.watchdog.task_monitor import TaskMonitorMixin
    from app.utils import utcnow

    board, agent, token = await _seed_paused_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id,
        title="Assigned card that waits for the watcher",
        status="inbox",
        assigned_agent_id=agent.id,
    )

    await _push_dispatch(card.id, board.id)

    # Past the watcher's 5 s grace window.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Task, card.id)
        assert row.dispatched_at is None
        row.updated_at = utcnow() - timedelta(minutes=5)
        s.add(row)
        await s.commit()

    prepared = AsyncMock(return_value=True)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge", prepared), \
             patch("app.services.dispatch._build_dispatch_message",
                   AsyncMock(return_value="brief")):
            await TaskMonitorMixin()._check_undispatched_tasks(s)

    assert prepared.await_count == 1, (
        "the undispatched-card watcher no longer prepares an assigned card "
        "for a paused cli-bridge agent — update ADR-089 Nachtrag 2026-10-20 "
        "(b)/(d): the immediate poll claim is then the only way in, unprepared"
    )
    assert prepared.await_args.args[0].id == agent.id
    assert prepared.await_args.args[1].id == card.id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        stamped = await s.get(Task, card.id)
        assert stamped.status == "inbox"
        assert stamped.dispatched_at is not None
        watched_agent = await s.get(Agent, agent.id)
        assert watched_agent.operational_mode == "paused"
        # Known display side effect, named in the Nachtrag (d).
        assert watched_agent.run_state == "running"

    body = await _poll(client, token)
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(card.id)
