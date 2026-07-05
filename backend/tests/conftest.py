"""
Central test fixtures for Mission Control Backend.

- In-memory SQLite DB (no PostgreSQL needed)
- fakeredis (no Redis server needed)
- FastAPI TestClient with auth
- Factory functions for test data
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

# ── Patch settings BEFORE the app is imported ────────────────────────────
# database_url stays PostgreSQL (so app.database.engine is created without error).
# The engine is never used — we override get_session with our SQLite engine.

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
    use_subagent_dispatch=False,  # Tests run in legacy mode; new tests enable the flag explicitly
    secrets_encryption_key="bkMM-h80JH3_PRkNc6_-T0YrLMOShvZeoDkKnGrI7JM=",
    vault_path=_TEST_VAULT_ROOT,
    lifecycle_watchdog_enabled=True,  # ADR-046: on by default; the check is only ever
                                      # invoked when a test calls _check_stuck_in_progress directly.
)

# Now import app modules
from app.database import get_session
from app.redis_client import get_redis

# Import all models so create_all knows about all tables
import app.models  # noqa: F401
import app.models.agent_template  # noqa: F401 — not in __init__.py, but FK reference from Agent
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

# ── Test engine (SQLite in-memory, StaticPool = all connections share one DB) ──

test_engine = create_async_engine(
    "sqlite+aiosqlite://",
    echo=False,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# SQLite: do NOT enable foreign keys.
# Reason: SQLAlchemy ORM only orders INSERTs via relationship() definitions,
# not via FK constraints. Since the models have no relationship()s,
# PRAGMA foreign_keys=ON would break every test that creates Board+Agent+Task
# in one session. FK enforcement runs in production via PostgreSQL + Alembic migrations.

# ── Database fixtures ─────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
async def setup_db():
    """Before each test: create tables. Afterward: drop everything."""
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    """DB session for tests that access the DB directly."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
async def async_session() -> AsyncGenerator[AsyncSession, None]:
    """Alias fixture for tests that use 'async_session' instead of 'session'."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
async def board_with_agents(async_session: AsyncSession):
    """Fixture: board with Boss (board_lead) + developer agent."""
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
    """In-memory Redis replacement."""
    server = fakeredis.aioredis.FakeServer()
    redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    yield redis
    await redis.aclose()


# ── FastAPI TestClient ────────────────────────────────────────────────────

@pytest.fixture
async def client(fake_redis) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client against the FastAPI app (without auth)."""
    from app.main import app as fastapi_app
    import app.redis_client

    async def override_get_session():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    async def override_get_redis():
        return fake_redis

    fastapi_app.dependency_overrides[get_session] = override_get_session
    fastapi_app.dependency_overrides[get_redis] = override_get_redis

    # Also seed the module-level singleton so any code (including test
    # modules) that does `from app.redis_client import get_redis` and calls
    # it directly gets the fake client instead of trying a real connection.
    original_redis_singleton = app.redis_client._redis
    app.redis_client._redis = fake_redis

    # get_redis() is also called directly (not via Depends).
    # Must be patched in every module that imported it.
    import app.routers.system as system_mod
    import app.routers.agents as agents_mod
    import app.routers.runtimes as runtimes_mod
    import app.services.sse as sse_mod
    import app.services.agent_runtime_switch as switch_mod
    original_system_get_redis = system_mod.get_redis
    original_agents_get_redis = agents_mod.get_redis
    original_runtimes_get_redis = runtimes_mod.get_redis
    original_sse_get_redis = sse_mod.get_redis
    original_switch_get_redis = switch_mod.get_redis
    system_mod.get_redis = override_get_redis
    agents_mod.get_redis = override_get_redis
    runtimes_mod.get_redis = override_get_redis
    sse_mod.get_redis = override_get_redis  # broadcast() calls get_redis() directly
    switch_mod.get_redis = override_get_redis

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    fastapi_app.dependency_overrides.clear()
    system_mod.get_redis = original_system_get_redis
    agents_mod.get_redis = original_agents_get_redis
    runtimes_mod.get_redis = original_runtimes_get_redis
    sse_mod.get_redis = original_sse_get_redis
    switch_mod.get_redis = original_switch_get_redis
    app.redis_client._redis = original_redis_singleton


@pytest.fixture
async def auth_client(client: AsyncClient) -> AsyncClient:
    """Client with a valid JWT token (admin user)."""
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


# ── Test data factories ──────────────────────────────────────────────────

@pytest.fixture
def make_board():
    """Factory: create a board."""
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
    """Factory: create an agent.

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
    """Factory: create a task."""
    async def _make(board_id: uuid.UUID, title: str = "Test Task", **kwargs):
        from app.models.task import Task
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            task = Task(id=uuid.uuid4(), board_id=board_id, title=title, **kwargs)
            s.add(task)
            await s.commit()
            await s.refresh(task)
            return task
    return _make
