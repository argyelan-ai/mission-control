"""Tests for the review-park grace window (Lauf 7, approved 22.09.2026).

Live incident (30-day sample): a card in status `review` parked the
ASSIGNED agent unconditionally — poll returned state="working" for any
agent holding a review card, forever. 15x the reviewer sat parked on
a review card while other work waited (~1900 min total); in 8 of those
cases the reviewer had already commented, only the approve/reject decision
was missing. Separately, 30x no reviewer was found and the review card
stayed on the DEVELOPER's own current_task_id (`handle_review_handoff`
returns None without reassigning) — the developer was then wrongly parked
too, since the old poll/Guard-1 logic didn't distinguish "the real
reviewer is sitting on their own review assignment" from "someone else's
task happens to be in status=review".

Fix: `app.services.review_park.review_still_parks(session, task, agent)`
is the single source of truth, used by BOTH:
  - poll (`GET /agent/me/poll`, app/routers/agents.py) — after the
    existing blocked-grace block, a stale/non-reviewer/commented review
    card is released (falls through to inbox-claim) instead of parking.
  - dispatch Guard 1 (`app/services/dispatch.py auto_dispatch_task`) — a
    released review card referenced by `best_agent.current_task_id` no
    longer blocks a NEW task from being queued behind it.

Rules (only meaningful when task.status == "review"), checked in order:
  0. settings.review_park_grace_enabled (default True) is a kill switch —
     off means: always True (today's unconditional-parking behavior).
  1. A live turn (agent.status == "working", heartbeat younger than
     TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS, same signal as dispatch
     Guard 3) always parks -> True, even past the grace window or after a
     comment (Nachpruefung N3: releasing mid-turn risks a bridge pasting
     the next prompt into a still-running session).
  2. Only the real reviewer is parkable: dispatch_intent == "review_handoff"
     AND assigned_agent_id == agent.id. Otherwise -> False immediately
     (developer holding their own submitted card, or unrelated intent).
  3. If the reviewer already posted a real-content comment
     (review_park.REVIEW_DONE_COMMENT_TYPES: feedback/message/reflection)
     on the card since delivery -> False (review is done, only the
     approve/reject decision is outstanding). Automatic bridge comments
     (blocker/progress/...) do NOT count (Nachpruefung N2).
  4. Otherwise -> True as long as less than
     settings.review_park_grace_minutes (default 30) have passed since
     delivery (ack_at, fallback dispatched_at, fallback updated_at;
     Nachpruefung K1).

Exception (poll only): a review card that was never delivered to the
agent yet (ack_at IS NULL) must still be delivered exactly once — the
grace/park check never applies to it, matching existing claim semantics
for review-handoffs to cli-bridge agents.
"""
import datetime as dt
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment
from tests.conftest import test_engine


def _ago(minutes: float) -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(minutes=minutes)


async def _make_board_and_agent(session: AsyncSession, *, role: str = "reviewer"):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:8]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"reviewer-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
        role=role,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return board, agent, raw_token


async def _make_review_task(
    session: AsyncSession,
    *,
    board: Board,
    agent: Agent,
    dispatched_at: dt.datetime,
    ack_at,
    dispatch_intent: str = "review_handoff",
    assigned_agent_id: uuid.UUID | None = None,
    title: str = "Review-park probe",
):
    task = Task(
        board_id=board.id,
        assigned_agent_id=assigned_agent_id if assigned_agent_id is not None else agent.id,
        title=title,
        status="review",
        dispatch_intent=dispatch_intent,
        dispatched_at=dispatched_at,
        ack_at=ack_at,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


async def _add_comment(
    session: AsyncSession, *, task: Task, agent: Agent, created_at: dt.datetime,
    comment_type: str = "message",
):
    comment = TaskComment(
        task_id=task.id,
        author_type="agent",
        author_agent_id=agent.id,
        comment_type=comment_type,
        content="LGTM modulo the approve/reject decision.",
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    # created_at has a server_default — age it explicitly via raw UPDATE,
    # same pattern as the blocked-grace tests age blocked_at/updated_at.
    await session.exec(
        update(TaskComment).where(TaskComment.id == comment.id).values(created_at=created_at)
    )
    await session.commit()
    return comment


async def _make_inbox_task(session: AsyncSession, *, board: Board, agent: Agent, title: str = "Fresh inbox work"):
    task = Task(board_id=board.id, assigned_agent_id=agent.id, title=title, status="inbox")
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── review_still_parks + poll ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fresh_review_parks_reviewer(client: AsyncClient, async_session):
    """Reviewer, 5min since delivery, no comment yet -> parks (True);
    poll keeps returning working on that same review card."""
    from app.services.review_park import review_still_parks

    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(5), ack_at=_ago(5),
    )

    assert await review_still_parks(async_session, review_task, agent) is True

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "working", body
    assert body["task_id"] == str(review_task.id)


@pytest.mark.asyncio
async def test_review_releases_after_grace(client: AsyncClient, async_session):
    """Reviewer, 31min since delivery (> default 30min grace), no comment
    -> released (False); poll no longer parks and delivers the agent's
    fresh inbox task instead."""
    from app.services.review_park import review_still_parks

    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(31), ack_at=_ago(31),
    )
    inbox_task = await _make_inbox_task(async_session, board=board, agent=agent)

    assert await review_still_parks(async_session, review_task, agent) is False

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(inbox_task.id)
    assert body["task"]["id"] != str(review_task.id)


@pytest.mark.asyncio
async def test_review_releases_once_reviewer_commented(client: AsyncClient, async_session):
    """Reviewer commented on the card after delivery (review already
    done, only the decision is outstanding) -> released (False) even
    though only 6min have passed, well inside the grace window."""
    from app.services.review_park import review_still_parks

    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(6), ack_at=_ago(6),
    )
    await _add_comment(async_session, task=review_task, agent=agent, created_at=_ago(3))

    assert await review_still_parks(async_session, review_task, agent) is False

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] != "working", body
    assert body["state"] == "idle", body


@pytest.mark.asyncio
async def test_developer_holding_review_card_is_not_parked(client: AsyncClient, async_session):
    """A card in status=review whose dispatch_intent isn't review_handoff
    (the developer holding their OWN just-submitted card, e.g. no
    reviewer was found and handle_review_handoff left it unassigned) is
    never parkable, regardless of freshness -> False; poll delivers the
    developer's other inbox work."""
    from app.services.review_park import review_still_parks

    board, agent, token = await _make_board_and_agent(async_session, role="developer")
    review_task = await _make_review_task(
        async_session, board=board, agent=agent,
        dispatched_at=_ago(5), ack_at=_ago(5),
        dispatch_intent="root",
    )
    inbox_task = await _make_inbox_task(async_session, board=board, agent=agent)

    assert await review_still_parks(async_session, review_task, agent) is False

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(inbox_task.id)


@pytest.mark.asyncio
async def test_unacked_review_card_is_still_delivered_once(client: AsyncClient, async_session):
    """A review-handoff card never yet delivered (ack_at IS NULL) is
    exempt from the park/release check entirely — poll must deliver IT
    (once), never silently skip to a different inbox task."""
    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(5), ack_at=None,
    )
    # A fresh inbox task exists too — proves the review card, not the
    # inbox task, is what gets delivered.
    await _make_inbox_task(async_session, board=board, agent=agent)

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="x"):
        resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(review_task.id)


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_parking(client: AsyncClient, async_session, monkeypatch):
    """settings.review_park_grace_enabled=False -> always parks (True),
    even 60min after delivery with no comment (today's unconditional
    behavior, unchanged)."""
    from app.config import settings
    from app.services.review_park import review_still_parks

    monkeypatch.setattr(settings, "review_park_grace_enabled", False)

    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(60), ack_at=_ago(60),
    )

    assert await review_still_parks(async_session, review_task, agent) is True

    resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "working", body
    assert body["task_id"] == str(review_task.id)


def test_setting_defaults():
    """Schalter default ON, Gnadenfrist default 30min."""
    from app.config import settings

    assert settings.review_park_grace_enabled is True
    assert settings.review_park_grace_minutes == 30


@pytest.mark.asyncio
async def test_automatic_comment_types_do_not_release(async_session):
    """Nachpruefung N2: a 'blocker' comment (poll.sh turn-state automation)
    and a 'progress' comment (bridge classification) from the reviewer are
    NOT real review content -- neither releases the park, even well past
    the two comments' timestamps and inside the grace window otherwise
    being irrelevant here since no real verdict was posted."""
    from app.services.review_park import review_still_parks

    board, agent, _ = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(6), ack_at=_ago(6),
    )
    await _add_comment(
        async_session, task=review_task, agent=agent, created_at=_ago(4),
        comment_type="blocker",
    )
    await _add_comment(
        async_session, task=review_task, agent=agent, created_at=_ago(3),
        comment_type="progress",
    )

    assert await review_still_parks(async_session, review_task, agent) is True


@pytest.mark.asyncio
async def test_feedback_comment_releases(async_session):
    """Nachpruefung N2: a 'feedback' comment IS real review content and
    releases the park, same as the existing 'message' coverage."""
    from app.services.review_park import review_still_parks

    board, agent, _ = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(6), ack_at=_ago(6),
    )
    await _add_comment(
        async_session, task=review_task, agent=agent, created_at=_ago(3),
        comment_type="feedback",
    )

    assert await review_still_parks(async_session, review_task, agent) is False


@pytest.mark.asyncio
async def test_turn_signal_parks_even_past_grace(async_session):
    """Nachpruefung N3: a reviewer mid-turn (status=working, fresh
    heartbeat) still parks even 31min past the grace window with no
    comment at all -- releasing mid-turn risks a bridge pasting the next
    prompt into the still-running session (fail-open, poll.sh
    READY_TIMEOUT_SEC=5s)."""
    from app.services.review_park import review_still_parks

    board, agent, _ = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(31), ack_at=_ago(31),
    )
    agent.status = "working"
    agent.last_seen_at = _ago(0.1)
    async_session.add(agent)
    await async_session.commit()

    assert await review_still_parks(async_session, review_task, agent) is True


@pytest.mark.asyncio
async def test_stale_heartbeat_does_not_override_release(async_session):
    """Nachpruefung N3: same setup, but the heartbeat is 200s old -- past
    TURN_SIGNAL_HEARTBEAT_MAX_AGE_SECONDS (90s), so the turn signal is
    stale and falls through to the normal grace rule (expired, no
    comment) -> released, same as without the turn signal at all."""
    from app.services.review_park import review_still_parks

    board, agent, _ = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent, dispatched_at=_ago(31), ack_at=_ago(31),
    )
    agent.status = "working"
    agent.last_seen_at = _ago(200 / 60)
    async_session.add(agent)
    await async_session.commit()

    assert await review_still_parks(async_session, review_task, agent) is False


# ── dispatch Guard 1 ────────────────────────────────────────────────────


async def _run_dispatch(task_id: uuid.UUID, board_id: uuid.UUID) -> None:
    with patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):
        from app.services.dispatch import auto_dispatch_task
        await auto_dispatch_task(task_id, board_id)


async def _seed_guard1(make_board, make_agent, make_task, *, review_age_minutes: float):
    board = await make_board(
        name=f"ReviewPark-{uuid.uuid4().hex[:8]}",
        slug=f"review-park-{uuid.uuid4().hex[:8]}",
        auto_dispatch_enabled=True,
    )
    agent = await make_agent(
        name="alpha", role="developer", board_id=board.id,
        agent_runtime="cli-bridge", requires_git_workflow=False,
    )
    review_ts = _ago(review_age_minutes)
    review_task = await make_task(
        board_id=board.id, title="Open review card",
        status="review", dispatch_intent="review_handoff",
        assigned_agent_id=agent.id,
        dispatched_at=review_ts, ack_at=review_ts,
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        await s.exec(update(Agent).where(Agent.id == agent.id).values(current_task_id=review_task.id))
        await s.commit()

    new_task = await make_task(
        board_id=board.id, title="New task to dispatch",
        status="inbox", assigned_agent_id=agent.id,
    )
    return board, agent, review_task, new_task


@pytest.mark.asyncio
async def test_guard1_does_not_queue_behind_released_review(make_board, make_agent, make_task, fake_redis):
    """best_agent.current_task_id points at a review card 31min old
    (released) -> Guard 1 must NOT queue the new task behind it."""
    from app.services.task_queue import queue_length

    board, agent, review_task, new_task = await _seed_guard1(
        make_board, make_agent, make_task, review_age_minutes=31,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        await _run_dispatch(new_task.id, board.id)
        assert await queue_length(str(agent.id)) == 0, "released review must not block dispatch"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, new_task.id)
        assert refreshed.dispatched_at is not None, "new task must dispatch, not queue"


@pytest.mark.asyncio
async def test_guard1_still_queues_behind_fresh_review(make_board, make_agent, make_task, fake_redis):
    """best_agent.current_task_id points at a review card only 5min old
    (still parking) -> Guard 1 still queues the new task behind it,
    exactly like today."""
    from app.services.task_queue import queue_length

    board, agent, review_task, new_task = await _seed_guard1(
        make_board, make_agent, make_task, review_age_minutes=5,
    )
    with patch("app.services.task_queue.get_redis", return_value=fake_redis):
        await _run_dispatch(new_task.id, board.id)
        assert await queue_length(str(agent.id)) == 1, "fresh review must still park (queue behind it)"

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        refreshed = await s.get(Task, new_task.id)
        assert refreshed.dispatched_at is None, "new task must NOT dispatch while parked"


# ── prompt hint for an open review decision ────────────────────────────


@pytest.mark.asyncio
async def test_new_task_prompt_mentions_open_review_decision(client: AsyncClient, async_session):
    """When poll delivers a NEW task to an agent who still holds a
    released-but-undecided review card (review_handoff, status=review, no
    comment/decision yet), the delivered prompt must contain a reminder
    line so the agent doesn't silently forget to record the decision with
    `mc approve <id>` / `mc reject <id> --feedback ...` (NOT `mc review`,
    which submits the agent's OWN card into review — Nachpruefung N1)."""
    board, agent, token = await _make_board_and_agent(async_session)
    review_task = await _make_review_task(
        async_session, board=board, agent=agent,
        dispatched_at=_ago(40), ack_at=_ago(40),
        title="Ancient undecided review",
    )
    inbox_task = await _make_inbox_task(async_session, board=board, agent=agent)

    with patch("app.services.dispatch.build_agent_task_prompt", return_value="BASE PROMPT"):
        resp = await client.get("/api/v1/agent/me/poll", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "new_task", body
    assert body["task"]["id"] == str(inbox_task.id)

    prompt = body["task"]["prompt"]
    assert "Open review decision" in prompt, prompt
    assert review_task.title in prompt, prompt
    assert str(review_task.id) in prompt, prompt
    assert f"mc approve {review_task.id}" in prompt, prompt
    assert f"mc reject {review_task.id} --feedback" in prompt, prompt
