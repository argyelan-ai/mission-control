"""Regression: SSE streams and WebSocket proxies must NOT hold a DB
connection (with its implicit transaction) for the lifetime of the stream.

Mechanism (proven, fastapi/routing.py ``request_response``): FastAPI enters
``Depends(get_session)`` on the request-scoped AsyncExitStack and unwinds it
only AFTER ``await response(...)`` — i.e. after an SSE stream / WebSocket
proxy has fully finished. Any session touching the DB pins a pool connection
plus an open (autobegin) transaction for the whole stream. Measured
2026-09-16: holds up to 405 s, 13 pool warnings/hour — the same mechanism
that exhausted the pool on 2026-09-14 (healthcheck 500 → container restart).

Two levers, both pinned here:
  1. auth deps (``require_user`` & Co.) release right after authentication —
     one place that heals EVERY auth-protected stream at once;
  2. stream endpoints with their own ``Depends(get_session)`` release after
     their last query, before returning the response.

The AST guard at the bottom fails when a NEW streaming endpoint is added
that takes a session dependency without releasing it (mutation-probed in
``test_guard_detects_newly_inserted_bad_endpoint``).
"""

import ast
import asyncio
import textwrap
import uuid
from pathlib import Path

import fakeredis.aioredis
import pytest
from fastapi import Request as _Request
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import get_session, managed_session

ROUTERS = Path(__file__).resolve().parent.parent / "app" / "routers"
AUTH = Path(__file__).resolve().parent.parent / "app" / "auth.py"

# Streaming response constructors whose surrounding endpoint holds the
# response open for as long as the client stays connected.
_STREAM_CALLS = {"EventSourceResponse", "StreamingResponse", "make_sse_response"}


# ── AST guard helpers ─────────────────────────────────────────────────────


def _is_depends_get_session(default: ast.expr) -> bool:
    """True for ``Depends(get_session)`` (the exact spelling used in this
    codebase's router signatures)."""
    return (
        isinstance(default, ast.Call)
        and isinstance(default.func, ast.Name)
        and default.func.id == "Depends"
        and len(default.args) == 1
        and isinstance(default.args[0], ast.Name)
        and default.args[0].id == "get_session"
    )


def _has_release_call(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "release_session"
        ):
            return True
    return False


def _takes_session_dep(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    defaults = list(fn.args.defaults) + [
        d for d in fn.args.kw_defaults if d is not None
    ]
    return any(_is_depends_get_session(default) for default in defaults)


def _streaming_endpoints(tree: ast.Module) -> list[str]:
    """Names of endpoint functions that keep the response open (WebSocket
    handlers, or HTTP handlers returning a streaming response) AND take a
    session dependency WITHOUT releasing it — directly or via a same-file
    helper it delegates to."""
    fns = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    releases = {fn.name: _has_release_call(fn) for fn in fns}

    def delegates_to_releasing_helper(fn) -> bool:
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and releases.get(node.func.id)
            ):
                return True
        return False

    endpoints = []
    for fn in fns:
        is_ws = any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "websocket"
            for dec in fn.decorator_list
        )
        returns_stream = any(
            isinstance(node, (ast.Return, ast.Expr))
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in _STREAM_CALLS
            for node in ast.walk(fn)
        )
        if (is_ws or returns_stream) and _takes_session_dep(fn):
            if not (_has_release_call(fn) or delegates_to_releasing_helper(fn)):
                endpoints.append(fn.name)
    return endpoints


def _scan_routers() -> list[str]:
    """All streaming endpoints that take a session dependency but never
    release it — i.e. every one of them pins a pool connection for the
    whole stream (see module docstring)."""
    violations = []
    for path in sorted(ROUTERS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for name in _streaming_endpoints(tree):
            violations.append(f"{path.name}: {name}")
    return violations


# ── Guard tests (mutation probe included) ─────────────────────────────────


def test_guard_no_stream_endpoint_holds_session():
    """Every streaming endpoint that takes Depends(get_session) must call
    release_session (directly or via a same-file helper) before returning
    the response."""
    violations = _scan_routers()
    assert violations == [], (
        "Streaming endpoints holding a Depends(get_session) session without "
        f"release_session() — each pins a pool connection + open transaction "
        f"for the whole stream: {violations}"
    )


def test_guard_auth_deps_release_session():
    """The user-facing auth deps run on EVERY stream endpoint (auth lever);
    they must release the connection after authenticating, otherwise every
    protected stream pins a connection for its whole lifetime.

    Exempt: require_agent — no agent-authenticated stream endpoint exists,
    and agent endpoints re-attach the returned Agent row via
    session.add(agent), which a closed shared session would break. If an
    agent-authenticated stream is ever added, give require_agent the same
    treatment as require_user (own session via use_cache=False + release).
    """
    tree = ast.parse(AUTH.read_text())
    exempt = {"require_agent"}
    missing = [
        fn.name
        for fn in tree.body
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name.startswith("require_")
        and fn.name not in exempt
        and _takes_session_dep(fn)
        and not _has_release_call(fn)
    ]
    assert missing == [], (
        f"auth dependencies {missing} query the DB but never call "
        "release_session — their connection + transaction live until after "
        "the response has fully streamed (SSE/WS!)"
    )


def test_guard_detects_newly_inserted_bad_endpoint():
    """Mutation probe: the guard must flag a NEW streaming endpoint that
    takes a session dependency without releasing it — and accept the fixed
    variant, so the rule is neither blind nor over-eager."""
    bad = textwrap.dedent(
        '''
        import uuid
        from fastapi import APIRouter, Depends
        from sqlmodel.ext.asyncio.session import AsyncSession
        from app.database import get_session
        from app.services.sse import make_sse_response

        router = APIRouter()

        @router.get("/things/stream")
        async def stream_things(session: AsyncSession = Depends(get_session)):
            rows = (await session.exec("SELECT 1")).all()  # type: ignore[attr-defined]
            return make_sse_response(["mc:events:things"])
        '''
    )
    assert _streaming_endpoints(ast.parse(bad)) == ["stream_things"]

    good = bad.replace(
        '    return make_sse_response(["mc:events:things"])',
        "    await release_session(session)\n"
        '    return make_sse_response(["mc:events:things"])',
    )
    assert _streaming_endpoints(ast.parse(good)) == []


# ── Behavioral regression (works on SQLite and Postgres lanes) ────────────

# NOTE on transport: httpx's ASGITransport BUFFERS the whole response body
# (it awaits the ASGI app to completion) — an infinite SSE stream never
# returns headers there and the test hangs. These tests therefore drive the
# ASGI app directly: a hand-rolled scope + queue-based `send` that yields
# body chunks as they arrive, exactly what a real server would deliver.


def _patch_sse_pubsub(fake_redis, monkeypatch) -> None:
    """Point ``_sse_generator``'s pubsub client (aioredis.from_url) at the
    same FakeServer the app's broadcast() uses via get_redis."""
    from app.services import sse as sse_mod

    server = fake_redis.connection_pool.connection_kwargs.get("server")
    if server is None:
        server = fakeredis.aioredis.FakeServer()

    def fake_from_url(*_a, **_k):
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(sse_mod.aioredis, "from_url", fake_from_url)


async def _seed_admin(engine) -> uuid.UUID:
    from app.models.user import User

    user_id = uuid.UUID("00000000-0000-0000-0000-000000000098")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.add(
            User(
                id=user_id,
                email="stream-release@mc.local",
                name="Stream Release Test",
                role="admin",
                is_active=True,
            )
        )
        await s.commit()
    return user_id


class _SseStream:
    """Runs one request against the ASGI app and exposes the streamed body
    lines as they arrive. Cancel `task` when done."""

    def __init__(self, app, path: str, token: str | None = None,
                 headers: list[tuple[bytes, bytes]] | None = None):
        self.app, self.path, self.token = app, path, token
        self.extra_headers = headers or []
        self.status: int | None = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        headers = list(self.extra_headers)
        if self.token:
            headers.append((b"authorization", f"Bearer {self.token}".encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }

        async def send(message) -> None:
            if message["type"] == "http.response.start":
                self.status = message["status"]
            elif message["type"] == "http.response.body":
                await self._queue.put(message.get("body", b""))
                if not message.get("more_body", False):
                    await self._queue.put(None)

        received = {"done": False}

        async def receive() -> dict:
            # First read: the (empty) request body; afterwards the client is
            # "gone" for body-purposes — Starlette raises on a second
            # http.request otherwise.
            if not received["done"]:
                received["done"] = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.Event().wait()  # never disconnects while streaming
            return {"type": "http.disconnect"}

        self.task = asyncio.create_task(self.app(scope, receive, send))
        for _ in range(100):
            if self.status is not None:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"stream never sent response headers: {self.path}")

    async def first_frame(self, publish, attempts: int = 10) -> str:
        """Next SSE event/data line, retrying `publish` between reads.
        Bounded — the generator's own keepalive ping event arrives within
        one ping interval even if pubsub delivery lags."""
        for _ in range(attempts):
            await publish()
            line = await self._next_line()
            if line.startswith(("event:", "data:")):
                return line
        raise AssertionError("stream produced no event frame at all")

    async def _next_line(self) -> str:
        buf = getattr(self, "_buf", b"")
        while True:
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._buf = buf
                return raw.decode().rstrip("\r")
            chunk = await self._queue.get()
            if chunk is None:
                self._buf = buf
                raise AssertionError("stream ended before a frame arrived")
            buf += chunk

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


@pytest.fixture
async def counting_app(monkeypatch, fake_redis):
    """The real app wired to a QueuePool engine (checkedout() is
    meaningful, unlike conftest's StaticPool) and to fakeredis pubsub.
    Yields (app, engine, session_override)."""
    from app.main import app as fastapi_app

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=AsyncAdaptedQueuePool
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await _seed_admin(engine)

    _patch_sse_pubsub(fake_redis, monkeypatch)

    async def override_get_session(request: _Request):
        session = AsyncSession(engine, expire_on_commit=False)
        # Same lifecycle as production get_session — the release/teardown
        # behavior under test is the real one, not a lookalike.
        async with managed_session(session, route=request.url.path):
            yield session

    fastapi_app.dependency_overrides[get_session] = override_get_session
    yield fastapi_app, engine, override_get_session
    fastapi_app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


@pytest.fixture
async def counting_auth(counting_app):
    """Token for the admin user seeded on the counting engine."""
    from app.auth import create_access_token

    app, engine, override_get_session = counting_app
    token = create_access_token("00000000-0000-0000-0000-000000000098", "admin")
    return app, engine, token


async def test_sse_stream_releases_db_connection_while_open(counting_auth):
    """Auth lever: while an SSE stream is OPEN and delivering frames, the
    pool must hold ZERO checked-out connections. Before the fix the auth
    dependency's session (one User lookup → autobegin transaction) stayed
    checked out until the stream ended."""
    import asyncio

    from app.services.sse import broadcast

    app, engine, token = counting_auth
    stream = _SseStream(app, "/api/v1/approvals/stream", token)
    await stream.start()
    try:
        async def publish():
            await broadcast("mc:events:approvals", "ping-test", {"n": 1})

        await stream.first_frame(publish)
        # Stream is live, frame delivered — connection must be back.
        assert engine.pool.checkedout() == 0, (
            "SSE stream holds a pooled DB connection while idle-waiting for "
            "events — the session was not released before streaming"
        )
    finally:
        await stream.stop()


async def test_group_stream_releases_db_connection_while_open(counting_auth):
    """Own-session lever: group_stream queries group + preview sources with
    its own Depends(get_session), then streams. The connection must be
    released before the generator runs, not after the stream ends."""
    from app.models.group import AgentGroup
    from app.services.sse import broadcast

    app, engine, token = counting_auth

    group_id = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as s:
        s.add(AgentGroup(id=group_id, thread_id=uuid.uuid4(), name="Stream Test", goal="regression probe"))
        await s.commit()

    stream = _SseStream(app, f"/api/v1/groups/{group_id}/stream", token)
    await stream.start()
    try:
        async def publish():
            await broadcast(f"mc:events:group:{group_id}", "ping-test", {"n": 1})

        await stream.first_frame(publish)
        assert engine.pool.checkedout() == 0, (
            "group_stream holds a pooled DB connection while streaming — "
            "release_session missing before EventSourceResponse"
        )
    finally:
        await stream.stop()


# ── Postgres-lane proof: measured transaction age during a live stream ────


@pytest.mark.postgres
async def test_pg_stat_activity_no_open_transaction_during_stream(
    auth_client: AsyncClient,
    fake_redis,
    monkeypatch,
):
    """The original watchdog evidence, reproduced in-process: while an SSE
    stream is open, pg_stat_activity must show NO 'idle in transaction'
    backend with a non-trivial transaction age for this database.

    Pre-fix this fails with a growing age (the require_user session's
    transaction lives as long as the stream — up to 405 s measured in
    production); post-fix the release closes the transaction immediately.
    """
    import asyncio

    import asyncpg

    from app.config import settings
    from app.main import app as fastapi_app
    from app.services.sse import broadcast

    _patch_sse_pubsub(fake_redis, monkeypatch)

    url = settings.database_url.replace("postgresql+asyncpg://", "postgres://")
    obs = await asyncpg.connect(url)

    async def open_txn_ages() -> list[float]:
        rows = await obs.fetch(
            """
            SELECT COALESCE(EXTRACT(EPOCH FROM (now() - xact_start)), 0) AS age
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'idle in transaction'
              AND pid <> pg_backend_pid()
            """
        )
        return [float(r["age"]) for r in rows]

    stream = _SseStream(
        fastapi_app,
        "/api/v1/approvals/stream",
        headers=[(b"authorization", auth_client.headers["authorization"].encode())],
    )
    try:
        await stream.start()
        try:
            async def publish():
                await broadcast("mc:events:approvals", "ping-test", {"n": 1})

            await stream.first_frame(publish)
            # Hold the stream OPEN well past the warning threshold so a
            # pinned transaction has time to AGE (the production finding was
            # age up to 405 s; here 4 s suffice to cross the 1 s line).
            for _ in range(8):
                await asyncio.sleep(0.5)
                await publish()
            ages = await open_txn_ages()
            stale = [a for a in ages if a > 1.0]
            # Measurement printout for the proof protocol (pytest -s).
            print(f"open-transaction ages during live stream: {ages}")
            assert stale == [], (
                "SSE stream holds an open DB transaction while waiting for "
                f"events (ages >1s: {stale}) — connection pinned for the "
                "whole stream"
            )
        finally:
            await stream.stop()
    finally:
        await obs.close()
