"""The agent's own poll respects the system mode like push dispatch does.

The system mode (Redis, ``PUT /api/v1/system/mode``) is MC's emergency
brake: HALTED holds every card, DRAINING holds every card except the
automatic continuation flows (subtask, review hand-off, rework). Push
dispatch reads it in ``operations.check_dispatch_allowed``. The poll claim
in ``/agent/me/poll`` hands cards out without going through that guard, and
until this fix it never read the mode: a HALTED system kept handing cards
to every agent that polls (omp bridge, poll.sh, hermes bridge).

Contract pinned here:

- HALTED: the poll hands out no card, on every hand-out path of the poll
  (fresh inbox card, phase approval, a dispatched card whose prompt never
  arrived, the restart of an orphaned run). The answer has the same shape
  as the update drain's (``state=idle`` + one flag + ``detail``):
  ``system_halted`` / ``system_draining``. The card stays untouched, so the
  first poll after the halt delivers it; nothing to catch up.
- DRAINING: a root card is held, a continuation flow still goes out.
- In flight: a card the poll already handed to this agent under its current
  attempt, not yet ACKed, keeps its ``new_task`` answer (same attempt). The
  bridges read ``idle`` as "card gone" and would drop and re-paste it. A
  card only stamped by push dispatch or the watcher, a new attempt, a card
  delivered to another agent and the orphan restart are not in flight.
- Poll and push dispatch decide the system mode with one rule
  (``operations.system_mode_refusal``): for every mode and dispatch intent
  the poll hands a card out exactly when ``check_dispatch_allowed`` would,
  and the fresh-card path asks its gates in push dispatch's order (HALTED,
  update drain, runtime readiness, DRAINING), answering with the same gate.
- Unchanged: a paused agent still takes a card assigned to it by name in
  normal mode (ADR-089 §4 Nachtrag, PR #911); the update drain answers as
  before outside HALTED; running work keeps reporting ``working``.
- Pool hygiene (incident 2026-09-14): every Redis call of the gate (mode,
  delivery mark) happens with no pool connection checked out.

The other paths that hand cards out (the undispatched-card watcher, the
ACK and pending ladders, pull dispatch) are pinned in
``test_system_mode_holds_every_path.py``.
"""
from __future__ import annotations

import datetime
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from tests.conftest import test_engine

PROMPT = "prompt"
INTENTS = ("root", "subtask", "review_handoff", "review_rework", "manual_redispatch")


def _session() -> AsyncSession:
    return AsyncSession(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _no_update_drain():
    """Every test starts without a cached MC update drain."""
    from app.services import update_drain

    update_drain.reset_cache()
    yield
    update_drain.reset_cache()


async def _set_mode(mode: str) -> None:
    """The real write path of PUT /api/v1/system/mode (Redis is the
    fixture's fake client, seeded by the ``client`` fixture)."""
    from app.services.operations import set_system_mode

    await set_system_mode(mode, "test", f"test sets {mode}")


async def _seed_agent(make_board, make_agent, **agent_kwargs):
    board = await make_board(
        name=f"Halt-{uuid.uuid4().hex[:8]}", slug=f"halt-{uuid.uuid4().hex[:8]}",
    )
    raw, token_hash = generate_agent_token()
    agent_kwargs.setdefault("name", f"Poller-{uuid.uuid4().hex[:6]}")
    agent = await make_agent(
        board_id=board.id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned",
        requires_git_workflow=False,
        **agent_kwargs,
    )
    return board, agent, raw


async def _poll(client, token: str) -> dict:
    with patch("app.services.dispatch.build_agent_task_prompt", return_value=PROMPT):
        resp = await client.get(
            "/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _card(task_id):
    from app.models.task import Task

    async with _session() as s:
        return await s.get(Task, task_id)


async def _agent(agent_id):
    from app.models.agent import Agent

    async with _session() as s:
        return await s.get(Agent, agent_id)


def _assert_held(body: dict, flag: str, other_flag: str, reason: str) -> None:
    assert body["state"] == "idle", (
        f"the poll handed out a card although the system is {flag[7:].upper()}: {body}"
    )
    assert body.get(flag) is True, body
    assert other_flag not in body, body
    assert body.get("detail") == reason, body
    assert "task" not in body, body
    # Same response family as the update drain: comments are still delivered.
    assert "new_comments" in body and "my_agent_id" in body, body


# ── HALTED ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_halted_poll_hands_out_no_fresh_inbox_card(
    client, make_board, make_agent, make_task,
):
    """The core hole: HALTED, a fresh card assigned to a polling agent. The
    poll must hold it, leave it untouched, and deliver it on the first poll
    after the halt is lifted."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="fresh", status="inbox", assigned_agent_id=agent.id,
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)

    held = await _card(card.id)
    assert held.status == "inbox"
    assert held.dispatched_at is None, "a held card must not be stamped as delivered"
    assert held.dispatch_attempt_id is None
    assert (await _agent(agent.id)).current_task_id is None

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(card.id)


@pytest.mark.asyncio
async def test_halted_poll_holds_a_phase_approval_card(
    client, make_board, make_agent, make_task,
):
    """The phase-approval branch picks its card before the inbox branch —
    HALTED must hold it too."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, lead, token = await _seed_agent(make_board, make_agent, is_board_lead=True)
    approval = await make_task(
        board_id=board.id, title="Phase Approval: parent", status="inbox",
        assigned_agent_id=lead.id, delegation_type="phase_approval",
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    assert (await _card(approval.id)).dispatched_at is None

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(approval.id), body


@pytest.mark.asyncio
async def test_halted_poll_does_not_deliver_an_undelivered_active_card(
    client, make_board, make_agent, make_task,
):
    """A card dispatched past the inbox whose prompt never reached the agent
    (ack_at empty) is delivered by the poll — not while HALTED."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="direct dispatch", status="in_progress",
        assigned_agent_id=agent.id,
        dispatched_at=datetime.datetime.now(tz=datetime.timezone.utc),
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    assert (await _card(card.id)).ack_at is None

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body


@pytest.mark.asyncio
async def test_halted_poll_does_not_restart_an_orphaned_run(
    client, make_board, make_agent, make_task,
):
    """An acked run without a live signal is restarted by the poll with a
    fresh attempt id. Restarting is handing out work: while HALTED the
    poll keeps reporting ``working`` and changes nothing on the card."""
    from app.models.activity import ActivityEvent

    stale = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=900)
    board, agent, token = await _seed_agent(
        make_board, make_agent, last_task_activity_at=stale,
    )
    card = await make_task(
        board_id=board.id, title="orphaned run", status="in_progress",
        assigned_agent_id=agent.id, ack_at=stale, dispatched_at=stale,
        dispatch_attempt_id="attempt-before-halt",
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    assert body["state"] == "working", body
    assert body["task_id"] == str(card.id)
    held = await _card(card.id)
    assert held.ack_at is not None, "the halt must not clear the ACK of a running card"
    assert held.dispatch_attempt_id == "attempt-before-halt"
    async with _session() as s:
        events = (await s.exec(
            select(ActivityEvent).where(
                ActivityEvent.event_type == "task.orphaned_run_redispatched",
                ActivityEvent.task_id == card.id,
            )
        )).all()
    assert events == [], "no orphan redispatch may be recorded while HALTED"

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task", body
    assert body.get("orphaned_run_redispatched") is True, body


@pytest.mark.asyncio
async def test_halted_poll_still_reports_running_work(
    client, make_board, make_agent, make_task,
):
    """HALTED holds new work; it does not interrupt running work (that is
    the stop button's job). A live acked run keeps reporting ``working``."""
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    board, agent, token = await _seed_agent(make_board, make_agent, last_task_activity_at=now)
    card = await make_task(
        board_id=board.id, title="running", status="in_progress",
        assigned_agent_id=agent.id, ack_at=now, dispatched_at=now,
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    assert body["state"] == "working" and body["task_id"] == str(card.id), body


# ── DRAINING ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_draining_poll_holds_a_root_card(client, make_board, make_agent, make_task):
    from app.services.operations import SYSTEM_DRAINING_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="new root work", status="inbox", assigned_agent_id=agent.id,
    )

    await _set_mode("draining")
    body = await _poll(client, token)
    _assert_held(body, "system_draining", "system_halted", SYSTEM_DRAINING_REASON)
    assert (await _card(card.id)).dispatched_at is None


@pytest.mark.asyncio
async def test_draining_poll_still_hands_out_a_continuation_flow(
    client, make_board, make_agent, make_task,
):
    """DRAINING lets running work finish: a subtask of running work still
    goes out, exactly as push dispatch lets it through."""
    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="subtask of running work", status="inbox",
        assigned_agent_id=agent.id, dispatch_intent="subtask",
    )

    await _set_mode("draining")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body


# ── NORMAL and the unchanged neighbours ─────────────────────────────────────


@pytest.mark.asyncio
async def test_normal_mode_poll_hands_out_the_card(client, make_board, make_agent, make_task):
    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="normal", status="inbox", assigned_agent_id=agent.id,
    )

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(card.id)
    assert "system_halted" not in body and "system_draining" not in body
    assert (await _card(card.id)).dispatched_at is not None


@pytest.mark.asyncio
async def test_paused_agent_still_takes_its_assigned_card_in_normal_mode(
    client, make_board, make_agent, make_task,
):
    """ADR-089 §4 Nachtrag (PR #911, R-paused-poll-takes-assigned): paused
    is an agent rule and stays out of the system-mode gate. A paused agent
    takes a card assigned to it by name in normal mode — but not while the
    whole system is HALTED."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent, operational_mode="paused")
    card = await make_task(
        board_id=board.id, title="assigned by name", status="inbox", assigned_agent_id=agent.id,
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body
    assert (await _agent(agent.id)).operational_mode == "paused"


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


@pytest.mark.asyncio
async def test_update_drain_keeps_its_answer_and_halted_comes_first(
    client, make_board, make_agent, make_task,
):
    """The MC update drain keeps its own answer (ADR-094 decision 7) in
    normal mode and under DRAINING. HALTED comes first, as in push
    dispatch (check_dispatch_allowed step 1 before step 1.5)."""
    from app.services import update_drain
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="held by the update", status="inbox", assigned_agent_id=agent.id,
    )
    await _set_update_drain()

    for mode in ("active", "draining"):
        await _set_mode(mode)
        body = await _poll(client, token)
        assert body["state"] == "idle", body
        assert body.get("update_drain") is True, body
        assert body.get("detail") == update_drain.REASON, body
        assert "system_halted" not in body and "system_draining" not in body, body
        assert (await _card(card.id)).dispatched_at is None

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    assert "update_drain" not in body, body
    assert (await _card(card.id)).dispatched_at is None


@pytest.mark.asyncio
async def test_halted_answers_before_the_runtime_readiness_gate(
    client, make_board, make_agent, make_task,
):
    """HALTED with the agent's model down: the operator's own switch is the
    reason. The poll answers ``system_halted`` and does not ask the
    readiness gate at all, which would write a 'waiting, the runtime does
    not answer' note on the card's timeline and name the model instead."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    await make_task(
        board_id=board.id, title="model down too", status="inbox", assigned_agent_id=agent.id,
    )
    readiness = AsyncMock(return_value=(False, "model down"))

    await _set_mode("halted")
    with patch("app.services.runtime_readiness.runtime_ready_for_agent", readiness):
        body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    assert "runtime_not_ready" not in body, body
    assert readiness.await_count == 0, "the readiness gate ran although the system is HALTED"


# Which gate answered: push dispatch's reason → the poll's flag.
def _push_gate(reason: str) -> str:
    from app.services.operations import (
        SYSTEM_DRAINING_REASON,
        SYSTEM_HALTED_REASON,
        UPDATE_DRAIN_REASON,
    )

    return {
        SYSTEM_HALTED_REASON: "system_halted",
        UPDATE_DRAIN_REASON: "update_drain",
        SYSTEM_DRAINING_REASON: "system_draining",
    }.get(reason, "runtime_not_ready")


_POLL_GATES = ("system_halted", "update_drain", "system_draining", "runtime_not_ready")


@pytest.mark.asyncio
@pytest.mark.parametrize("model_up", [True, False], ids=["model-up", "model-down"])
@pytest.mark.parametrize("drain", [False, True], ids=["no-drain", "update-drain"])
@pytest.mark.parametrize("intent", ["root", "subtask"])
@pytest.mark.parametrize("mode", ["active", "draining", "halted"])
async def test_poll_holds_a_fresh_card_at_push_dispatchs_gate_in_its_order(
    client, make_board, make_agent, make_task, mode, intent, drain, model_up,
):
    """The fresh-card path asks the gates push dispatch asks, in the same
    order (check_dispatch_allowed: HALTED, update drain, runtime readiness,
    DRAINING): for every combination the poll holds the card exactly when
    push would, and answers with the same gate's flag."""
    from app.services.operations import check_dispatch_allowed

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title=f"{mode}/{intent}", status="inbox",
        assigned_agent_id=agent.id, dispatch_intent=intent,
    )
    if drain:
        await _set_update_drain()
    await _set_mode(mode)
    readiness = AsyncMock(return_value=(True, None) if model_up else (False, "model down"))

    with patch("app.services.runtime_readiness.runtime_ready_for_agent", readiness):
        async with _session() as s:
            push_allowed, push_reason = await check_dispatch_allowed(card, agent, s)
        body = await _poll(client, token)

    assert (body["state"] == "new_task") is push_allowed, (
        f"push says allowed={push_allowed} ({push_reason!r}), poll says {body}"
    )
    if not push_allowed:
        gate = _push_gate(push_reason)
        assert body.get(gate) is True, f"push held at {gate!r}, poll answered {body}"
        assert [g for g in _POLL_GATES if g in body] == [gate], body


# ── a card already in flight ────────────────────────────────────────────────


async def _deliver(client, token: str, card_id) -> str:
    """Normal mode: the poll hands the card out. Returns its attempt id."""
    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card_id), body
    return body["task"]["dispatch_attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["halted", "draining"])
async def test_a_card_already_delivered_keeps_its_answer_while_held(
    client, make_board, make_agent, make_task, mode,
):
    """A card the poll already handed to this agent, not yet ACKed, is in
    flight: the prompt is in the session. An ``idle`` answer would make
    the bridges treat it as gone (hermes, grok and omp clear their dedup,
    grok resets its session, poll.sh drops CURRENT_TASK_ID and its task
    lock) and paste it a second time after the halt. The poll keeps
    answering the same ``new_task`` with the same attempt: nothing new goes
    out, and the bridges dedup on the attempt as on every poll before ACK."""
    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="in flight", status="inbox", assigned_agent_id=agent.id,
    )
    attempt = await _deliver(client, token, card.id)
    delivered_at = (await _card(card.id)).dispatched_at

    await _set_mode(mode)
    body = await _poll(client, token)
    assert body["state"] == "new_task", f"an in-flight card was answered {body}"
    assert body["task"]["id"] == str(card.id), body
    assert body["task"]["dispatch_attempt_id"] == attempt, body
    assert "system_halted" not in body and "system_draining" not in body, body
    row = await _card(card.id)
    assert row.dispatch_attempt_id == attempt
    assert row.dispatched_at == delivered_at

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task", body
    assert body["task"]["dispatch_attempt_id"] == attempt, "the card was handed out anew after the halt"


@pytest.mark.asyncio
async def test_a_card_push_prepared_but_never_delivered_is_held(
    client, make_board, make_agent, make_task,
):
    """``dispatched_at`` alone does not mean delivered: push dispatch and the
    undispatched-card watcher stamp it while they prepare the workspace,
    and delivery into the session is the poll's. Such a card is held."""
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="prepared by push", status="inbox", assigned_agent_id=agent.id,
        dispatched_at=datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(minutes=1),
        dispatch_attempt_id="push-attempt",
    )

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    assert (await _card(card.id)).dispatch_attempt_id == "push-attempt"

    await _set_mode("active")
    body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["dispatch_attempt_id"] == "push-attempt", body


@pytest.mark.asyncio
async def test_a_new_attempt_of_a_delivered_card_is_held(
    client, make_board, make_agent, make_task,
):
    """The delivery mark belongs to one attempt. Once the attempt changed
    (the ACK ladder's silent retry, a resume), the new attempt was never
    delivered, so the poll holds it."""
    from app.models.task import Task
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="rotated", status="inbox", assigned_agent_id=agent.id,
    )
    await _deliver(client, token, card.id)
    async with _session() as s:
        row = await s.get(Task, card.id)
        row.dispatch_attempt_id = "rotated-attempt"
        s.add(row)
        await s.commit()

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)


@pytest.mark.asyncio
async def test_a_card_delivered_to_another_agent_is_held(
    client, make_board, make_agent, make_task,
):
    """The delivery mark belongs to one agent: a card reassigned after its
    delivery was never handed to the new agent."""
    from app.models.task import Task
    from app.services.operations import SYSTEM_HALTED_REASON

    board, first, first_token = await _seed_agent(make_board, make_agent)
    raw, token_hash = generate_agent_token()
    second = await make_agent(
        board_id=board.id, name=f"Second-{uuid.uuid4().hex[:6]}",
        agent_token_hash=token_hash, scopes=["tasks:read", "tasks:write"],
        provision_status="provisioned", requires_git_workflow=False,
    )
    card = await make_task(
        board_id=board.id, title="reassigned", status="inbox", assigned_agent_id=first.id,
    )
    await _deliver(client, first_token, card.id)
    async with _session() as s:
        row = await s.get(Task, card.id)
        row.assigned_agent_id = second.id
        s.add(row)
        await s.commit()

    await _set_mode("halted")
    body = await _poll(client, raw)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)


@pytest.mark.asyncio
async def test_a_run_reset_by_a_healer_is_not_in_flight(
    client, make_board, make_agent, make_task,
):
    """The poll delivered the card, the agent ACKed and worked on it, then a
    healer reset the run for a fresh dispatch without a new attempt (Tier-3
    recovery: ``dispatched_at`` and ``ack_at`` cleared, then
    ``auto_dispatch_task``, which HALTED refuses). That card is not in
    flight: the delivery belongs to the old dispatch. HALTED holds it."""
    from app.models.task import Task
    from app.services.operations import SYSTEM_HALTED_REASON

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title="reset by recovery", status="inbox", assigned_agent_id=agent.id,
    )
    attempt = await _deliver(client, token, card.id)
    async with _session() as s:  # ACKed, worked, then Tier 3 resets the run
        row = await s.get(Task, card.id)
        row.status = "in_progress"
        row.ack_at = None
        row.dispatched_at = None
        s.add(row)
        await s.commit()

    await _set_mode("halted")
    body = await _poll(client, token)
    _assert_held(body, "system_halted", "system_draining", SYSTEM_HALTED_REASON)
    row = await _card(card.id)
    assert row.ack_at is None and row.dispatch_attempt_id == attempt


@pytest.mark.asyncio
async def test_halted_poll_does_not_restart_an_orphaned_run_it_delivered_itself(
    client, make_board, make_agent, make_task,
):
    """Restarting an orphaned run hands out a fresh attempt, so it is new
    work, even when the poll delivered the run's current attempt itself.
    The in-flight rule does not apply to it: HALTED holds the restart."""
    from app.models.task import Task

    stale = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=900)
    board, agent, token = await _seed_agent(make_board, make_agent, last_task_activity_at=stale)
    card = await make_task(
        board_id=board.id, title="delivered, acked, died", status="inbox", assigned_agent_id=agent.id,
    )
    attempt = await _deliver(client, token, card.id)
    async with _session() as s:  # the agent ACKed, then its run died
        row = await s.get(Task, card.id)
        row.status = "in_progress"
        row.ack_at = stale
        s.add(row)
        await s.commit()

    await _set_mode("halted")
    body = await _poll(client, token)
    assert body["state"] == "working", body
    assert (await _card(card.id)).dispatch_attempt_id == attempt


# ── one rule for poll and push ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["active", "draining", "halted"])
@pytest.mark.parametrize("intent", INTENTS)
async def test_poll_and_push_dispatch_decide_the_system_mode_alike(
    client, make_board, make_agent, make_task, mode, intent,
):
    """For every mode and dispatch intent, the poll hands the card out
    exactly when the push-dispatch guard would."""
    from app.services.operations import check_dispatch_allowed

    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(
        board_id=board.id, title=f"{mode}/{intent}", status="inbox",
        assigned_agent_id=agent.id, dispatch_intent=intent,
    )

    await _set_mode(mode)
    push_allowed, push_reason = await check_dispatch_allowed(card, agent)
    body = await _poll(client, token)

    assert (body["state"] == "new_task") is push_allowed, (
        f"mode={mode} intent={intent}: push says allowed={push_allowed} "
        f"({push_reason!r}), poll says {body}"
    )
    if not push_allowed:
        assert body.get("detail") == push_reason, body


# ── pool hygiene ────────────────────────────────────────────────────────────


class _PoolProbe:
    """How many pool connections are checked out right now."""

    def __init__(self, engine):
        self.held = 0
        event.listen(engine.sync_engine, "checkout", self._out)
        event.listen(engine.sync_engine, "checkin", self._in)

    def _out(self, *a, **kw):
        self.held += 1

    def _in(self, *a, **kw):
        self.held -= 1

    def detach(self, engine):
        event.remove(engine.sync_engine, "checkout", self._out)
        event.remove(engine.sync_engine, "checkin", self._in)


def _recording_get_redis(probe, seen: list):
    """``get_redis`` for operations.py that records the checked-out pool
    connections at every read of the system-mode key."""
    from app.redis_client import RedisKeys, get_redis as real_get_redis

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if name != "get":
                return attr

            async def _get(key, *a, **kw):
                if key == RedisKeys.system_mode():
                    seen.append(probe.held)
                return await attr(key, *a, **kw)

            return _get

    async def _get_redis():
        return _Recorder(await real_get_redis())

    return _get_redis


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["claim", "approval", "orphan"])
async def test_system_mode_read_holds_no_pool_connection(
    client, make_board, make_agent, make_task, path,
):
    """Every Redis read of the mode in the poll (before a fresh claim,
    before a phase approval or an undelivered active card, which the poll
    has just re-read from the database, and before restarting an orphaned
    run) happens after the read transaction was released."""
    stale = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(seconds=900)
    if path == "claim":
        board, agent, token = await _seed_agent(make_board, make_agent)
        await make_task(board_id=board.id, title="claim", status="inbox", assigned_agent_id=agent.id)
    elif path == "approval":
        board, agent, token = await _seed_agent(make_board, make_agent, is_board_lead=True)
        await make_task(
            board_id=board.id, title="Phase Approval: p", status="inbox",
            assigned_agent_id=agent.id, delegation_type="phase_approval",
        )
    else:
        board, agent, token = await _seed_agent(make_board, make_agent, last_task_activity_at=stale)
        await make_task(
            board_id=board.id, title="orphan", status="in_progress",
            assigned_agent_id=agent.id, ack_at=stale, dispatched_at=stale,
        )
    await _set_mode("halted")

    probe = _PoolProbe(test_engine)
    seen: list[int] = []
    try:
        with patch("app.services.operations.get_redis", _recording_get_redis(probe, seen)):
            body = await _poll(client, token)
    finally:
        probe.detach(test_engine)

    assert body["state"] in ("idle", "working"), body
    assert seen, "the poll never read the system mode"
    assert all(held == 0 for held in seen), (
        f"a pool connection was still checked out at the system-mode read: {seen}"
    )


def _recording_delivery_redis(probe, seen: list):
    """``get_redis`` for routers/agents.py that records the checked-out
    pool connections at every read and write of a delivery mark."""
    from app.redis_client import get_redis as real_get_redis

    class _Recorder:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if name not in ("get", "set"):
                return attr

            async def _call(key, *a, **kw):
                if ":poll_delivered" in str(key):
                    seen.append((name, probe.held))
                return await attr(key, *a, **kw)

            return _call

    async def _get_redis():
        return _Recorder(await real_get_redis())

    return _get_redis


@pytest.mark.asyncio
async def test_delivery_mark_holds_no_pool_connection(
    client, make_board, make_agent, make_task,
):
    """The delivery mark is written when the poll hands a card out and read
    when a held mode meets it. Both happen with no pool connection checked
    out (incident 2026-09-14)."""
    board, agent, token = await _seed_agent(make_board, make_agent)
    await make_task(board_id=board.id, title="mark", status="inbox", assigned_agent_id=agent.id)

    probe = _PoolProbe(test_engine)
    seen: list = []
    try:
        with patch("app.routers.agents.get_redis", _recording_delivery_redis(probe, seen)):
            await _set_mode("active")
            assert (await _poll(client, token))["state"] == "new_task"
            await _set_mode("halted")
            assert (await _poll(client, token))["state"] == "new_task"
    finally:
        probe.detach(test_engine)

    assert {op for op, _ in seen} == {"get", "set"}, seen
    assert all(held == 0 for _, held in seen), (
        f"a pool connection was still checked out at a delivery-mark call: {seen}"
    )


# ── the helper itself ───────────────────────────────────────────────────────


@pytest.mark.parametrize("intent", INTENTS)
def test_system_mode_refusal_is_the_rule_of_check_dispatch_allowed(intent):
    from app.models.task import Task
    from app.services.operations import (
        SYSTEM_DRAINING_REASON,
        SYSTEM_HALTED_REASON,
        is_continuation_flow,
        system_mode_refusal,
    )

    task = Task(board_id=uuid.uuid4(), title="t", dispatch_intent=intent)
    assert system_mode_refusal("active", task) is None
    assert system_mode_refusal("halted", task) == SYSTEM_HALTED_REASON
    expected = None if is_continuation_flow(task) else SYSTEM_DRAINING_REASON
    assert system_mode_refusal("draining", task) == expected


@pytest.mark.asyncio
async def test_check_dispatch_allowed_keeps_its_order_under_draining(
    make_board, make_agent, make_task, async_session,
):
    """Taking the rule out of check_dispatch_allowed must not move DRAINING
    ahead of the gates listed before it. The update drain's reason matters
    most: only that reason makes auto_dispatch_task record the card for the
    catch-up after the update (ADR-094 decision 7)."""
    from app.services.operations import (
        UPDATE_DRAIN_REASON,
        check_dispatch_allowed,
    )

    board = await make_board(name="Order", slug=f"order-{uuid.uuid4().hex[:8]}")
    active_agent = await make_agent(name="ActiveAgent", board_id=board.id)
    paused_agent = await make_agent(name="PausedAgent", board_id=board.id, operational_mode="paused")
    root = await make_task(board_id=board.id, title="root")
    held = await make_task(board_id=board.id, title="held", run_control="manual_hold")

    with patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="draining"):
        _, reason = await check_dispatch_allowed(held, active_agent)
        assert "run_control" in reason
        _, reason = await check_dispatch_allowed(root, paused_agent)
        assert "PAUSED" in reason
        await _set_update_drain()
        _, reason = await check_dispatch_allowed(root, active_agent, async_session)
        assert reason == UPDATE_DRAIN_REASON


@pytest.mark.asyncio
async def test_redis_failure_keeps_the_poll_working(client, make_board, make_agent, make_task):
    """The mode read fails open like push dispatch (get_system_mode answers
    'active' when Redis is gone): a Redis outage must not stall the fleet."""
    board, agent, token = await _seed_agent(make_board, make_agent)
    card = await make_task(board_id=board.id, title="redis down", status="inbox", assigned_agent_id=agent.id)

    with patch("app.services.operations.get_redis", AsyncMock(side_effect=ConnectionError("down"))):
        body = await _poll(client, token)
    assert body["state"] == "new_task" and body["task"]["id"] == str(card.id), body
