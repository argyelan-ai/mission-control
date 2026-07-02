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
