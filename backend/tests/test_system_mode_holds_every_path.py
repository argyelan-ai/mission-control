"""The system mode holds every path that hands a card out, not only the poll.

Companion of ``test_poll_respects_system_halt.py``. Holding the poll claim
alone left three neighbours that read no mode:

- The undispatched-card watcher (``_check_undispatched_tasks``) prepared the
  workspace of a held root card for a cli-bridge agent and stamped
  ``dispatched_at`` under DRAINING (``watchdog/core.py`` skips it only for
  HALTED and the update drain).
- The ACK ladder (``task_runner._check_dispatch_ack``) then counted a card
  the system itself was holding as "not ACKed": silent retry (a new attempt)
  at half the ACK timeout, operator escalation "kein ACK nach Dispatch" at
  the full one. The pending ladder ("Dispatch nie zugestellt") did the same
  for a held card that was never stamped. Both now wait while the system
  mode or the MC update drain holds the card, and for one ACK timeout plus
  margin after the hold ends, so the first poll after it can deliver.
- Pull dispatch (``GET /agent/boards/{id}/tasks/next``) claimed a card
  straight to ``in_progress``: now held by the same rule (204).

Decided, not held: ``/agent/me/active-task-recovery``. Every poller calls it
once at its own start (poll.sh FIRST_POLL, the omp bridge, the host poll
template) to restore the card it already owns; it reuses the card's attempt.
Held there, the card would not come back for the rest of that run (a Board
Lead's card has no orphan restart behind it). Running work is not the
brake's to stop; the orphan restart, which hands out a fresh attempt and
runs on every poll, is held.
"""
from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from tests.conftest import test_engine


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


def _ago(minutes: float) -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(minutes=minutes)


@pytest.fixture(autouse=True)
def _no_update_drain():
    from app.services import update_drain

    update_drain.reset_cache()
    yield
    update_drain.reset_cache()


async def _set_mode(mode: str) -> None:
    from app.services.operations import set_system_mode

    await set_system_mode(mode, "test", f"test sets {mode}")


async def _set_update_drain() -> None:
    from app.models.system_update import DRAIN_KEY, SystemState

    expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)
    async with _session() as s:
        s.add(SystemState(key=DRAIN_KEY, value={
            "run_id": "20261020T120000Z-test",
            "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "set_by": "mc_update",
        }))
        await s.commit()


async def _card(task_id):
    from app.models.task import Task

    async with _session() as s:
        return await s.get(Task, task_id)


async def _set(task_id, **fields) -> None:
    from app.models.task import Task

    async with _session() as s:
        row = await s.get(Task, task_id)
        for k, v in fields.items():
            setattr(row, k, v)
        s.add(row)
        await s.commit()


async def _events(task_id, event_type: str) -> list:
    from app.models.activity import ActivityEvent

    async with _session() as s:
        return (await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.task_id == task_id,
                ActivityEvent.event_type == event_type,
            )
        )).all()


# ── the undispatched-card watcher ───────────────────────────────────────────


async def _watch():
    """One pass of the watcher with the workspace preparation and the
    host branch's auto dispatch replaced by spies."""
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    prepared = AsyncMock(return_value=True)
    auto = AsyncMock(return_value=None)
    async with _session() as s:
        with patch("app.services.cli_bridge_runner.dispatch_to_cli_bridge", prepared), \
             patch("app.services.dispatch.auto_dispatch_task", auto), \
             patch("app.services.dispatch._build_dispatch_message", AsyncMock(return_value="brief")):
            await TaskMonitorMixin()._check_undispatched_tasks(s)
    return prepared, auto


async def _watched_card(make_board, make_agent, make_task, *, runtime: str, intent: str):
    board = await make_board(name="Watch", slug=f"watch-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name=f"W-{uuid.uuid4().hex[:6]}", board_id=board.id, agent_runtime=runtime,
    )
    card = await make_task(
        board_id=board.id, title=f"{runtime}/{intent}", status="inbox",
        assigned_agent_id=agent.id, dispatch_intent=intent,
    )
    await _set(card.id, updated_at=_ago(5))  # past the watcher's 5 s grace
    return agent, card


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["draining", "halted"])
async def test_watcher_does_not_prepare_a_card_the_system_holds(
    make_board, make_agent, make_task, mode,
):
    """A root card for a cli-bridge agent: no workspace preparation, no
    ``dispatched_at``, the agent not shown as running. Nothing for the ACK
    ladder to count."""
    agent, card = await _watched_card(
        make_board, make_agent, make_task, runtime="cli-bridge", intent="root",
    )

    await _set_mode(mode)
    prepared, _ = await _watch()

    assert prepared.await_count == 0, f"the watcher prepared a card held by {mode.upper()}"
    assert (await _card(card.id)).dispatched_at is None
    from app.models.agent import Agent

    async with _session() as s:
        assert (await s.get(Agent, agent.id)).run_state != "running"


@pytest.mark.asyncio
async def test_watcher_raises_no_recovery_event_for_a_held_host_card(
    make_board, make_agent, make_task,
):
    """The other runtimes go through auto_dispatch_task, which refuses a held
    card itself. The watcher no longer starts it and writes "Nachdispatcht"
    on the card's timeline every pass for a card the drain holds."""
    _, card = await _watched_card(
        make_board, make_agent, make_task, runtime="host", intent="root",
    )

    await _set_mode("draining")
    _, auto = await _watch()

    assert auto.call_count == 0
    assert await _events(card.id, "task.undispatched_recovery") == []


@pytest.mark.asyncio
async def test_watcher_still_prepares_a_continuation_flow_under_draining(
    make_board, make_agent, make_task,
):
    """DRAINING lets running work finish: a subtask is prepared as before."""
    _, card = await _watched_card(
        make_board, make_agent, make_task, runtime="cli-bridge", intent="subtask",
    )

    await _set_mode("draining")
    prepared, _ = await _watch()

    assert prepared.await_count == 1
    assert (await _card(card.id)).dispatched_at is not None


# ── the ACK ladder and the pending ladder ───────────────────────────────────


async def _ladder_agent(make_board, make_agent, runtime: str = "cli-bridge"):
    board = await make_board(name="Ladder", slug=f"ladder-{uuid.uuid4().hex[:8]}")
    agent = await make_agent(
        name=f"L-{uuid.uuid4().hex[:6]}", board_id=board.id, is_board_lead=False,
        role="developer", agent_runtime=runtime,
    )
    return board, agent


async def _tick():
    """One pass of the ACK/pending ladder. Escalations and the model-wait
    rule are spies; everything else (rotation, Redis keys) is real."""
    from app.services.task_runner import task_runner

    escalate = AsyncMock(return_value="notice")
    model_wait = AsyncMock(return_value=False)
    with patch.object(task_runner, "_create_dispatch_approval", escalate), \
         patch.object(task_runner, "_return_if_model_unreachable", model_wait):
        async with _session() as s:
            await task_runner._check_dispatch_ack(s)
    return escalate, model_wait


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["halted", "draining"])
@pytest.mark.parametrize("minutes", [8, 16], ids=["retry-window", "escalation"])
async def test_ack_ladder_waits_while_the_system_holds_the_card(
    make_board, make_agent, make_task, mode, minutes,
):
    """A root card dispatched 8 min (silent retry: new attempt) or 16 min
    (operator escalation) ago, for a cli-bridge agent (ACK timeout 15 min).
    While the system holds it, the ladder rotates nothing, escalates
    nothing and does not run the model-wait rule."""
    board, agent = await _ladder_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="held, not ACKed", status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_ago(minutes),
        dispatch_attempt_id="attempt-before-hold",
    )

    await _set_mode(mode)
    escalate, model_wait = await _tick()

    assert escalate.await_count == 0, f"operator escalation for a card {mode.upper()} holds"
    assert (await _card(card.id)).dispatch_attempt_id == "attempt-before-hold", (
        "the ladder rotated the attempt of a held card"
    )
    assert model_wait.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["halted", "draining"])
async def test_pending_ladder_waits_while_the_system_holds_the_card(
    make_board, make_agent, make_task, mode,
):
    """A held root card that was never stamped (``dispatched_at`` empty) is
    not "Dispatch nie zugestellt": nothing was supposed to deliver it."""
    board, agent = await _ladder_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="held, never stamped", status="inbox", assigned_agent_id=agent.id,
    )
    await _set(card.id, updated_at=_ago(120))

    await _set_mode(mode)
    escalate, _ = await _tick()

    assert escalate.await_count == 0
    assert await _events(card.id, "task.dispatch_pending") == []


@pytest.mark.asyncio
async def test_ack_ladder_waits_while_the_update_drain_holds_the_card(
    make_board, make_agent, make_task,
):
    """The MC update drain holds every card the same way (ADR-094)."""
    board, agent = await _ladder_agent(make_board, make_agent)
    await make_task(
        board_id=board.id, title="held by the update", status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_ago(16), dispatch_attempt_id="a",
    )

    await _set_update_drain()
    escalate, _ = await _tick()
    assert escalate.await_count == 0


@pytest.mark.asyncio
async def test_ack_ladder_still_escalates_a_card_the_mode_lets_through(
    make_board, make_agent, make_task,
):
    """DRAINING lets a subtask through, so its ladder runs as before."""
    board, agent = await _ladder_agent(make_board, make_agent)
    await make_task(
        board_id=board.id, title="subtask, not ACKed", status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_ago(16),
        dispatch_attempt_id="a", dispatch_intent="subtask",
    )

    await _set_mode("draining")
    escalate, _ = await _tick()
    assert escalate.await_count == 1


@pytest.mark.asyncio
async def test_ack_ladder_gives_one_ack_timeout_after_the_hold_ends(
    make_board, make_agent, make_task,
):
    """After the halt, ``dispatched_at`` of a held card is old. Without a
    grace window the first tick after the halt would escalate before the
    first poll could deliver it. The ladder waits one ACK timeout plus
    margin after the last held tick, then runs as before."""
    from app.redis_client import RedisKeys, get_redis
    from app.services.task_runner import (
        AGENT_RUNTIME_ACK_TIMEOUTS,
        RESUME_SUPPRESS_MARGIN_MINUTES,
    )

    board, agent = await _ladder_agent(make_board, make_agent, runtime="host")
    card = await make_task(
        board_id=board.id, title="held across the halt", status="inbox",
        assigned_agent_id=agent.id, dispatched_at=_ago(30), dispatch_attempt_id="a",
    )

    await _set_mode("halted")
    escalate, _ = await _tick()
    assert escalate.await_count == 0

    redis = await get_redis()
    key = RedisKeys.dispatch_system_hold(str(card.id))
    ttl = await redis.ttl(key)
    expected = (AGENT_RUNTIME_ACK_TIMEOUTS["host"] + RESUME_SUPPRESS_MARGIN_MINUTES) * 60
    assert 0 < ttl <= expected, f"grace TTL {ttl}s, expected up to {expected}s"

    await _set_mode("active")
    escalate, _ = await _tick()
    assert escalate.await_count == 0, "escalated right after the halt, before any poll could deliver"

    await redis.delete(key)  # the grace window has run out
    escalate, _ = await _tick()
    assert escalate.await_count == 1, "the ladder no longer runs after the grace window"


# ── pull dispatch: GET /agent/boards/{id}/tasks/next ─────────────────────────


async def _pull_setup(make_board, make_agent, make_task, *, lead: bool, intent: str):
    board = await make_board(name="Pull", slug=f"pull-{uuid.uuid4().hex[:8]}")
    raw, token_hash = generate_agent_token()
    agent = await make_agent(
        name=f"P-{uuid.uuid4().hex[:6]}", board_id=board.id, is_board_lead=lead,
        agent_token_hash=token_hash, scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned", requires_git_workflow=False,
    )
    parent = None
    if intent != "root":
        parent = await make_task(board_id=board.id, title="parent", status="done")
    card = await make_task(
        board_id=board.id, title=f"pull {intent}", status="inbox", assigned_agent_id=agent.id,
        dispatch_intent=intent, parent_task_id=parent.id if parent else None,
    )
    return board, raw, card


async def _pull(client, token: str, board_id):
    return await client.get(
        f"/api/v1/agent/boards/{board_id}/tasks/next",
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lead,intent", [(True, "root"), (False, "subtask")])
async def test_pull_dispatch_claims_nothing_while_halted(
    client, make_board, make_agent, make_task, lead, intent,
):
    board, token, card = await _pull_setup(make_board, make_agent, make_task, lead=lead, intent=intent)

    await _set_mode("halted")
    resp = await _pull(client, token, board.id)
    assert resp.status_code == 204, f"pull dispatch handed out a card while HALTED: {resp.text}"
    row = await _card(card.id)
    assert row.status == "inbox" and row.ack_at is None

    await _set_mode("active")
    resp = await _pull(client, token, board.id)
    assert resp.status_code == 200 and resp.json()["task"]["id"] == str(card.id), resp.text


@pytest.mark.asyncio
async def test_pull_dispatch_under_draining_holds_root_work_only(
    client, make_board, make_agent, make_task,
):
    board, token, root = await _pull_setup(make_board, make_agent, make_task, lead=True, intent="root")

    await _set_mode("draining")
    resp = await _pull(client, token, board.id)
    assert resp.status_code == 204, resp.text
    assert (await _card(root.id)).status == "inbox"

    board, token, sub = await _pull_setup(make_board, make_agent, make_task, lead=False, intent="subtask")
    resp = await _pull(client, token, board.id)
    assert resp.status_code == 200 and resp.json()["task"]["id"] == str(sub.id), resp.text


@pytest.mark.asyncio
async def test_pull_dispatch_claims_nothing_during_an_update(
    client, make_board, make_agent, make_task,
):
    board, token, card = await _pull_setup(make_board, make_agent, make_task, lead=False, intent="subtask")

    await _set_update_drain()
    resp = await _pull(client, token, board.id)
    assert resp.status_code == 204, resp.text
    assert (await _card(card.id)).status == "inbox"


# ── decided: recovery of the agent's own running card is not held ───────────


@pytest.mark.asyncio
async def test_active_task_recovery_restores_the_own_running_card_while_halted(
    client, make_board, make_agent, make_task,
):
    """A poller restarted during HALTED gets its own running card back, with
    the attempt it already has (nothing new is handed out). See the module
    docstring for why this path, unlike the orphan restart, is not held."""
    board = await make_board(name="Recover", slug=f"recover-{uuid.uuid4().hex[:8]}")
    raw, token_hash = generate_agent_token()
    lead = await make_agent(
        name=f"R-{uuid.uuid4().hex[:6]}", board_id=board.id, is_board_lead=True,
        agent_token_hash=token_hash, scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned", requires_git_workflow=False,
    )
    card = await make_task(
        board_id=board.id, title="running before the halt", status="in_progress",
        assigned_agent_id=lead.id, ack_at=_ago(3), dispatched_at=_ago(4),
        dispatch_attempt_id="running-attempt",
    )

    await _set_mode("halted")
    with patch("app.services.dispatch.build_agent_task_prompt", AsyncMock(return_value="prompt")):
        resp = await client.get(
            "/api/v1/agent/me/active-task-recovery",
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active"] is True and body["task"]["id"] == str(card.id), body
    assert body["task"]["dispatch_attempt_id"] == "running-attempt", body


# ── the silent-mailbox watcher and its operator escalation ──────────────────


async def _mailbox(make_board, make_agent, make_task, *, intents=("root",), minutes=20, **worker):
    """A Board Lead and a cli-bridge worker whose inbox cards have sat
    ``minutes`` with nothing in progress: past the silent-mailbox threshold
    (15 min). Returns the worker's cards in ``intents`` order."""
    board = await make_board(name="Mailbox", slug=f"mailbox-{uuid.uuid4().hex[:8]}")
    await make_agent(
        name=f"Lead-{uuid.uuid4().hex[:6]}", board_id=board.id, is_board_lead=True, role="lead",
    )
    worker = await make_agent(
        name=f"M-{uuid.uuid4().hex[:6]}", board_id=board.id, is_board_lead=False,
        role="developer", agent_runtime="cli-bridge", **worker,
    )
    parent = None
    if any(i != "root" for i in intents):
        parent = await make_task(board_id=board.id, title="parent", status="done")
    cards = []
    for intent in intents:
        cards.append(await make_task(
            board_id=board.id, title=f"queued {intent}", status="inbox",
            assigned_agent_id=worker.id, dispatch_intent=intent,
            parent_task_id=parent.id if parent and intent != "root" else None,
            created_at=_ago(minutes), updated_at=_ago(minutes),
        ))
    return worker, cards


async def _mailbox_notes() -> list:
    from app.models.task import TaskComment

    async with _session() as s:
        return (await s.exec(
            select(TaskComment).where(
                TaskComment.comment_type == "watchdog_notify",
                TaskComment.content.like("STILLE SCHLANGE%"),  # type: ignore[union-attr]
            )
        )).all()


async def _silent_mailbox_pass() -> None:
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    with patch("app.services.watchdog.task_monitor.emit_event", AsyncMock()):
        async with _session() as s:
            await TaskMonitorMixin()._check_silent_mailbox(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["halted", "draining", "update_drain"])
async def test_silent_mailbox_does_not_report_a_card_the_system_holds(
    make_board, make_agent, make_task, hold,
):
    """A root card held for 20 min is not a dead mailbox: the poll answers
    ``idle`` for it on purpose. Before, the Board Lead got a "STILLE
    SCHLANGE" note 15 min into every hold, for every agent with a held
    card."""
    await _mailbox(make_board, make_agent, make_task)

    if hold == "update_drain":
        await _set_update_drain()
    else:
        await _set_mode(hold)
    await _silent_mailbox_pass()

    assert await _mailbox_notes() == [], f"silent-mailbox alarm for a card {hold} holds"


@pytest.mark.asyncio
async def test_silent_mailbox_still_counts_the_cards_draining_lets_through(
    make_board, make_agent, make_task,
):
    """DRAINING lets a subtask through, so a worker sitting 20 min on it is
    still silent. The note counts that card only, not the held root card
    next to it."""
    _, (root, sub) = await _mailbox(
        make_board, make_agent, make_task, intents=("root", "subtask"),
    )

    await _set_mode("draining")
    await _silent_mailbox_pass()

    notes = await _mailbox_notes()
    assert len(notes) == 1, "a continuation card the worker does not pull is still reported"
    assert notes[0].task_id == sub.id
    assert "Karten in inbox: 1" in notes[0].content


@pytest.mark.asyncio
async def test_no_operator_escalation_for_a_mailbox_the_halt_holds(
    client, make_board, make_agent, make_task,
):
    """The full chain the reviewer reproduced: HALTED, the worker's poll
    answers ``idle``, its root card has waited an hour. No stage-1 note to
    the Board Lead, and so no stage-2 escalation to the operator 30 min
    later."""
    from app.models.task import TaskComment
    from app.services.watchdog.task_monitor import TaskMonitorMixin

    raw, token_hash = generate_agent_token()
    _, (card,) = await _mailbox(
        make_board, make_agent, make_task, minutes=60,
        agent_token_hash=token_hash, scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned", requires_git_workflow=False,
    )

    await _set_mode("halted")
    with patch("app.services.dispatch.build_agent_task_prompt", AsyncMock(return_value="P")):
        resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {raw}"})
    assert resp.json().get("system_halted") is True, resp.text

    await _silent_mailbox_pass()
    async with _session() as s:  # 31 min later
        for c in (await s.exec(select(TaskComment).where(TaskComment.task_id == card.id))).all():
            c.created_at = _ago(31)
            s.add(c)
        await s.commit()
    with patch("app.services.watchdog.task_monitor.emit_event", AsyncMock()), \
         patch("app.services.operator_approvals.send_approval", AsyncMock()):
        async with _session() as s:
            await TaskMonitorMixin()._check_lead_notify_escalations(s)

    async with _session() as s:
        alarms = (await s.exec(select(TaskComment).where(
            TaskComment.task_id == card.id,
            TaskComment.comment_type.in_(("watchdog_notify", "lead_escalated_notify")),  # type: ignore[union-attr]
        ))).all()
    assert alarms == [], [a.comment_type for a in alarms]


@pytest.mark.asyncio
async def test_silent_mailbox_waits_one_ack_timeout_after_the_hold_ends(
    make_board, make_agent, make_task,
):
    """Right after the halt the held card is still old. The first poll after
    it delivers the card, so the watcher waits the ladders' grace window
    (``RedisKeys.dispatch_system_hold``) before it calls the mailbox dead,
    then reports as before."""
    from app.redis_client import RedisKeys, get_redis

    _, (card,) = await _mailbox(make_board, make_agent, make_task)

    await _set_mode("halted")
    await _tick()  # the ladder marks the held card
    await _set_mode("active")
    await _silent_mailbox_pass()
    assert await _mailbox_notes() == [], "alarm right after the halt, before any poll could deliver"

    await (await get_redis()).delete(RedisKeys.dispatch_system_hold(str(card.id)))
    await _silent_mailbox_pass()
    assert len(await _mailbox_notes()) == 1, "the watcher no longer reports after the grace window"
