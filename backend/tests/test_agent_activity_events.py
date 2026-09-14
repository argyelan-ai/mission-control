"""Tests for the agent-side activity_events READ API.

GET /api/v1/agent/me/activity-events and its /summary counterpart give an
agent a scoped, capped, paginated read path over ActivityEvent — the same
table the operator-only task timeline (tasks.py get_task_timeline) already
reads per-task. Board Task bfba0507 (2026-09-14): before this, "how often
did event X happen in the last 7 days, and what followed?" was unanswerable
by an agent without a human pulling it from psql on the host.

Fixtures mirror test_agent_thread_read.py (agent-token auth via raw
Authorization header, not the `auth_client` user-auth fixture).
"""
import datetime as dt
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.activity import ActivityEvent
from app.models.agent import Agent
from app.models.board import Board
from tests.conftest import test_engine


async def _board_with_agent(async_session: AsyncSession, *, board_id: uuid.UUID | None = None):
    board = Board(id=board_id or uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Agent-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=board.id,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    return board, agent, raw_token


async def _add_event(
    async_session: AsyncSession,
    *,
    board_id: uuid.UUID | None,
    event_type: str = "task.blocked",
    created_at: dt.datetime | None = None,
    agent_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    title: str = "evt",
    detail: dict | None = None,
) -> ActivityEvent:
    ev = ActivityEvent(
        board_id=board_id,
        event_type=event_type,
        title=title,
        agent_id=agent_id,
        task_id=task_id,
        detail=detail,
        created_at=created_at or dt.datetime.now(tz=dt.timezone.utc),
    )
    async_session.add(ev)
    await async_session.commit()
    await async_session.refresh(ev)
    return ev


async def _list(client: AsyncClient, token: str, **params):
    return await client.get(
        "/api/v1/agent/me/activity-events",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


async def _summary(client: AsyncClient, token: str, **params):
    return await client.get(
        "/api/v1/agent/me/activity-events/summary",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


# ── Auth ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/agent/me/activity-events")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_summary_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/agent/me/activity-events/summary")
    assert resp.status_code == 401


# ── Scope — the tragende Bedingung ───────────────────────────────────────

@pytest.mark.asyncio
async def test_list_never_returns_another_boards_events(client: AsyncClient, async_session):
    """Sabotage probe: drop `ActivityEvent.board_id == agent.board_id` from
    the query in agent_scoped.py's agent_list_activity_events -> this test
    goes red (board_b's event leaks into agent_a's result)."""
    board_a, agent_a, token_a = await _board_with_agent(async_session)
    board_b, agent_b, token_b = await _board_with_agent(async_session)

    await _add_event(async_session, board_id=board_a.id, event_type="task.blocked", title="A-event")
    await _add_event(async_session, board_id=board_b.id, event_type="task.blocked", title="B-event")

    resp = await _list(client, token_a, since=_iso_days_ago(1))
    assert resp.status_code == 200
    titles = {e["title"] for e in resp.json()["events"]}
    assert titles == {"A-event"}


@pytest.mark.asyncio
async def test_list_rejects_explicit_foreign_board_id(client: AsyncClient, async_session):
    """Sabotage probe: drop the `board_id != agent.board_id -> 403` guard ->
    this test goes red (request for a foreign board_id silently succeeds
    instead of being rejected)."""
    board_a, agent_a, token_a = await _board_with_agent(async_session)
    board_b, _agent_b, _token_b = await _board_with_agent(async_session)

    resp = await _list(client, token_a, board_id=str(board_b.id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_accepts_own_board_id_explicitly(client: AsyncClient, async_session):
    board_a, agent_a, token_a = await _board_with_agent(async_session)
    await _add_event(async_session, board_id=board_a.id, title="own")

    resp = await _list(client, token_a, board_id=str(board_a.id), since=_iso_days_ago(1))
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 1


@pytest.mark.asyncio
async def test_summary_never_returns_another_boards_counts(client: AsyncClient, async_session):
    """Sabotage probe: drop the board scope filter in
    agent_activity_events_summary -> this test goes red."""
    board_a, agent_a, token_a = await _board_with_agent(async_session)
    board_b, _agent_b, _token_b = await _board_with_agent(async_session)

    await _add_event(async_session, board_id=board_a.id, event_type="task.blocked")
    await _add_event(async_session, board_id=board_b.id, event_type="task.blocked")
    await _add_event(async_session, board_id=board_b.id, event_type="task.blocked")

    resp = await _summary(client, token_a, since=_iso_days_ago(1))
    assert resp.status_code == 200
    total = sum(b["count"] for b in resp.json()["buckets"])
    assert total == 1


@pytest.mark.asyncio
async def test_summary_rejects_explicit_foreign_board_id(client: AsyncClient, async_session):
    """W1 (PR #588 review): mirrors test_list_rejects_explicit_foreign_board_id
    for /summary. Sabotage probe: drop the `board_id != agent.board_id ->
    403` guard on the summary endpoint -> this test goes red (a request for
    a foreign board_id silently returns that agent's OWN counts instead of
    being rejected — the exact 'silent narrowing' the list docstring rules
    out, but which the summary endpoint had no test pinning)."""
    board_a, agent_a, token_a = await _board_with_agent(async_session)
    board_b, _agent_b, _token_b = await _board_with_agent(async_session)

    resp = await _summary(client, token_a, board_id=str(board_b.id))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_agent_without_board_sees_only_board_none_events(client: AsyncClient, async_session):
    """N2 (PR #588 review): pins the documented no-board case
    (agent_scoped.py docstring: 'An agent with no board only ever sees
    events with board_id IS NULL, never another board's'). Correctly built
    already (`ActivityEvent.board_id == None` renders as IS NULL in
    SQLAlchemy) but previously unpinned."""
    raw_token, token_hash = generate_agent_token()
    agent = Agent(
        name=f"Agent-{uuid.uuid4().hex[:6]}",
        agent_runtime="cli-bridge",
        agent_token_hash=token_hash,
        board_id=None,
    )
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)

    board, _agent2, _token2 = await _board_with_agent(async_session)
    await _add_event(async_session, board_id=None, title="global")
    await _add_event(async_session, board_id=board.id, title="scoped")

    resp = await _list(client, raw_token, since=_iso_days_ago(1))
    assert resp.status_code == 200
    titles = {e["title"] for e in resp.json()["events"]}
    assert titles == {"global"}


# ── Filters ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_filters_by_event_type(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    await _add_event(async_session, board_id=board.id, event_type="task.blocked", title="blocked-1")
    await _add_event(async_session, board_id=board.id, event_type="task.stuck", title="stuck-1")

    resp = await _list(client, token, event_type="task.blocked", since=_iso_days_ago(1))
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["title"] == "blocked-1"


@pytest.mark.asyncio
async def test_list_filters_by_comma_separated_event_types(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    await _add_event(async_session, board_id=board.id, event_type="task.blocked", title="blocked-1")
    await _add_event(async_session, board_id=board.id, event_type="task.stuck", title="stuck-1")
    await _add_event(async_session, board_id=board.id, event_type="task.promoted", title="promoted-1")

    resp = await _list(client, token, event_type="task.blocked,task.stuck", since=_iso_days_ago(1))
    assert resp.status_code == 200
    titles = {e["title"] for e in resp.json()["events"]}
    assert titles == {"blocked-1", "stuck-1"}


@pytest.mark.asyncio
async def test_list_filters_by_agent_id_and_task_id(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    other_agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    other_task_id = uuid.uuid4()

    await _add_event(async_session, board_id=board.id, agent_id=agent.id, task_id=task_id, title="mine")
    await _add_event(async_session, board_id=board.id, agent_id=other_agent_id, task_id=task_id, title="other-agent")
    await _add_event(async_session, board_id=board.id, agent_id=agent.id, task_id=other_task_id, title="other-task")

    resp = await _list(client, token, agent_id=str(agent.id), task_id=str(task_id), since=_iso_days_ago(1))
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert len(events) == 1
    assert events[0]["title"] == "mine"


@pytest.mark.asyncio
async def test_summary_filters_by_agent_id_and_task_id(client: AsyncClient, async_session):
    """W2 (PR #588 review): mirrors test_list_filters_by_agent_id_and_task_id
    for /summary — both filters were previously ungepinnt (agent_id sabotage
    lets the 'other-agent' event leak into the count; task_id sabotage lets
    'other-task' leak in), so a break would silently return the whole
    board's numbers in a template a human reads."""
    board, agent, token = await _board_with_agent(async_session)
    other_agent_id = uuid.uuid4()
    task_id = uuid.uuid4()
    other_task_id = uuid.uuid4()

    await _add_event(async_session, board_id=board.id, agent_id=agent.id, task_id=task_id, event_type="task.blocked")
    await _add_event(async_session, board_id=board.id, agent_id=other_agent_id, task_id=task_id, event_type="task.blocked")
    await _add_event(async_session, board_id=board.id, agent_id=agent.id, task_id=other_task_id, event_type="task.blocked")

    resp = await _summary(client, token, agent_id=str(agent.id), task_id=str(task_id), since=_iso_days_ago(1))
    assert resp.status_code == 200
    body = resp.json()
    assert sum(b["count"] for b in body["buckets"]) == 1


# ── Window default + clamp (the "Deckel" for time span) ──────────────────

@pytest.mark.asyncio
async def test_list_default_window_excludes_events_older_than_7_days(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    await _add_event(
        async_session, board_id=board.id, title="old",
        created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=10),
    )
    await _add_event(
        async_session, board_id=board.id, title="recent",
        created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(hours=1),
    )

    resp = await _list(client, token)
    assert resp.status_code == 200
    titles = {e["title"] for e in resp.json()["events"]}
    assert titles == {"recent"}


@pytest.mark.asyncio
async def test_list_window_clamps_to_max_span_and_flags_it(client: AsyncClient, async_session):
    """Sabotage probe: remove the `end - start > max_span` clamp in
    `_activity_events_window` -> this test goes red (clamped stays False,
    and the 200-day-old event that must stay excluded shows up)."""
    board, agent, token = await _board_with_agent(async_session)
    await _add_event(
        async_session, board_id=board.id, title="ancient",
        created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=200),
    )
    await _add_event(
        async_session, board_id=board.id, title="within-90d",
        created_at=dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=30),
    )

    resp = await _list(client, token, since=_iso_days_ago(365))
    assert resp.status_code == 200
    body = resp.json()
    assert body["window"]["clamped"] is True
    titles = {e["title"] for e in body["events"]}
    assert titles == {"within-90d"}


# ── Page size cap + pagination ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_rejects_limit_above_the_cap(client: AsyncClient, async_session):
    """Sabotage probe: drop `le=ACTIVITY_EVENTS_MAX_LIMIT` from the `limit`
    Query() declaration -> this test goes red (a limit of 99999 is accepted
    instead of rejected with 422)."""
    board, agent, token = await _board_with_agent(async_session)

    resp = await _list(client, token, limit=99999)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_paginates_via_cursor_without_gaps_or_duplicates(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)
    expected_titles = []
    for i in range(5):
        title = f"evt-{i}"
        expected_titles.append(title)
        await _add_event(
            async_session, board_id=board.id, title=title,
            created_at=now - dt.timedelta(minutes=i),
        )

    seen = []
    cursor = None
    for _ in range(10):  # generous upper bound on page count
        params = {"since": _iso_days_ago(1), "limit": 2}
        if cursor:
            params["before"] = cursor
        resp = await _list(client, token, **params)
        assert resp.status_code == 200
        body = resp.json()
        seen.extend(e["title"] for e in body["events"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert cursor is not None

    assert seen == expected_titles  # newest-first, no gaps, no dupes


@pytest.mark.asyncio
async def test_list_rejects_malformed_cursor(client: AsyncClient, async_session):
    board, agent, token = await _board_with_agent(async_session)
    resp = await _list(client, token, before="not-a-valid-cursor")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_rejects_until_before_since(client: AsyncClient, async_session):
    """N1 (PR #588 review): `until < since` used to silently return an empty
    result instead of signalling the caller's mistake with a 422."""
    board, agent, token = await _board_with_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)

    resp = await _list(
        client, token,
        since=now.isoformat(),
        until=(now - dt.timedelta(days=1)).isoformat(),
    )
    assert resp.status_code == 422


# ── The DoD question, answered against seeded test data ──────────────────

def _seed_summary_window_fixture(now: dt.datetime) -> dt.datetime:
    """Returns the fixed day-anchor used to seed events for the summary
    window test below (factored out so the postgres variant seeds
    identically)."""
    return now.replace(hour=12, minute=0, second=0, microsecond=0)


@pytest.mark.asyncio
async def test_summary_answers_how_often_event_happened_in_the_window(client: AsyncClient, async_session):
    """Directly exercises the question from the task's Kontext section: 'wie
    oft ist Ereignis X in den letzten 7 Tagen eingetreten' — seed a known
    count of task.blocked spread over 3 distinct days inside the 7-day
    window (plus noise: another event_type, and one blocked event outside
    the window), then verify the summary's bucket counts sum to exactly the
    seeded in-window count.

    F1 (PR #588 review): events used to be spread within a day via `hours=i`
    off the real `now`, and the window used the endpoint's own 7-day
    default. Before 02:00 UTC, subtracting hours pushes some events across
    the UTC calendar-day boundary, inflating `len(buckets)` to 4 or 5 in
    ~8% of CI runs. Fix: anchor each day at a fixed hour (noon) so the
    day-boundary is always >= 12h away from any offset used, spread within
    a day via `minutes=i` (max 2 minutes, nowhere near the boundary either
    way), and pass `since` explicitly so the window itself doesn't depend
    on the real wall-clock `now` at all.
    """
    board, agent, token = await _board_with_agent(async_session)
    base = _seed_summary_window_fixture(dt.datetime.now(tz=dt.timezone.utc))

    # 3 task.blocked on day -1, 2 on day -3, 1 on day -6 == 6 in-window
    for offset_days, count in [(1, 3), (3, 2), (6, 1)]:
        for i in range(count):
            await _add_event(
                async_session, board_id=board.id, event_type="task.blocked",
                created_at=base - dt.timedelta(days=offset_days, minutes=i),
            )

    # Noise: different event_type inside the window, and a blocked event outside it
    await _add_event(async_session, board_id=board.id, event_type="task.stuck", created_at=base - dt.timedelta(days=2))
    await _add_event(
        async_session, board_id=board.id, event_type="task.blocked",
        created_at=base - dt.timedelta(days=9),
    )

    resp = await _summary(
        client, token, event_type="task.blocked",
        since=(base - dt.timedelta(days=7)).isoformat(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(b["event_type"] == "task.blocked" for b in body["buckets"])
    assert sum(b["count"] for b in body["buckets"]) == 6
    assert len(body["buckets"]) == 3  # 3 distinct days


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_summary_answers_how_often_event_happened_in_the_window_postgres(client: AsyncClient, async_session):
    """F2 (PR #588 review): the SQLite-lane copy above never exercises
    `func.date(...)` against real Postgres — the exact portability line the
    PR chose over `date_trunc`. Same seed logic, same assertion, run on the
    Postgres lane (`pytest -m postgres`, see ci.yml:91 / conftest.py:246)."""
    board, agent, token = await _board_with_agent(async_session)
    base = _seed_summary_window_fixture(dt.datetime.now(tz=dt.timezone.utc))

    for offset_days, count in [(1, 3), (3, 2), (6, 1)]:
        for i in range(count):
            await _add_event(
                async_session, board_id=board.id, event_type="task.blocked",
                created_at=base - dt.timedelta(days=offset_days, minutes=i),
            )

    await _add_event(async_session, board_id=board.id, event_type="task.stuck", created_at=base - dt.timedelta(days=2))
    await _add_event(
        async_session, board_id=board.id, event_type="task.blocked",
        created_at=base - dt.timedelta(days=9),
    )

    resp = await _summary(
        client, token, event_type="task.blocked",
        since=(base - dt.timedelta(days=7)).isoformat(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(b["event_type"] == "task.blocked" for b in body["buckets"])
    assert sum(b["count"] for b in body["buckets"]) == 6
    assert len(body["buckets"]) == 3  # 3 distinct days


@pytest.mark.asyncio
async def test_summary_bucket_count_is_capped(client: AsyncClient, async_session, monkeypatch):
    """Sabotage probe: remove the `rows[:CAP]` truncation slicing in
    agent_activity_events_summary -> this test goes red (all 5 buckets come
    back instead of the lowered cap of 3, and `truncated` stays False).

    W3 (PR #588 review): this test does NOT independently pin the SQL-side
    `.limit(ACTIVITY_EVENTS_SUMMARY_CAP + 1)` call — with only 5 seeded rows,
    any limit value above the cap (verified up to `CAP + 1000`) produces the
    identical observable response, since ORDER BY already fixes the row
    order and the Python slice re-truncates to the same first `CAP` rows
    regardless of how many rows the DB returned. The DB-side limit is
    defense-in-depth for query cost on a large table, not something visible
    at this response's own shape; there is no cap-crossing HTTP behavior
    left to assert once the Python slice is in place."""
    from app.routers import agent_scoped

    monkeypatch.setattr(agent_scoped, "ACTIVITY_EVENTS_SUMMARY_CAP", 3)

    board, agent, token = await _board_with_agent(async_session)
    now = dt.datetime.now(tz=dt.timezone.utc)
    # 5 distinct (event_type, day) buckets
    for i in range(5):
        await _add_event(
            async_session, board_id=board.id, event_type=f"task.kind{i}",
            created_at=now - dt.timedelta(days=i),
        )

    resp = await _summary(client, token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert len(body["buckets"]) == 3


def _iso_days_ago(days: int) -> str:
    return (dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
