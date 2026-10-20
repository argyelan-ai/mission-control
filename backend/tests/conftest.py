"""
Central test fixtures for Mission Control Backend.

- In-memory SQLite DB (no PostgreSQL needed)
- fakeredis (no Redis server needed)
- FastAPI TestClient with auth
- Factory functions for test data
"""

import atexit
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

# ── Host paths: no test ever writes into the real home or checkout ───────
# Incident 2026-10-20: a plain local `pytest` wrote into the operator's real
# machine on every run. settings.home_host and the HOME_HOST env var default
# to the real home, settings.mc_repo_path to the real checkout — the one the
# live backend renders its fleet from. Measured on one run: new folders
# ~/.mc/agents/cardhostagent-host + cardhostagentoff-host (host launcher
# sync), rewritten skills/ folders, settings.json, plugins/*.json and
# .claude.json inside existing agent folders, and a rewrite of
# <checkout>/docker/docker-compose.agents.yml (write_compose_agents).
# Several modules resolve these paths ONCE at import (docker_agent_sync.
# AGENTS_DIR, pane_stream.AGENTS_ROOT, compose_renderer.DEFAULT_COMPOSE_PATH,
# dispatch._BACKEND_MOUNTED_ROOTS, cli_terminal._HOST_LAUNCH_AGENTS), so the
# redirect has to happen HERE, before the first `import app…`; a fixture
# alone would come too late for them. Every test process gets one stand-in
# host home in the temp dir; the repo path inside it is never created, which
# is exactly what CI sees (no checkout at the default path). HOME itself stays
# untouched: git, ssh and gh in test subprocesses read their config from it.
# What still uses the real HOME (measured 2026-10-20: full suite with an
# empty HOME, and with a filled one): git auth (GitService._ensure_git_auth)
# is moved onto the stand-in below ("Git auth"); the fixture
# `scratch_outside_tmp` in tests/test_mc_head_scratch_origin.py needs a
# folder outside every temp root and so makes ~/.cache/mc-head-test-<id> on
# purpose (removed after the test). Nothing else may: a test that needs "a
# folder in the home" takes the stand-in (HOME_HOST), as
# tests/test_visual_proof.py does.
# The autouse fixture `isolate_host_paths` below repeats the redirect for
# every test, and pytest_sessionstart / pytest_sessionfinish at the end of
# this file fail the run if the real places changed anyway.
#
# The real values are kept in MC_TEST_ORIGINAL_* the first time: xdist
# workers (and any pytest a test starts) inherit this process's env, which
# by then already holds the stand-in.
for _key in ("HOME_HOST", "MC_REPO_PATH"):
    os.environ.setdefault(f"MC_TEST_ORIGINAL_{_key}", os.environ.get(_key, ""))
_REAL_HOME = Path.home()
_REAL_HOST_HOME = Path(os.environ["MC_TEST_ORIGINAL_HOME_HOST"] or _REAL_HOME)
_REAL_MC_REPO = Path(
    os.environ["MC_TEST_ORIGINAL_MC_REPO_PATH"]
    or _REAL_HOST_HOME / "Workspace" / "Projects" / "mission-control"
)


def _make_test_host_home() -> Path:
    """A temp dir for the stand-in host home — never under /tmp/.

    A real host home never lies there, and MC treats /tmp/ as a writable
    root of its own (dispatch._BACKEND_MOUNTED_ROOTS): a stand-in under /tmp
    would make every host path look writable (seen on Linux CI, where the
    temp dir is /tmp). /var/tmp is the fallback there."""
    path = Path(tempfile.mkdtemp(prefix="mc-test-host-home-")).resolve()
    if not str(path).startswith("/tmp/"):
        return path
    shutil.rmtree(path, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix="mc-test-host-home-", dir="/var/tmp")).resolve()


_TEST_HOST_HOME = _make_test_host_home()
_TEST_MC_REPO = _TEST_HOST_HOME / "Workspace" / "Projects" / "mission-control"
atexit.register(shutil.rmtree, _TEST_HOST_HOME, ignore_errors=True)

# Env vars the app reads for host paths. MC_AGENTS_ROOT (mcp_sync) and
# MC_MCP_REGISTRY_DIR (mcp_registry) are the two places that would otherwise
# fall back to expanduser("~") — the real home.
_TEST_HOST_ENV = {
    "HOME_HOST": str(_TEST_HOST_HOME),
    "MC_AGENTS_ROOT": str(_TEST_HOST_HOME / ".mc" / "agents"),
    "MC_MCP_REGISTRY_DIR": str(_TEST_HOST_HOME / ".mc" / "mcp-servers"),
}
# MC_REPO_PATH is removed, not redirected: scripts use it to find their own
# code (scripts/hermes_acp_chat.py, scripts/auto-rebuild-agents-on-drift.sh),
# and without it they use the checkout under test. The setting
# mc_repo_path gets the stand-in below.
_TEST_HOST_ENV_REMOVED = ("MC_REPO_PATH",)
os.environ.update(_TEST_HOST_ENV)
for _key in _TEST_HOST_ENV_REMOVED:
    os.environ.pop(_key, None)
# `~` defaults of the token harvester, for a test that builds its own
# Settings() (the shared one below gets explicit values).
os.environ.update({
    "TOKEN_HARVEST_PATHS": json.dumps([str(_TEST_HOST_HOME / ".mc" / "agents")]),
    "GROK_HARVEST_PATH": str(_TEST_HOST_HOME / ".grok" / "logs" / "unified.jsonl"),
    "GROK_SESSIONS_PATH": str(_TEST_HOST_HOME / ".grok" / "sessions"),
    "HERMES_STATE_DB_PATH": str(_TEST_HOST_HOME / ".hermes" / "state.db"),
})

# Settings whose defaults derive from the host home or the checkout.
# tests/test_tests_never_touch_real_home.py walks EVERY settings field and
# fails if one still points at the real home — a new field with such a
# default has to be added here.
_TEST_HOST_SETTINGS = {
    "home_host": str(_TEST_HOST_HOME),
    "mc_repo_path": str(_TEST_MC_REPO),
    "wake_request_dir": _TEST_HOST_HOME / ".mc" / "wake-requests",
    "chat_media_root": _TEST_HOST_HOME / ".mc" / "chat-media",
    "token_harvest_paths": [str(_TEST_HOST_HOME / ".mc" / "agents")],
}

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

# Same class once more (head launcher): heads_root defaults to the host's
# ~/.mc/heads, which the real mc-head watcher reads. A test that forgot its
# monkeypatch must never drop a spool request there.
_TEST_HEADS_ROOT = Path(tempfile.mkdtemp(prefix="mc-test-heads-"))
# And for browser-session last images (ADR-088): never into the real ~/.mc.
_TEST_BROWSER_SESSIONS_ROOT = Path(tempfile.mkdtemp(prefix="mc-test-browser-sessions-"))

# Every test process makes these roots afresh (each xdist worker its own),
# so they go when the process ends — until 2026-10-20 they stayed, and
# $TMPDIR held thousands of them.
for _root in (_TEST_VAULT_ROOT, _TEST_HARVEST_ROOT, _TEST_HEADS_ROOT, _TEST_BROWSER_SESSIONS_ROOT):
    atexit.register(shutil.rmtree, _root, ignore_errors=True)

# Test ids must not carry these per-process paths: xdist collects in every
# worker and aborts the run when the ids differ ("Different tests were
# collected"). pytest_make_parametrize_id below puts a name in their place.
# Both spellings of each root: as made (macOS: /var/folders/…) and resolved
# (/private/var/folders/…), the longer one first — it contains the other.
_PER_PROCESS_ROOTS = tuple(
    (form, name)
    for root, name in (
        (_TEST_HOST_HOME, "<host-home>"),
        (_TEST_VAULT_ROOT, "<test-vault>"),
        (_TEST_HARVEST_ROOT, "<test-harvest>"),
        (_TEST_HEADS_ROOT, "<test-heads>"),
        (_TEST_BROWSER_SESSIONS_ROOT, "<test-browser-sessions>"),
    )
    for form in dict.fromkeys((str(root.resolve()), str(root)))
)

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
    heads_root=_TEST_HEADS_ROOT,
    heads_sync_interval=99999,
    chat_todo_sync_interval=99999,  # todo->checklist loop never auto-fires in tests
    browser_sessions_root=_TEST_BROWSER_SESSIONS_ROOT,
    browser_sessions_interval=99999,  # lifecycle loop never auto-fires in tests
    night_shift_interval=99999,  # night shift loop never auto-fires in tests
    lifecycle_watchdog_enabled=True,  # ADR-046: on by default; the check is only ever
                                      # invoked when a test calls _check_stuck_in_progress directly.
    grok_harvest_path=str(_TEST_HARVEST_ROOT / "unified.jsonl"),
    grok_sessions_path=str(_TEST_HARVEST_ROOT / "grok-sessions"),
    hermes_state_db_path=str(_TEST_HARVEST_ROOT / "state.db"),
    # Beweis-Tor off by default: tests that exercise it set warn/enforce
    # themselves (tests/test_proof_gate.py) — no other test may reach gh.
    proof_gate_mode="off",
    # Host home + checkout → the stand-in (see "Host paths" at the top).
    **_TEST_HOST_SETTINGS,
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
    """Delete every row of every model table.

    One query asks which tables hold rows at all; only those get a DELETE.
    Same end state as a DELETE per table (every table empty, schema and
    AUTOINCREMENT counters untouched), but one aiosqlite round trip instead of
    ~70 for the typical test that wrote into a handful of tables or none —
    the per-table DELETEs were ~14 ms of every test's setup (CI 2026-10-20:
    ~95 % of a no-op test's cost, minutes of the Backend Tests job).
    A table missing from the schema raises OperationalError here exactly like
    the old DELETE did, so setup_db's rebuild rescue still fires."""
    from sqlalchemy import text as _text
    tables = [t.name for t in reversed(SQLModel.metadata.sorted_tables)]
    probe = " UNION ALL ".join(
        f"SELECT {i} WHERE EXISTS (SELECT 1 FROM \"{name}\")"
        for i, name in enumerate(tables)
    )
    filled = {row[0] for row in await conn.execute(_text(probe))}
    for i, name in enumerate(tables):
        if i in filled:
            await conn.execute(_text(f'DELETE FROM "{name}"'))


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
def block_real_docker(request):
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

    # The one opt-out: a test marked ``throwaway_docker`` starts its own,
    # uniquely named container (``docker run --rm`` with a random suffix) and
    # removes it again — e.g. the backup.sh restore tests, which must prove
    # "truncated archive -> database unchanged" against a real Postgres. Such a
    # test never addresses a container it did not create.
    if request.node.get_closest_marker("throwaway_docker") is not None:
        yield
        return

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


@pytest.fixture(autouse=True)
def block_real_engine_metrics():
    """No test may ask a real engine's ``/metrics`` (E1 switch lock).

    Every switch now probes the engine it would end; the test fixtures use
    documentation addresses (192.0.2.x) that only time out. The default fake
    engine is unreachable — the lock then fails open, as it does live. Tests
    of the lock put their own ``httpx.MockTransport`` here.
    """
    import httpx

    from app.services.heads import engine

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no real engine in tests", request=request)

    original = engine._transport
    engine._transport = httpx.MockTransport(unreachable)
    yield
    engine._transport = original


# ── Host paths, per test + the guard around the whole run ───────────────

def _is_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _points_into_real_host(
    value,
    real_roots: tuple[Path, ...] | None = None,
    test_home: Path | None = None,
) -> bool:
    """True when a setting or env value addresses the real machine.

    That is a path under the real home, the real HOME_HOST or the real
    checkout (and not under the stand-in host home), or a ``~`` path, which
    expands to the real HOME. Lists and ``docker:host;…`` mapping strings are
    checked element by element; anything that is not a path never matches."""
    if real_roots is None:
        real_roots = (_REAL_HOME, _REAL_HOST_HOME, _REAL_MC_REPO)
    if test_home is None:
        test_home = _TEST_HOST_HOME
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_points_into_real_host(v, real_roots, test_home) for v in value)
    if not isinstance(value, (str, os.PathLike)):
        return False
    for token in os.fspath(value).replace(";", " ").replace(",", " ").replace(":", " ").split():
        if token.startswith("~"):
            return True
        if not token.startswith("/"):
            continue
        path = Path(os.path.normpath(token))
        if _is_under(path, test_home):
            continue
        if any(_is_under(path, root) for root in real_roots):
            return True
    return False


def _empty_test_host_home(home: Path | None = None) -> None:
    """Give the next test an empty stand-in host home (what CI has: a home
    without ~/.mc). Runs BEFORE the test, so a crashed predecessor cannot
    leave files behind for it — same reasoning as setup_db."""
    home = _TEST_HOST_HOME if home is None else home
    try:
        entries = list(os.scandir(home))
    except FileNotFoundError:
        home.mkdir(parents=True, exist_ok=True)
        return
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
        else:
            try:
                os.unlink(entry.path)
            except OSError:
                pass


def _redirect_host_paths(settings, environ) -> None:
    """Put every host-path setting and env var that addresses the real
    machine (or, for env vars, is missing — the app then falls back to
    expanduser("~")) back on the stand-in. Other values stay as they are."""
    for name, value in _TEST_HOST_SETTINGS.items():
        if _points_into_real_host(getattr(settings, name)):
            setattr(settings, name, value)
    for key, value in _TEST_HOST_ENV.items():
        current = environ.get(key)
        if current is None or _points_into_real_host(current):
            environ[key] = value
    for key in _TEST_HOST_ENV_REMOVED:
        if _points_into_real_host(environ.get(key)):
            environ.pop(key, None)


@pytest.fixture(autouse=True)
def isolate_host_paths():
    """Every test starts with its host paths in the stand-in host home.

    The redirect itself happens once at import (top of this file) — this
    fixture keeps it true for each test: a host-path setting or env var that
    points at the real home, the real HOME_HOST or the real checkout again
    (a test that set it without restoring, a deleted HOME_HOST, which makes
    the app fall back to expanduser("~")) is put back on the stand-in, the
    stand-in home is emptied, and after the test the values are reset.

    A test that wants another value sets it itself (monkeypatch, patch) — that
    runs after this fixture and wins. A value set earlier by a module- or
    session-scoped fixture is kept as long as it does not point at the real
    machine. Yields the stand-in host home.

    Deliberately without the monkeypatch fixture (see block_real_docker)."""
    settings = app.config.settings
    _redirect_host_paths(settings, os.environ)
    applied_settings = {name: getattr(settings, name) for name in _TEST_HOST_SETTINGS}
    applied_env = {key: os.environ.get(key) for key in (*_TEST_HOST_ENV, *_TEST_HOST_ENV_REMOVED)}
    _empty_test_host_home()
    try:
        yield _TEST_HOST_HOME
    finally:
        for name, value in applied_settings.items():
            setattr(settings, name, value)
        for key, value in applied_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# Git auth reads the HOME it runs under, not HOME_HOST: GitService.
# _ensure_git_auth writes ~/.git-credentials and runs `git config --global`
# (credential.helper, safe.directory, user.name, user.email) — and without a
# token it deletes ~/.git-credentials. Tests reach it through _run_cmd with
# the real HOME (test_git_service.py, test_worktree_relative_gitdir.py,
# test_agent_repo_binding.py): measured 2026-10-20, an existing credentials
# file was gone after every run, and with GH_TOKEN in the shell the token and
# a foreign git identity landed in the real home. So while it runs under the
# real HOME, HOME (and GIT_CONFIG_GLOBAL) point at the stand-in; a test that
# set HOME itself (tmp_path) runs the method unchanged.
from app.services.git_service import GitService as _GitService

_real_ensure_git_auth = _GitService._ensure_git_auth
_GIT_HOME_KEYS = ("HOME", "GIT_CONFIG_GLOBAL")


async def _ensure_git_auth_off_the_real_home(self) -> None:
    if os.path.realpath(os.path.expanduser("~")) != os.path.realpath(_REAL_HOME):
        return await _real_ensure_git_auth(self)
    saved = {key: os.environ.get(key) for key in _GIT_HOME_KEYS}
    os.environ["HOME"] = str(_TEST_HOST_HOME)
    os.environ["GIT_CONFIG_GLOBAL"] = str(_TEST_HOST_HOME / ".gitconfig")
    try:
        return await _real_ensure_git_auth(self)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_GitService._ensure_git_auth = _ensure_git_auth_off_the_real_home


# The guard: what the real machine looked like before the run and after it.
# Watched are the places tests have written into (incident 2026-10-20):
# the real ~/.mc/agents and the real fleet file
# docker/docker-compose.agents.yml (with its .bak/.tmp/.rejected) of the
# real checkout; and, by name only (depth 1), ~/.mc/references/groups and
# ~/.mc/workspaces, where older runs left room-*/running-*/paused-* group
# folders and test-agent workspaces. Only where they exist — on CI none
# does, so nothing is watched there. Live agents write logs, transcripts
# and session state into ~/.mc/agents all the time; those folders and
# suffixes are skipped, so the guard sees what tests write: new folders,
# agent config files, skills/, plugins/. Folders are compared by name, files
# by mtime and size. In the name-only places the live system adds a folder
# only when someone starts a group or creates an agent, and changes inside
# a folder are not looked at.
_HOST_GUARD_TREE_DEPTH = 4  # <slug>/claude-config/plugins/<file>
_HOST_GUARD_SKIP_DIRS = frozenset({
    "logs", "projects", "sessions", "session-env", "shell-snapshots",
    "file-history", "paste-cache", "debug", "telemetry", "statusline-state",
    "todos", "cache", "backups", "worktrees", "deliverables", "omp-sessions",
    "_tasks", ".git", "node_modules",
})
_HOST_GUARD_SKIP_SUFFIXES = (
    ".log", ".err", ".out", ".jsonl", ".paste", ".pid", ".lock", ".sock",
    ".marker", ".swp",
)
_HOST_GUARD_NAME_ONLY = (("references", "groups"), ("workspaces",))  # under <home>/.mc
_COMPOSE_FILE = "docker-compose.agents.yml"
_HOST_GUARD_KEY = pytest.StashKey[tuple]()
_HOST_GUARD_STAT = "home-guard failed"  # the word in the last summary line


def _main_checkout(repo: Path) -> Path:
    """The main checkout behind a git worktree (``repo`` itself otherwise).
    A worktree's ``.git`` is a file pointing at <main>/.git/worktrees/<name>,
    whose ``commondir`` leads back to <main>/.git."""
    marker = repo / ".git"
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except (IsADirectoryError, FileNotFoundError, PermissionError, NotADirectoryError):
        return repo
    if not text.startswith("gitdir:"):
        return repo
    gitdir = (repo / text.split(":", 1)[1].strip()).resolve()
    try:
        common = (gitdir / (gitdir / "commondir").read_text(encoding="utf-8").strip()).resolve()
    except OSError:
        return repo
    return common.parent


def _host_guard_targets(
    homes: tuple[Path, ...] | None = None,
    repos: tuple[Path, ...] | None = None,
) -> tuple[tuple[str, Path], ...]:
    """("tree", <home>/.mc/agents) and ("names", <home>/.mc/references/
    groups | workspaces) for every home that has MC installed (<home>/.mc
    exists), ("compose", <repo>/docker) for every checkout that holds a real
    fleet file. Defaults: the real home + HOME_HOST, and the real checkout +
    the main checkout behind this one."""
    if homes is None:
        homes = (_REAL_HOME, _REAL_HOST_HOME)
    if repos is None:
        here = Path(__file__).resolve().parents[2]
        repos = (_REAL_MC_REPO, _main_checkout(here), here)
    targets: dict[Path, str] = {}
    for home in homes:
        if (home / ".mc").is_dir():
            targets.setdefault((home / ".mc" / "agents").resolve(), "tree")
            for parts in _HOST_GUARD_NAME_ONLY:
                targets.setdefault(home.joinpath(".mc", *parts).resolve(), "names")
    for repo in repos:
        if (repo / "docker" / _COMPOSE_FILE).is_file():
            targets.setdefault((repo / "docker").resolve(), "compose")
    return tuple((kind, path) for path, kind in targets.items())


def _host_guard_snapshot(targets) -> dict[str, tuple[int, int] | None]:
    """path -> (mtime_ns, size) for files, None for folders and for every
    entry of a "names" target (names only)."""
    snap: dict[str, tuple[int, int] | None] = {}

    def add_file(path: str) -> None:
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return
        snap[path] = (st.st_mtime_ns, st.st_size)

    for kind, root in targets:
        if kind == "compose":
            for name in (_COMPOSE_FILE, *(f"{_COMPOSE_FILE}{s}" for s in (".bak", ".tmp", ".rejected"))):
                add_file(os.path.join(root, name))
            continue
        if kind == "names":
            try:
                entries = list(os.scandir(root))
            except OSError:
                continue
            for entry in entries:
                is_dir = entry.is_dir(follow_symlinks=False)
                snap[entry.path + (os.sep if is_dir else "")] = None
            continue
        stack = [(os.fspath(root), 1)]
        while stack:
            folder, depth = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _HOST_GUARD_SKIP_DIRS:
                        continue
                    snap[entry.path + os.sep] = None
                    if depth < _HOST_GUARD_TREE_DEPTH:
                        stack.append((entry.path, depth + 1))
                elif not entry.name.endswith(_HOST_GUARD_SKIP_SUFFIXES):
                    add_file(entry.path)
    return snap


def _host_guard_diff(before: dict, after: dict) -> list[str]:
    lines = [f"new:     {p}" for p in sorted(after.keys() - before.keys())]
    lines += [f"gone:    {p}" for p in sorted(before.keys() - after.keys())]
    lines += [
        f"changed: {p}"
        for p in sorted(before.keys() & after.keys())
        if before[p] != after[p]
    ]
    return lines


def pytest_make_parametrize_id(config, val, argname):
    """A test id without the per-process temp roots (_PER_PROCESS_ROOTS):
    each is replaced by its name, e.g. "<host-home>/.mc/agents/x". Under
    xdist every worker has its own roots, so an id built from one differs
    between workers and the whole run stops at collection. Values without
    such a root return None and keep pytest's own id."""
    if not isinstance(val, (str, os.PathLike)):
        return None
    text = os.fspath(val)
    if not isinstance(text, str):
        return None
    stable = text
    for form, name in _PER_PROCESS_ROOTS:
        stable = stable.replace(form, name)
    return stable if stable != text else None


def pytest_sessionstart(session):
    # Under xdist the controller watches the whole run; workers skip.
    if hasattr(session.config, "workerinput"):
        return
    targets = _host_guard_targets()
    if targets:
        session.config.stash[_HOST_GUARD_KEY] = (targets, _host_guard_snapshot(targets))


def pytest_sessionfinish(session, exitstatus):
    state = session.config.stash.get(_HOST_GUARD_KEY, None)
    if state is None:
        return
    targets, before = state
    changes = _host_guard_diff(before, _host_guard_snapshot(targets))
    if not changes:
        return
    lines = [
        "Tests must write only into tmp_path or the stand-in host home",
        "(backend/tests/conftest.py, 'Host paths'). These real places changed",
        "while this run was going:",
        "",
        *(f"  {line}" for line in changes[:40]),
        *([f"  … and {len(changes) - 40} more"] if len(changes) > 40 else []),
        "",
        "Find the test: the folder or agent name usually names it",
        "(grep the tests for it), or run the suspects alone with -p no:xdist.",
        "Then remove what the run left there. Not from this run? Another",
        "process may have written at the same time: a live agent, the live",
        "backend, or a test run on a branch from before this guard.",
    ]
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        # This hook runs inside the reporter's own sessionfinish, before it
        # prints the summary — so the failure also lands in the LAST line
        # ("N passed, 1 home-guard failed"), which is what people read and
        # what `| tail -1` shows. The empty line ends xdist's open progress
        # line ("[100%]") before the banner.
        reporter.stats.setdefault(_HOST_GUARD_STAT, []).append(
            SimpleNamespace(count_towards_summary=True, changes=len(changes))
        )
        reporter.write_line("")
        reporter.write_sep("=", "the real home or checkout changed during this test run", red=True, bold=True)
        for line in lines:
            reporter.write_line(line)
    else:
        import sys
        sys.stderr.write("\n".join(["real home or checkout changed during this test run", *lines]) + "\n")
    if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
async def session()-> AsyncGenerator[AsyncSession, None]:
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

    from fastapi import Request as _Request

    from app.database import managed_session

    async def override_get_session(request: _Request):
        # Same lifecycle as production get_session (managed_session): the
        # release/observability behavior under test is the real one, not a
        # lookalike. SQLite/StaticPool ignores pool_timeout — irrelevant here.
        session = AsyncSession(test_engine, expire_on_commit=False)
        async with managed_session(session, route=request.url.path):
            yield session

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
