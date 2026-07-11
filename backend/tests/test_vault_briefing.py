"""Tests for GET /api/v1/agent/vault/briefing (M.4 T3).

Pre-session briefing endpoint. Voice worker calls this before xAI session starts
so Grok arrives with a structured snapshot of: open tasks, open approvals,
recent vault lessons, recent vault writes, agents online/offline, current
time-of-day (German label).

Auth pattern mirrors test_vault_routes_agent_search.py — real agent + real
PBKDF2 token + minimal FastAPI app with the agent_router mounted.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── App factory ───────────────────────────────────────────────────────────────


def _make_briefing_app(vault_index=None, vault_activity=None) -> FastAPI:
    from app.database import get_session
    from app.routers.vault import agent_router

    fa = FastAPI()
    fa.include_router(agent_router)
    fa.state.vault_index = vault_index
    fa.state.vault_activity = vault_activity

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    fa.dependency_overrides[get_session] = override_get_session
    return fa


async def _make_agent(name: str, scopes: list[str]) -> str:
    """Create an agent with the given scopes and return the raw PBKDF2 token."""
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        agent = Agent(
            id=uuid.uuid4(),
            name=name,
            role="developer",
            agent_token_hash=token_hash,
            scopes=scopes,
        )
        s.add(agent)
        await s.commit()
    return raw_token


# ── Unit tests for the helper ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "utc_hour,expected",
    [
        # _TZ_OFFSET_HOURS = 1 → local = utc + 1
        (4, "morgens"),     # 05 local
        (9, "morgens"),     # 10 local
        (10, "morgens"),    # 11 local → mittags? Wait — 11 utc + 1 = 12 → mittags
        (11, "mittags"),    # 12 local
        (12, "nachmittags"), # 13 local
        (15, "nachmittags"), # 16 local
        (16, "abends"),     # 17 local
        (20, "abends"),     # 21 local
        (21, "nachts"),     # 22 local
        (23, "nachts"),     # 24/0 local
        (3, "nachts"),      # 04 local
    ],
)
def test_time_of_day_de_buckets(utc_hour, expected):
    from app.routers.vault import _time_of_day_de

    dt = datetime(2026, 5, 15, utc_hour, 0, 0, tzinfo=timezone.utc)
    # 10 utc + 1 = 11 local → still morgens per our definition (5..10 morgens,
    # 11..12 mittags). Adjust expectation when utc_hour=10.
    # Helper logic: local 11..12 = mittags. So utc 10 → local 11 → mittags.
    if utc_hour == 10:
        assert _time_of_day_de(dt) == "mittags"
    else:
        assert _time_of_day_de(dt) == expected


def test_time_of_day_de_naive_datetime_treated_as_utc():
    """A naive datetime is treated as UTC (no crash)."""
    from app.routers.vault import _time_of_day_de

    dt = datetime(2026, 5, 15, 8, 0, 0)  # naive — 8 utc + 1 = 9 local → morgens
    assert _time_of_day_de(dt) == "morgens"


def test_extract_date_from_id_well_formed():
    from app.routers.vault import _extract_date_from_id

    assert _extract_date_from_id("sparky-20260514T123000") == "2026-05-14"


def test_extract_date_from_id_malformed_returns_none():
    from app.routers.vault import _extract_date_from_id

    assert _extract_date_from_id(None) is None
    assert _extract_date_from_id("no-dash-id") is None
    assert _extract_date_from_id("sparky-notadate") is None
    assert _extract_date_from_id("") is None


def test_extract_date_from_id_rejects_uuid_fragment():
    """Root-cause regression: a UUID's trailing hex segment can accidentally be
    all-digits (12 chars, no 'T' separator) and must NOT be misread as a date.

    Real incident: id 'agent-<uuid>' with trailing segment '97045217xxxx' (12
    hex chars, all digits by chance) was parsed into date '9704-52-17'.
    """
    from app.routers.vault import _extract_date_from_id

    # 12-char all-digit UUID trailing segment — wrong length, no 'T'.
    assert _extract_date_from_id("boss-a1b2c3d4-e5f6-9704-8a1b-970452170000") is None
    # Exactly the incident shape: last dash-segment is 12 digits, no T.
    assert _extract_date_from_id("agent-970452170000") is None


def test_extract_date_from_id_rejects_implausible_month_day():
    from app.routers.vault import _extract_date_from_id

    # Well-formed shape (15 chars, T at index 8) but month=52 is impossible.
    assert _extract_date_from_id("sparky-20265217T123000") is None
    # Year out of the plausible window.
    assert _extract_date_from_id("sparky-97040517T123000") is None


def test_parse_reliable_date_accepts_plain_and_iso():
    from app.routers.vault import _parse_reliable_date

    assert _parse_reliable_date("2026-05-14") == "2026-05-14"
    assert _parse_reliable_date("2026-05-14T10:30:00Z") == "2026-05-14"


def test_parse_reliable_date_rejects_garbage():
    from app.routers.vault import _parse_reliable_date

    assert _parse_reliable_date(None) is None
    assert _parse_reliable_date("") is None
    assert _parse_reliable_date("not-a-date") is None
    assert _parse_reliable_date("9704-52-17") is None  # implausible month/day
    assert _parse_reliable_date("4466-01-01") is None  # implausible year


def test_note_date_prefers_frontmatter_date_over_id():
    from app.routers.vault import _note_date

    # Frontmatter date wins even when the id would (wrongly) parse to something else.
    note = {"date": "2026-06-01", "id": "agent-970452170000"}
    assert _note_date(note) == "2026-06-01"


def test_note_date_falls_back_to_id_when_no_frontmatter_date():
    from app.routers.vault import _note_date

    note = {"id": "sparky-20260514T123000"}
    assert _note_date(note) == "2026-05-14"


def test_note_date_returns_none_when_both_sources_unreliable():
    from app.routers.vault import _note_date

    note = {"date": "garbage", "id": "agent-970452170000"}
    assert _note_date(note) is None


def test_age_days_computes_delta():
    from app.routers.vault import _age_days

    now = datetime(2026, 5, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert _age_days("2026-05-14", now) == 6
    assert _age_days("2026-05-20", now) == 0
    assert _age_days(None, now) is None
    assert _age_days("not-a-date", now) is None


# ── Endpoint tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_briefing_returns_required_fields():
    """All top-level keys are present when sources are healthy."""
    from app.scopes import Scope

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    data = r.json()
    for key in (
        "current_iso",
        "current_time_of_day_de",
        "open_tasks",
        "open_approvals_count",
        "recent_lessons",
        "recent_writes",
        "agents_online",
        "agents_offline",
    ):
        assert key in data, f"missing key: {key}"


@pytest.mark.asyncio
async def test_briefing_requires_vault_read_scope():
    """Agent without vault:read scope receives 403."""
    from app.scopes import Scope

    raw_token = await _make_agent("Scopeless", [Scope.HEARTBEAT.value])
    app_instance = _make_briefing_app(MagicMock(list_all=MagicMock(return_value=[])), None)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_briefing_includes_open_tasks_in_correct_order():
    """Open tasks come back ordered by created_at DESC."""
    from app.models.board import Board
    from app.models.task import Task
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    # Seed: one board + 3 tasks with explicit created_at staggered, plus 1 done.
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug="b")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        # Order: t3 newest, t2 middle, t1 oldest. done_task must NOT appear.
        base = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        for i, (title, status, delta) in enumerate(
            [
                ("Oldest task", "inbox", timedelta(hours=0)),
                ("Middle task", "in_progress", timedelta(hours=1)),
                ("Newest task", "blocked", timedelta(hours=2)),
                ("Done task should not appear", "done", timedelta(hours=3)),
            ]
        ):
            task = Task(
                id=uuid.uuid4(),
                board_id=board.id,
                title=title,
                status=status,
                created_at=base + delta,
            )
            s.add(task)
        await s.commit()

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    titles = [t["title"] for t in r.json()["open_tasks"]]
    assert "Done task should not appear" not in titles
    # DESC order: newest first
    assert titles[0] == "Newest task"
    assert titles[-1] == "Oldest task"


@pytest.mark.asyncio
async def test_briefing_open_approvals_count():
    """Counts only approvals with status='pending'."""
    from app.models.approval import Approval
    from app.models.board import Board
    from app.models.agent import Agent
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug="b")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        # Use an existing agent (the Jarvis agent we just created) as approval owner —
        # avoids needing a fresh Agent insert. Query it.
        agent_stmt = await s.execute(
            __import__("sqlalchemy").select(Agent).limit(1)
        )
        agent = agent_stmt.scalar_one()

        for status in ("pending", "pending", "approved", "rejected"):
            s.add(
                Approval(
                    id=uuid.uuid4(),
                    board_id=board.id,
                    agent_id=agent.id,
                    action_type="mark_done",
                    description="x",
                    status=status,
                )
            )
        await s.commit()

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    assert r.json()["open_approvals_count"] == 2


@pytest.mark.asyncio
async def test_briefing_recent_lessons_filter_by_type():
    """list_all() entries with type='lesson' are surfaced; others are not."""
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    # Today's date stamp so the 24h cutoff doesn't filter these out.
    today = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    fake_notes = [
        {
            "path": "agents/sparky/lessons/foo.md",
            "id": f"sparky-{today}",
            "agent": "sparky",
            "type": "lesson",
            "tags": "[]",
            "project": None,
            "content": "Lesson A\nBody...",
        },
        {
            "path": "agents/cody/journals/bar.md",
            "id": f"cody-{today}",
            "agent": "cody",
            "type": "journal",
            "tags": "[]",
            "project": None,
            "content": "Journal X",
        },
        {
            "path": "agents/rex/lessons/baz.md",
            "id": f"rex-{today}",
            "agent": "rex",
            "type": "lesson",
            "tags": "[]",
            "project": None,
            "content": "Lesson B",
        },
    ]
    vault_index = MagicMock()
    vault_index.list_all.return_value = fake_notes
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    lessons = r.json()["recent_lessons"]
    assert len(lessons) == 2
    types = {l["path"] for l in lessons}
    assert "agents/sparky/lessons/foo.md" in types
    assert "agents/rex/lessons/baz.md" in types
    # No 'journal' type leaked in (briefing surfaces only 'lesson')
    assert "agents/cody/journals/bar.md" not in types


@pytest.mark.asyncio
async def test_briefing_fails_soft_when_vault_index_missing():
    """vault_index=None → returns 200 with error field, not 500."""
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    # No vault_index, no vault_activity
    app_instance = _make_briefing_app(vault_index=None, vault_activity=None)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    data = r.json()
    assert "error" in data
    assert "current_time_of_day_de" in data  # other fields still present
    assert data["recent_lessons"] == []
    assert data["recent_writes"] == []


@pytest.mark.asyncio
async def test_briefing_open_tasks_deduped_with_duplicate_count():
    """Exact-duplicate open tasks (title, status, assigned_to) collapse into one
    item with duplicate_count, instead of listing the same task 3x."""
    from app.models.board import Board
    from app.models.task import Task
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug="b")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        base = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        for i, delta in enumerate([timedelta(hours=0), timedelta(hours=1), timedelta(hours=2)]):
            s.add(
                Task(
                    id=uuid.uuid4(),
                    board_id=board.id,
                    title="Post-launch retro board",
                    status="inbox",
                    created_at=base + delta,
                )
            )
        s.add(
            Task(
                id=uuid.uuid4(),
                board_id=board.id,
                title="Unique task",
                status="inbox",
                created_at=base + timedelta(hours=3),
            )
        )
        await s.commit()

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    tasks = r.json()["open_tasks"]
    titles = [t["title"] for t in tasks]
    assert titles.count("Post-launch retro board") == 1
    dup = next(t for t in tasks if t["title"] == "Post-launch retro board")
    assert dup["duplicate_count"] == 3
    unique = next(t for t in tasks if t["title"] == "Unique task")
    assert unique["duplicate_count"] == 1
    assert "age_days" in dup and dup["age_days"] is not None


@pytest.mark.asyncio
async def test_briefing_open_tasks_dedup_scoped_to_board():
    """Same (title, status, assigned_to) on TWO different boards are real,
    distinct tasks — they must NOT collapse into one duplicate. Regression
    for a dedup key that omitted board_id."""
    from app.models.board import Board
    from app.models.task import Task
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board_a = Board(id=uuid.uuid4(), name="Board A", slug="board-a")
        board_b = Board(id=uuid.uuid4(), name="Board B", slug="board-b")
        s.add(board_a)
        s.add(board_b)
        await s.commit()
        await s.refresh(board_a)
        await s.refresh(board_b)

        base = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        for board, delta in [(board_a, timedelta(hours=0)), (board_b, timedelta(hours=1))]:
            s.add(
                Task(
                    id=uuid.uuid4(),
                    board_id=board.id,
                    title="Same title different board",
                    status="inbox",
                    created_at=base + delta,
                )
            )
        await s.commit()

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    tasks = r.json()["open_tasks"]
    matching = [t for t in tasks if t["title"] == "Same title different board"]
    assert len(matching) == 2
    assert all(t["duplicate_count"] == 1 for t in matching)


@pytest.mark.asyncio
async def test_briefing_open_tasks_status_tier_ordering():
    """in_progress/blocked sort before inbox regardless of created_at."""
    from app.models.board import Board
    from app.models.task import Task
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="B", slug="b")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        base = datetime(2026, 5, 14, 10, 0, 0, tzinfo=timezone.utc)
        # Newer inbox task, older in_progress task — in_progress must still win.
        s.add(
            Task(
                id=uuid.uuid4(), board_id=board.id, title="Newer inbox item",
                status="inbox", created_at=base + timedelta(hours=5),
            )
        )
        s.add(
            Task(
                id=uuid.uuid4(), board_id=board.id, title="Older in-progress item",
                status="in_progress", created_at=base,
            )
        )
        await s.commit()

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    titles = [t["title"] for t in r.json()["open_tasks"]]
    assert titles[0] == "Older in-progress item"
    assert titles[1] == "Newer inbox item"


@pytest.mark.asyncio
async def test_briefing_recent_writes_sorted_by_real_date_not_write_count():
    """Regression: top_n_writes ranks by write COUNT, so an old note written
    many times used to outrank a genuinely recent single write. The briefing
    must re-sort candidates by real date before truncating to 5."""
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    # "old-frequent.md" has a high write-count score (ranked first by Redis)
    # but is old; "new-rare.md" has a low score but is genuinely recent.
    fake_notes = {
        "agents/sparky/old-frequent.md": {
            "path": "agents/sparky/old-frequent.md", "id": "sparky-x",
            "agent": "sparky", "type": "note", "date": "2026-03-01",
        },
        "agents/rex/new-rare.md": {
            "path": "agents/rex/new-rare.md", "id": "rex-y",
            "agent": "rex", "type": "note", "date": "2026-05-19",
        },
    }
    vault_index = MagicMock()
    vault_index.list_all.return_value = list(fake_notes.values())
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(
        return_value=[
            {"path": "agents/sparky/old-frequent.md", "score": 40},
            {"path": "agents/rex/new-rare.md", "score": 1},
        ]
    )

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    writes = r.json()["recent_writes"]
    assert writes[0]["path"] == "agents/rex/new-rare.md"
    assert writes[0]["date"] == "2026-05-19"
    assert writes[1]["path"] == "agents/sparky/old-frequent.md"


@pytest.mark.asyncio
async def test_briefing_staleness_summary_reflects_newest_write_age():
    """staleness_summary.newest_write_age_days is computed from the real date,
    and the note goes honest ('no writes in last 7 days') when everything is old."""
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    old_date = (datetime.now(timezone.utc) - timedelta(days=55)).strftime("%Y-%m-%d")
    fake_note = {
        "path": "agents/sparky/old.md", "id": "sparky-x",
        "agent": "sparky", "type": "note", "date": old_date,
    }
    vault_index = MagicMock()
    vault_index.list_all.return_value = [fake_note]
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(
        return_value=[{"path": "agents/sparky/old.md", "score": 1}]
    )

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    summary = r.json()["staleness_summary"]
    assert summary["newest_write_age_days"] == 55
    assert summary["note"] == "no writes in last 7 days"


@pytest.mark.asyncio
async def test_briefing_staleness_summary_no_dated_items():
    """No reliably dated writes → note says so instead of implying freshness."""
    from app.scopes import Scope

    raw_token = await _make_agent("Jarvis", [Scope.VAULT_READ.value])

    vault_index = MagicMock()
    vault_index.list_all.return_value = []
    vault_activity = MagicMock()
    vault_activity.top_n_writes = AsyncMock(return_value=[])

    app_instance = _make_briefing_app(vault_index, vault_activity)
    headers = {"Authorization": f"Bearer {raw_token}"}
    transport = ASGITransport(app=app_instance)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        r = await ac.get("/api/v1/agent/vault/briefing")

    assert r.status_code == 200, r.text
    summary = r.json()["staleness_summary"]
    assert summary["newest_write_age_days"] is None
    assert summary["note"] == "no reliably dated writes found"
