"""
Zentrale Test-Fixtures fuer Mission Control Backend.

- In-Memory SQLite DB (kein PostgreSQL noetig)
- fakeredis (kein Redis-Server noetig)
- FastAPI TestClient mit Auth
- Factory-Funktionen fuer Test-Daten
"""

import os
import tempfile
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

# ── Settings patchen BEVOR app importiert wird ───────────────────────────
# database_url bleibt PostgreSQL (damit app.database.engine ohne Fehler erstellt wird).
# Der Engine wird nie benutzt — wir overriden get_session mit unserem SQLite-Engine.

import app.config

# Pin vault_path to a session-scoped temp dir so a test that forgets its
# monkeypatch can NEVER write into the real ~/.mc/vault. Discovered after the
# 2026-05-17 vault-pollution incident: the default Settings(vault_path=...)
# resolved to the prod path inside the Docker backend (HOME_HOST=$HOME), and
# during Phase 29 verification 160 fixture notes ended up in the operator's real vault.
_TEST_VAULT_ROOT = Path(tempfile.mkdtemp(prefix="mc-test-vault-"))

app.config.settings = app.config.Settings(
    database_url="postgresql+asyncpg://test:test@localhost:5432/test",
    redis_url="redis://fake",
    jwt_secret_key="test-secret-key-for-testing",
    local_auth_token="",
    openclaw_ws_url="",
    environment="test",
    intelligence_interval=99999,
    embedding_retry_interval=99999,  # Phase 5 MSY-04 Pitfall 4: never auto-fire in tests
    obsidian_export_interval=99999,  # Phase 7 OBS-02 Pitfall 4: never auto-fire in tests
    vault_lint_interval_hours=99999,  # M.3 T4 Pitfall 4 mirror: vault_lint loop must not auto-fire in tests
    ollama_url="http://localhost:99999",
    use_subagent_dispatch=False,  # Tests laufen im Legacy-Modus; neue Tests aktivieren Flag explizit
    secrets_encryption_key="bkMM-h80JH3_PRkNc6_-T0YrLMOShvZeoDkKnGrI7JM=",
    vault_path=_TEST_VAULT_ROOT,
    lifecycle_watchdog_enabled=True,  # ADR-046: on by default; the check is only ever
                                      # invoked when a test calls _check_stuck_in_progress directly.
)

# Jetzt App-Module importieren
from app.database import get_session
from app.redis_client import get_redis

# Alle Models importieren damit create_all alle Tabellen kennt
import app.models  # noqa: F401
import app.models.agent_template  # noqa: F401 — nicht in __init__.py, aber FK-Referenz von Agent
import app.models.content  # noqa: F401
import app.models.checkpoint  # noqa: F401
import app.models.deliverable  # noqa: F401
import app.models.cost_event  # noqa: F401
import app.models.secret  # noqa: F401
import app.models.credential  # noqa: F401
import app.models.deploy_history  # noqa: F401
import app.models.scheduled_job  # noqa: F401
import app.models.checklist  # noqa: F401
import app.models.agent_task_comment_cursor  # noqa: F401

# ── Test-Engine (SQLite in-memory, StaticPool = alle Connections teilen eine DB) ──

test_engine = create_async_engine(
    "sqlite+aiosqlite://",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# SQLite: Foreign Keys NICHT aktivieren.
# Begruendung: SQLAlchemy ORM ordnet INSERTs nur ueber relationship()-Definitionen,
# nicht ueber FK-Constraints. Da die Models keine relationship()s haben, wuerde
# PRAGMA foreign_keys=ON alle Tests brechen die Board+Agent+Task in einer Session
# erstellen. FK-Enforcement laeuft in Production via PostgreSQL + Alembic-Migrations.

# ── Database Fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def setup_db():
    """Vor jedem Test: Tabellen erstellen. Danach: alles droppen."""
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """DB-Session fuer Tests die direkt auf die DB zugreifen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """Alias-Fixture fuer Tests die 'async_session' statt 'session' nutzen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
async def board_with_agents(async_session: AsyncSession):
    """Fixture: Board mit Boss (board_lead) + developer agent."""
    from app.models.board import Board
    from app.models.agent import Agent
    board = Board(name="Test Board", slug="test-board")
    async_session.add(board)
    await async_session.commit()
    await async_session.refresh(board)

    boss = Agent(
        name="Boss",
        board_id=board.id,
        is_board_lead=True,
        role="orchestrator",
        emoji="👑",
    )
    developer = Agent(
        name="Dev",
        board_id=board.id,
        is_board_lead=False,
        role="developer",
        emoji="🛠",
    )
    async_session.add(boss)
    async_session.add(developer)
    await async_session.commit()
    await async_session.refresh(boss)
    await async_session.refresh(developer)

    return {"board": board, "boss": boss, "developer": developer}


# ── Redis (fakeredis) ─────────────────────────────────────────────────────

@pytest.fixture
async def fake_redis():
    """In-Memory Redis-Ersatz."""
    server = fakeredis.aioredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    yield redis
    await redis.aclose()


# ── FastAPI TestClient ────────────────────────────────────────────────────

@pytest.fixture
async def client(fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP-Client gegen die FastAPI-App (ohne Auth)."""
    from app.main import app as fastapi_app
    import app.redis_client

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    async def override_get_redis():
        return fake_redis

    fastapi_app.dependency_overrides[get_session] = override_get_session
    fastapi_app.dependency_overrides[get_redis] = override_get_redis

    # get_redis() wird auch direkt (nicht via Depends) aufgerufen.
    # Muss in jedem Modul gepatcht werden das es importiert hat.
    import app.routers.system as system_mod
    import app.services.sse as sse_mod
    original_system_get_redis = system_mod.get_redis
    original_sse_get_redis = sse_mod.get_redis
    system_mod.get_redis = override_get_redis
    sse_mod.get_redis = override_get_redis  # broadcast() ruft get_redis() direkt auf

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    fastapi_app.dependency_overrides.clear()
    system_mod.get_redis = original_system_get_redis
    sse_mod.get_redis = original_sse_get_redis


@pytest.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """Client mit gueltigem JWT-Token (Admin-User)."""
    from app.auth import create_access_token
    from app.models.user import User

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000099")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        user = User(
            id=user_id,
            email="test@mc.local",
            name="Test Admin",
            role="admin",
            is_active=True,
        )
        s.add(user)
        await s.commit()

    token = create_access_token(str(user_id), "admin")
    client.headers["Authorization"] = f"Bearer {token}"
    return client


# ── Test-Daten Factories ──────────────────────────────────────────────────

@pytest.fixture
def make_board():
    """Factory: Board erstellen."""
    async def _make(name: str = "Test Board", slug: str = "test-board", **kwargs):
        from app.models.board import Board
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            board = Board(id=uuid.uuid4(), name=name, slug=slug, **kwargs)
            s.add(board)
            await s.commit()
            await s.refresh(board)
            return board
    return _make


@pytest.fixture
def make_agent():
    """Factory: Agent erstellen.

    Phase 30: agent_runtime defaults to 'cli-bridge' (the post-sunset
    mainstream). Production migration 0123 will replace the legacy
    'openclaw' default + add a CHECK constraint forbidding it. Tests
    that need a specific runtime override it explicitly.
    """
    async def _make(name: str = "Test Agent", **kwargs):
        from app.models.agent import Agent
        kwargs.setdefault("agent_runtime", "cli-bridge")
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            agent = Agent(id=uuid.uuid4(), name=name, **kwargs)
            s.add(agent)
            await s.commit()
            await s.refresh(agent)
            return agent
    return _make


@pytest.fixture
def make_task():
    """Factory: Task erstellen."""
    async def _make(board_id: uuid.UUID, title: str = "Test Task", **kwargs):
        from app.models.task import Task
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = Task(id=uuid.uuid4(), board_id=board_id, title=title, **kwargs)
            s.add(task)
            await s.commit()
            await s.refresh(task)
            return task
    return _make
