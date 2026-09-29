"""Terminals, the plugin shell and keystroke input into agent sessions are
admin-only (operator decision 24.09.2026).

Before this, any authenticated user of any role (viewer included) could open
a container or host-agent terminal, the plugin shell, or type text/keys into
an agent's live tmux pane via the Sessions chat. Every one of those is
interactive command execution on the box, so the admin role is required.

Each test proves both halves of the gate: a viewer and an operator are
refused, and an admin gets past the gate (to whatever the endpoint does
next — usually a 404/4004 for the made-up agent, which is fine: it shows the
role check did not stop the admin).
"""
from __future__ import annotations

import asyncio
import uuid

import fakeredis.aioredis
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app.config
from app.auth import create_access_token
from app.services import stream_tickets
from tests.conftest import test_engine

ADMIN_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OPERATOR_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
VIEWER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
USERS = {"admin": ADMIN_ID, "operator": OPERATOR_ID, "viewer": VIEWER_ID}

AGENT = "00000000-0000-0000-0000-00000000beef"  # no such agent

# Every interactive WebSocket: agent container terminal (docker exec → tmux),
# the cli-bridge worker/shell session, the legacy per-task session, the
# host-agent terminal (host-pty-bridge → tmux) and the plugin shell.
WS_PATHS = [
    f"/api/v1/agents/{AGENT}/terminal",
    f"/api/v1/agents/{AGENT}/terminal/ws",
    f"/api/v1/agents/{AGENT}/terminal/task-1/ws",
    f"/api/v1/host-agents/{AGENT}/terminal",
    "/api/v1/plugins/shell/ws",
]

# Every HTTP endpoint that types into a session or starts/stops a shell.
HTTP_ENDPOINTS = [
    ("POST", f"/api/v1/agents/{AGENT}/terminal/task-1/input", {"text": "ls\n"}),
    ("DELETE", f"/api/v1/agents/{AGENT}/terminal/task-1", None),
    ("POST", "/api/v1/plugins/shell", None),
    ("DELETE", "/api/v1/plugins/shell", None),
    ("POST", f"/api/v1/agents/{AGENT}/chat/input", {"text": "hello"}),
    ("POST", f"/api/v1/agents/{AGENT}/chat/keys", {"keys": ["Escape"]}),
    ("POST", f"/api/v1/agents/{AGENT}/chat/effort", {"level": "high"}),
]


async def _seed_users() -> None:
    from app.models.user import User

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for role, uid in USERS.items():
            if await s.get(User, uid) is None:
                s.add(User(id=uid, email=f"{role}@mc.local", name=role, role=role, is_active=True))
        await s.commit()


# ── WebSockets ────────────────────────────────────────────────────────────


@pytest.fixture
def ws_app(monkeypatch):
    """The real terminal/shell routers over a sync TestClient (a bare app:
    the full app's lifespan wants PostgreSQL), with the ticket store, the WS
    auth session and the request session pointed at the test backends."""
    from fastapi import FastAPI, Request

    import app.auth as auth_mod
    from app.database import get_session
    from app.routers import cli_plugins, cli_terminal

    fastapi_app = FastAPI()
    fastapi_app.include_router(cli_terminal.router)
    fastapi_app.include_router(cli_plugins.router)

    server = fakeredis.aioredis.FakeServer()

    async def _redis():
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(stream_tickets, "get_redis", _redis)
    monkeypatch.setattr(
        auth_mod, "_ws_session", lambda: AsyncSession(test_engine, expire_on_commit=False)
    )
    # A dead bridge answers at once (connection refused) instead of a DNS
    # lookup for host.docker.internal — the plugin shell has no agent lookup
    # that would stop an admin before the bridge.
    monkeypatch.setattr(app.config.settings, "free_code_bridge_url", "http://127.0.0.1:9")

    async def _session(request: Request = None):  # noqa: ARG001
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    fastapi_app.dependency_overrides[get_session] = _session

    def issue(role: str, path: str) -> str:
        return asyncio.run(
            stream_tickets.issue_ticket(user_id=str(USERS[role]), token_version=0, path=path)
        )

    with TestClient(fastapi_app) as c:
        c.portal.call(_seed_users)
        yield c, issue


def _close_code(client: TestClient, url: str) -> int | str:
    """The close code the server answers with, or "accepted" when the
    socket was accepted (the plugin shell then reports the dead bridge)."""
    try:
        with client.websocket_connect(url) as ws:
            ws.receive_text()
            return "accepted"
    except WebSocketDisconnect as exc:
        return exc.code


@pytest.mark.parametrize("path", WS_PATHS)
@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_ws_refuses_non_admin(ws_app, path, role):
    client, issue = ws_app
    code = _close_code(client, f"{path}?ticket={issue(role, path)}")
    assert code == 4003, f"{role} must be refused with 4003 on {path}, got {code}"


@pytest.mark.parametrize("path", WS_PATHS)
def test_ws_lets_admin_past_the_gate(ws_app, path):
    client, issue = ws_app
    code = _close_code(client, f"{path}?ticket={issue('admin', path)}")
    # 4004 = the (made-up) agent was looked up → the role gate passed;
    # "accepted" = the plugin shell opened and tried its bridge.
    assert code in (4004, "accepted"), f"admin blocked on {path}: {code}"


@pytest.mark.parametrize("path", WS_PATHS)
def test_ws_without_credentials_is_still_4001(ws_app, path):
    client, _ = ws_app
    assert _close_code(client, path) == 4001


# ── HTTP ──────────────────────────────────────────────────────────────────


def _bearer(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(USERS[role]), role)}"}


@pytest.fixture
async def http(client):
    await _seed_users()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", HTTP_ENDPOINTS)
@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_http_refuses_non_admin(http, method, path, body, role):
    r = await http.request(method, path, json=body, headers=_bearer(role))
    assert r.status_code == 403, f"{role} {method} {path} → {r.status_code} {r.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", HTTP_ENDPOINTS)
async def test_http_lets_admin_past_the_gate(http, method, path, body, monkeypatch):
    # The plugin shell calls the host bridge synchronously; answer for it.
    import app.routers.cli_terminal as cli_terminal

    monkeypatch.setattr(cli_terminal, "_bridge_post", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cli_terminal, "_bridge_delete", lambda *a, **k: {"ok": True})
    r = await http.request(method, path, json=body, headers=_bearer("admin"))
    assert r.status_code not in (401, 403), f"admin {method} {path} → {r.status_code} {r.text}"


@pytest.mark.asyncio
async def test_read_only_session_views_stay_open_to_viewers(http):
    """Watching is not typing: chat history and the session lists stay on
    the plain login (the gate is only on interactive endpoints)."""
    r = await http.get(f"/api/v1/agents/{AGENT}/chat/history", headers=_bearer("viewer"))
    assert r.status_code not in (401, 403), r.text


# ── Plugin update/remove must not smuggle a shell start/stop ──────────────
#
# ``/plugins/{plugin_key:path}`` decodes %23 to "#". Built into the bridge URL
# as ``/plugins/shell#/update`` (or ``/plugins/shell#``), urllib drops the
# fragment and the bridge receives ``POST /plugins/shell`` /
# ``DELETE /plugins/shell`` — the admin-only shell start/stop, reached on the
# plain login. Plugin keys are ``name@marketplace``; anything else is refused
# before the bridge is called.

SMUGGLED = [
    ("POST", "/api/v1/plugins/shell%23/update"),
    ("DELETE", "/api/v1/plugins/shell%23"),
    ("POST", "/api/v1/plugins/shell%3Fx/update"),
    ("DELETE", "/api/v1/plugins/shell%3Fx"),
    ("POST", "/api/v1/plugins/shell/update"),
    ("DELETE", "/api/v1/plugins/..%2Fshell"),
    ("POST", "/api/v1/plugins/a%20b@x/update"),
]


@pytest.fixture
def bridge_calls(monkeypatch):
    """Record the bridge paths without talking to a bridge."""
    import app.routers.cli_terminal as cli_terminal

    calls: list[tuple[str, str]] = []

    def _post(path, body, timeout=5):  # noqa: ARG001
        calls.append(("POST", path))
        return {"ok": True}

    def _delete(path):
        calls.append(("DELETE", path))
        return {"ok": True}

    monkeypatch.setattr(cli_terminal, "_bridge_post", _post)
    monkeypatch.setattr(cli_terminal, "_bridge_delete", _delete)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", SMUGGLED)
@pytest.mark.parametrize("role", ["viewer", "admin"])
async def test_plugin_key_cannot_reach_other_bridge_paths(http, bridge_calls, method, path, role):
    r = await http.request(method, path, headers=_bearer(role))
    assert r.status_code == 400, f"{role} {method} {path} → {r.status_code} {r.text}"
    assert bridge_calls == [], f"bridge was called: {bridge_calls}"


@pytest.mark.asyncio
async def test_plugin_install_refuses_bad_key(http, bridge_calls):
    r = await http.post(
        "/api/v1/plugins/install", json={"plugin_key": "shell#"}, headers=_bearer("admin")
    )
    assert r.status_code == 400, r.text
    assert bridge_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["frontend-design@claude-plugins-official", "superpowers@superpowers-dev", "my_plugin.v2"]
)
async def test_real_plugin_keys_still_reach_the_bridge(http, bridge_calls, key):
    r = await http.post(f"/api/v1/plugins/{key}/update", headers=_bearer("admin"))
    assert r.status_code == 200, r.text
    r = await http.delete(f"/api/v1/plugins/{key}", headers=_bearer("admin"))
    assert r.status_code == 200, r.text
    assert bridge_calls == [("POST", f"/plugins/{key}/update"), ("DELETE", f"/plugins/{key}")]
