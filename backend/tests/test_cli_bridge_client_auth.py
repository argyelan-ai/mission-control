"""The backend sends X-MC-Bridge-Token on every call to the cli-bridge helper.

The helper (scripts/cli-bridge.py) answers 401 / closes the WebSocket without
it, so a caller that forgets the header breaks provisioning, plugins, image
builds or the terminal. Covered here:

* token source: CLI_BRIDGE_TOKEN wins, else the helper's token file, else none
* the shared HTTP helpers (cli_terminal), the async clients (cli_update_runner,
  cli_versions) and the WebSocket PTY proxy end to end
* a guard: every backend module that talks to free_code_bridge_url builds its
  headers through app.services.cli_bridge_auth
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from pathlib import Path

import httpx
import pytest

import app.config
from app.services import cli_bridge_auth

TOKEN = "unit-test-bridge-token"
HEADER = "X-MC-Bridge-Token"
APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def token_env(monkeypatch, tmp_path):
    """Explicit token set; the default token file path points into tmp."""
    monkeypatch.setattr(app.config.settings, "cli_bridge_token", TOKEN)
    monkeypatch.setattr(app.config.settings, "cli_bridge_token_file", str(tmp_path / "none"))
    return TOKEN


# ── token source ──────────────────────────────────────────────────────────


def test_env_token_wins(token_env, tmp_path, monkeypatch):
    f = tmp_path / "file.token"
    f.write_text("from-file\n")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token_file", str(f))
    assert cli_bridge_auth.bridge_headers() == {HEADER: TOKEN}


def test_token_file_fallback(monkeypatch, tmp_path):
    f = tmp_path / "cli-bridge.token"
    f.write_text("from-file\n")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token", "")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token_file", str(f))
    assert cli_bridge_auth.bridge_headers() == {HEADER: "from-file"}


def test_default_file_is_under_home_host(monkeypatch, tmp_path):
    secrets_dir = tmp_path / ".mc" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "cli-bridge.token").write_text("home-token")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token", "")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token_file", "")
    monkeypatch.setattr(app.config.settings, "home_host", str(tmp_path))
    assert cli_bridge_auth.bridge_headers() == {HEADER: "home-token"}


def test_no_token_means_no_header_and_no_value_in_logs(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(app.config.settings, "cli_bridge_token", "")
    monkeypatch.setattr(app.config.settings, "cli_bridge_token_file", str(tmp_path / "missing"))
    monkeypatch.setattr(cli_bridge_auth, "_warned_missing", False)
    with caplog.at_level(logging.WARNING, logger="mc.cli_bridge_auth"):
        assert cli_bridge_auth.bridge_headers() == {}
    assert "token not configured" in caplog.text


# ── HTTP callers ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("call", ["get", "post", "delete"])
def test_cli_terminal_helpers_send_the_token(token_env, monkeypatch, call):
    from app.routers import cli_terminal

    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return _Resp({"ok": True})

    monkeypatch.setattr(cli_terminal.urllib.request, "urlopen", fake_urlopen)
    if call == "get":
        cli_terminal._bridge_get("/health")
    elif call == "post":
        cli_terminal._bridge_post("/plugins/shell", {})
    else:
        cli_terminal._bridge_delete("/plugins/shell")
    assert len(seen) == 1
    # urllib normalises header names with .capitalize()
    assert seen[0].get_header(HEADER.capitalize()) == TOKEN


def test_cli_terminal_401_is_logged_without_the_token(token_env, monkeypatch, caplog):
    import urllib.error

    from app.routers import cli_terminal

    def deny(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(cli_terminal.urllib.request, "urlopen", deny)
    with caplog.at_level(logging.WARNING, logger="mc.cli_terminal"):
        assert cli_terminal._bridge_get("/health") is None
        result = cli_terminal._bridge_post("/plugins/shell", {})
    assert "rejected (401)" in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(result)


@pytest.mark.asyncio
async def test_cli_update_runner_client_sends_the_token(token_env):
    from app.services import cli_update_runner

    seen = []
    async with cli_update_runner._client() as c:
        c._transport = httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(200, json={})
        )
        await c.get("/agent-images/build/status")
    assert seen[0].headers.get(HEADER) == TOKEN


@pytest.mark.asyncio
async def test_cli_versions_host_probe_sends_the_token(token_env, monkeypatch):
    from app.services import cli_versions

    seen = []
    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(
            lambda r: seen.append(r) or httpx.Response(200, json={"version": "1.0.0"})
        )
        return real(*a, **k)

    monkeypatch.setattr(cli_versions.httpx, "AsyncClient", client)
    assert await cli_versions.installed_version_host("grok") == "1.0.0"
    assert seen[0].url.path == "/host-cli/grok/version"
    assert seen[0].headers.get(HEADER) == TOKEN


# ── WebSocket PTY proxy, end to end ──────────────────────────────────────


@pytest.fixture
def fake_helper_ws():
    """A stand-in for the helper's PTY server that records the handshake
    header and greets once."""
    import websockets

    seen: list[str | None] = []
    ready = threading.Event()
    state: dict = {}

    async def handler(ws):
        seen.append(ws.request.headers.get(HEADER))
        await ws.send("hello-from-helper")
        await ws.close()

    def run():
        loop = asyncio.new_event_loop()
        state["loop"] = loop

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                state["port"] = server.sockets[0].getsockname()[1]
                state["stop"] = asyncio.Event()
                ready.set()
                await state["stop"].wait()

        loop.run_until_complete(main())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert ready.wait(5)
    yield state["port"], seen
    state["loop"].call_soon_threadsafe(state["stop"].set)
    t.join(5)


@pytest.fixture
def plugin_shell_client(monkeypatch):
    """The real plugin-shell WS proxy (admin + stream ticket) over a bare app,
    wired like tests/test_terminal_admin_only.py."""
    import uuid

    import fakeredis.aioredis
    from fastapi import FastAPI
    from sqlmodel.ext.asyncio.session import AsyncSession
    from starlette.testclient import TestClient

    import app.auth as auth_mod
    from app.models.user import User
    from app.routers import cli_plugins
    from app.services import stream_tickets
    from tests.conftest import test_engine

    admin_id = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
    fastapi_app = FastAPI()
    fastapi_app.include_router(cli_plugins.router)
    server = fakeredis.aioredis.FakeServer()

    async def _redis():
        return fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)

    monkeypatch.setattr(stream_tickets, "get_redis", _redis)
    monkeypatch.setattr(
        auth_mod, "_ws_session", lambda: AsyncSession(test_engine, expire_on_commit=False)
    )

    async def _seed():
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            if await s.get(User, admin_id) is None:
                s.add(User(id=admin_id, email="bridge-admin@mc.local", name="a",
                           role="admin", is_active=True))
            await s.commit()

    with TestClient(fastapi_app) as c:
        c.portal.call(_seed)
        path = "/api/v1/plugins/shell/ws"
        ticket = asyncio.run(
            stream_tickets.issue_ticket(user_id=str(admin_id), token_version=0, path=path)
        )
        yield c, f"{path}?ticket={ticket}"


def test_ws_proxy_sends_the_token_in_the_handshake(
    token_env, fake_helper_ws, plugin_shell_client, monkeypatch
):
    """Browser → backend proxy (ticket, admin) → helper: the helper sees the
    shared token in the handshake; the browser never holds it."""
    port, seen = fake_helper_ws
    client, url = plugin_shell_client
    monkeypatch.setattr(app.config.settings, "free_code_bridge_url", f"http://127.0.0.1:{port}")
    with client.websocket_connect(url) as ws:
        assert ws.receive_text() == "hello-from-helper"
    assert seen == [TOKEN]
    assert TOKEN not in url


# ── guard: no caller builds bridge requests on its own ───────────────────


def test_every_bridge_request_carries_bridge_headers():
    """Per call site, not per file: every place that builds a request from
    free_code_bridge_url (an f-string URL or an httpx base_url) must pass
    bridge_headers() within the next few lines. Mentions in messages
    (``bridge_url`` in a health payload, a log hint) are not requests."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path.name in ("config.py", "cli_bridge_auth.py"):
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            is_request = (
                re.search(r"\{settings\.free_code_bridge_url\}[/{]", line)
                or "base_url=settings.free_code_bridge_url" in line
            )
            if not is_request:
                continue
            window = "\n".join(lines[i : i + 8])
            if "bridge_headers()" not in window:
                offenders.append(f"{path.relative_to(APP_DIR)}:{i + 1}")
    assert offenders == [], f"bridge requests without X-MC-Bridge-Token: {offenders}"


def test_every_bridge_ws_connect_carries_the_headers():
    for name in ("routers/cli_terminal.py", "routers/cli_plugins.py"):
        text = (APP_DIR / name).read_text()
        connects = re.findall(r"_ws\.connect\((.*?)\)\s+as ", text)
        assert connects, name
        for args in connects:
            assert "additional_headers=bridge_headers()" in args, f"{name}: {args}"
