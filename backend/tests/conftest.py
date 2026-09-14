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

# Same incident class as the vault pollution above, pre-empted this time:
# token_harvester's Grok/Hermes sources (Bench #18 PR1) default to
# ~/.grok/... / ~/.hermes/... via HOME_HOST. On a dev machine that actually
# runs Grok/Hermes (like this one) those paths are real and non-empty — a
# test calling run_harvest() without overriding them would silently harvest
# the operator's real session history. Point both at a tmp dir that never
# has anything in it; tests that DO want to exercise these sources pass
# explicit grok_log_path=/grok_sessions_path=/hermes_state_db_path= paths.
_TEST_HARVEST_ROOT = Path(tempfile.mkdtemp(prefix="mc-test-harvest-"))

# Third incident of the same class (2026-09-07, omp agent): tests that spawn
# real subprocesses (render-omp-config.sh & Co.) inherited the agent
# container's OMP_ENV_FILE=/home/agent/.omp/omp.env. The script honours that
# variable over OMP_HOME, so a fixture run inside an omp agent's own container
# rewrote the agent's REAL omp.env with fixture values (HOME=/tmp/pytest-...,
# model org/glm53-exl3, endpoint 192.0.2.20) — every omp relaunch afterwards
# hit the setup wizard + "no-model" and the task stalled for 70 minutes.
# Scrub every runtime-shaped variable from the process env at import time so
# no test (and no subprocess it spawns) can address the operator's or the
# host agent's live config, whatever machine the suite runs on. HOME itself
# stays untouched — path-validation tests compare against the real one.
_RUNTIME_ENV_PREFIXES = ("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR", "MC_AGENT_TOKEN")
for _key in [k for k in os.environ if k.startswith(_RUNTIME_ENV_PREFIXES)]:
    os.environ.pop(_key, None)

# ── Postgres test lane (W0.2, 10.09.2026) ────────────────────────────────
# MC_TEST_DATABASE_URL=postgresql+asyncpg://... switches the suite from the
# SQLite in-memory engine to a real Postgres whose schema was applied by
# `alembic upgrade head`. What the lane adds (review #486, measured): drift
# on the DATABASE side — a missing or outdated plpgsql trigger
# `validate_task_transition` (migration 0159) and real row locks / two real
# transactions — which the SQLite lane structurally cannot see and passes
# green. Drift on the Python side is caught by both lanes. Tests that need
# the Postgres guarantees carry `@pytest.mark.postgres` and are skipped on
# the SQLite lane. The lane TRUNCATEs all tables → the DB name must contain
# "test" (guard below).
_PG_TEST_URL = os.environ.get("MC_TEST_DATABASE_URL", "").strip()
POSTGRES_LANE = _PG_TEST_URL.startswith("postgresql")


def _assert_test_database(url: str) -> None:
    """Refuse to run the Postgres lane against anything that is not clearly a
    TEST database. The lane TRUNCATEs every model table before each test
    (review #486 W1: one passed test emptied a filled `boards` table) — a
    developer who points MC_TEST_DATABASE_URL at their DATABASE_URL would
    wipe `mission_control`. Rule: the database NAME (last path segment) must
    contain "test". CI uses `.../test`."""
    from urllib.parse import urlparse
    name = (urlparse(url).path or "").rsplit("/", 1)[-1].lower()
    if "test" not in name:
        raise RuntimeError(
            f"MC_TEST_DATABASE_URL points at database {name!r} — the Postgres lane "
            "TRUNCATEs every table; the database name must contain 'test'."
        )


if POSTGRES_LANE:
    _assert_test_database(_PG_TEST_URL)

app.config.settings = app.config.Settings(
    database_url=_PG_TEST_URL or "postgresql+asyncpg://test:test@localhost:5432/test",
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
    grok_harvest_path=str(_TEST_HARVEST_ROOT / "unified.jsonl"),
    grok_sessions_path=str(_TEST_HARVEST_ROOT / "grok-sessions"),
    hermes_state_db_path=str(_TEST_HARVEST_ROOT / "state.db"),
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

if POSTGRES_LANE:
    # NullPool: every AsyncSession gets its OWN connection, so two sessions in
    # one test really are two transactions (row locks + identity-map traps
    # only show up that way — a shared pool makes them look harmless).
    from sqlalchemy.pool import NullPool
    test_engine = create_async_engine(_PG_TEST_URL, echo=False, poolclass=NullPool)
else:
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

async def _empty_all_tables(conn) -> None:
    from sqlalchemy import text as _text
    for table in reversed(SQLModel.metadata.sorted_tables):
        await conn.execute(_text(f'DELETE FROM "{table.name}"'))


# Schema fingerprint of the SQLite engine right after create_all — one row
# per table with its CREATE statement, so COLUMNS count, not just table
# presence. Review #488 B1: a migration test seeds `ALTER TABLE … ADD COLUMN`
# and aborts on purpose; `DELETE` names no columns, so the OperationalError
# rescue never fired and the next test saw the drifted schema ("duplicate
# column name"). The fingerprint is compared before EVERY test (one cheap
# query) and any drift — dropped table, added/removed column, changed
# constraint — triggers a full drop_all + create_all.
_PRISTINE_FINGERPRINT: dict = {"value": None}


async def _schema_fingerprint(conn) -> tuple:
    from sqlalchemy import text as _text
    rows = await conn.execute(_text(
        "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ))
    return tuple((r[0], r[1]) for r in rows)


async def _rebuild_schema(conn) -> None:
    await conn.run_sync(SQLModel.metadata.drop_all)
    await conn.run_sync(SQLModel.metadata.create_all)
    _PRISTINE_FINGERPRINT["value"] = await _schema_fingerprint(conn)


async def _ensure_pristine_schema(conn) -> bool:
    """Rebuild the schema if it drifted from the pristine fingerprint.
    Returns True when a rebuild happened (tests use this as the oracle)."""
    if _PRISTINE_FINGERPRINT["value"] is None:
        await _rebuild_schema(conn)
        return True
    if await _schema_fingerprint(conn) != _PRISTINE_FINGERPRINT["value"]:
        await _rebuild_schema(conn)
        return True
    return False


@pytest.fixture(autouse=True)
async def setup_db():
    """Give every test an empty database — without rebuilding the schema.

    Until 10.09.2026 this fixture ran create_all + drop_all around EVERY test:
    78 CREATE TABLEs plus 78 DROPs per test, each crossing aiosqlite's thread
    boundary — measured at ~78 ms per test (PR #316), i.e. most of the
    22-minute CI job spent building a schema that never changes. Now the
    schema is created once per engine (lazily, on the first test of each
    xdist worker) and every test starts by deleting the rows (~8 ms). The
    isolation is unchanged: empty tables at start, cleanup happens BEFORE the
    test so a crashed predecessor cannot poison its successor.

    Isolation of the SCHEMA (review #488 B1): the migration tests replay real
    Alembic steps against this engine — they drop tables and ADD COLUMNs and
    some abort on purpose mid-way. Before every test a schema fingerprint
    (sqlite_master: tables + indexes + their CREATE sql) is compared with the
    pristine one; any drift triggers drop_all + create_all. Only paid when
    something actually changed.

    Postgres lane: the schema comes from Alembic (triggers included) and is
    never dropped; isolation is a TRUNCATE of every model table."""
    from sqlalchemy.exc import OperationalError
    if POSTGRES_LANE:
        from sqlalchemy import text as _text
        names = ", ".join(f'"{t.name}"' for t in SQLModel.metadata.sorted_tables)
        async with test_engine.begin() as conn:
            await conn.execute(_text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        yield
        return
    async with test_engine.begin() as conn:
        await _ensure_pristine_schema(conn)
        try:
            await _empty_all_tables(conn)
        except OperationalError:
            # belt and braces: a table vanished between fingerprint and DELETE
            await _rebuild_schema(conn)
            await _empty_all_tables(conn)
    yield


def pytest_collection_modifyitems(config, items):
    """`postgres`-marked tests only run on the Postgres lane; everything else
    runs on both (the SQLite lane stays byte-identical when the env is unset)."""
    if POSTGRES_LANE:
        return
    skip = pytest.mark.skip(reason="needs the Postgres test lane (MC_TEST_DATABASE_URL)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
async def isolate_redis_singleton():
    """No test may reach the developer's real Redis.

    Most code takes `redis` from a fixture, but service code that calls
    ``get_redis()`` itself (runtime_grace, for one) would otherwise open the
    module-level singleton against ``settings.redis_url`` — which on a dev box
    IS the running Mission Control instance, sharing a key namespace with it.
    A test switching the "qwen-general" runtime would then write real keys and
    blind the real watcher for 20 minutes. Point the singleton at an in-memory
    server for every test; the ``client`` fixture still layers its own
    ``fake_redis`` on top and restores this one afterwards.
    """
    import app.redis_client as redis_client

    server = fakeredis.aioredis.FakeServer()
    isolated = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    original = redis_client._redis
    redis_client._redis = isolated
    yield isolated
    redis_client._redis = original
    await isolated.aclose()


@pytest.fixture(autouse=True)
def block_real_docker():
    """No test may reach the developer's real Docker daemon.

    Same reasoning as ``isolate_redis_singleton`` above, and it cost us a live
    incident to learn it (06.09.2026): a propagation test that did NOT patch
    ``reload_omp_config`` ran a real ``docker exec mc-agent-<name>
    render-omp-config.sh`` on the dev box — where an agent container of that
    very name was running — and wrote the test's fake endpoint/model into its
    live ``omp.env``. Until then the call happened to fail (the script's bootstrap
    was unauthenticated), so nobody noticed; the moment the fix made the call
    work, the test rewrote production config.

    Blocked is every ``docker`` call that could ACT on a container or image —
    the verb list below. Pure parsing calls (``docker compose config``,
    ``docker compose version``) stay allowed: they never reach the daemon, and
    the compose-template contract test needs them. Tests that WANT to inspect a
    blocked command still patch ``subprocess.run`` themselves — their patch
    replaces this guard, so nothing here gets in their way.
    """
    import subprocess

    # ``Popen`` bleibt bewusst unangetastet: es ist eine KLASSE, und Bibliotheken
    # annotieren damit (``subprocess.Popen[bytes]``) — als Funktion ersetzt,
    # bricht schon deren Import. MC ruft Docker ausschliesslich ueber
    # ``subprocess.run`` (geprueft), die anderen Namen sind Guertel und
    # Hosentraeger.
    originals = {
        name: getattr(subprocess, name)
        for name in ("run", "call", "check_call", "check_output")
    }
    acting_verbs = {
        "exec", "run", "start", "stop", "restart", "kill", "rm", "up", "down",
        "create", "commit", "cp", "pull", "push", "build", "prune", "logs",
    }

    def _is_docker(cmd) -> bool:
        if isinstance(cmd, (list, tuple)):
            parts = [str(part) for part in cmd]
        elif isinstance(cmd, str):
            parts = cmd.split()
        else:
            return False
        if not parts:
            return False
        head = parts[0]
        if head != "docker" and not head.endswith("/docker"):
            return False
        return any(part in acting_verbs for part in parts[1:])

    def _guard(name, original):
        def _wrapped(cmd, *args, **kwargs):
            if _is_docker(cmd):
                raise AssertionError(
                    f"subprocess.{name} tried to run Docker in a test: {cmd!r}. "
                    "Patch the calling function (or subprocess.run) instead — "
                    "a test must never touch real containers."
                )
            return original(cmd, *args, **kwargs)

        return _wrapped

    # Bewusst OHNE die monkeypatch-Fixture: eine autouse-Fixture, die
    # monkeypatch anfordert, zieht deren Aufbau vor die DB-Session — und damit
    # ihr Zuruecksetzen HINTER den Session-Abbau. Tests, die (wie
    # test_bench_orchestrator_flow) asyncio.create_task patchen, raeumen dann
    # in die falsche Reihenfolge hinein auf. Also selbst setzen und
    # zuruecksetzen.
    for name, original in originals.items():
        setattr(subprocess, name, _guard(name, original))
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(subprocess, name, original)


@pytest.fixture(autouse=True)
def reset_github_config_cache():
    """The github_config TTL cache must never leak between tests (ADR-055)."""
    from app.services.github_config import invalidate_github_config_cache
    invalidate_github_config_cache()
    yield
    invalidate_github_config_cache()


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
