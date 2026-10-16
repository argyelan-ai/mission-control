"""Shared-token gate of the cli-bridge host helper (scripts/cli-bridge.py).

The helper starts agents and attaches terminals on the host. Every endpoint
except /ping and every WebSocket PTY attach must require X-MC-Bridge-Token.
These tests run the real Handler / WS handler on an ephemeral loopback port.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cli-bridge.py"
TOKEN = "test-token-0123456789abcdef0123456789abcdef"


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    logdir = tmp_path_factory.mktemp("bridge-logs")
    old = os.environ.get("CLI_BRIDGE_LOG_DIR")
    os.environ["CLI_BRIDGE_LOG_DIR"] = str(logdir)
    try:
        spec = importlib.util.spec_from_file_location("cli_bridge_under_test", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is None:
            os.environ.pop("CLI_BRIDGE_LOG_DIR", None)
        else:
            os.environ["CLI_BRIDGE_LOG_DIR"] = old
    mod.BRIDGE_TOKEN = TOKEN
    return mod


@pytest.fixture(scope="module")
def http_base(bridge):
    server = HTTPServer(("127.0.0.1", 0), bridge.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _call(url: str, method: str = "GET", token: str | None = None, body: dict | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-MC-Bridge-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ── HTTP ──────────────────────────────────────────────────────────────────


class _NoSessions:
    stdout = ""
    stderr = ""
    returncode = 1


@pytest.fixture(autouse=True)
def _no_host_side_effects(bridge, monkeypatch):
    """Even if the gate were broken (sabotage runs), no request may touch the
    real host: every subprocess call returns "no tmux sessions"."""
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: _NoSessions())

    def _no_popen(*a, **k):
        raise AssertionError("subprocess.Popen reached in a unit test")

    monkeypatch.setattr(bridge._sp, "Popen", _no_popen)


# /start and /restart are left out on purpose: past a broken gate they would
# spawn tmux / re-exec the test process. The gate is one shared check, so the
# endpoints below cover it for all three verbs.
@pytest.mark.parametrize("method,path", [
    ("GET", "/health"),
    ("GET", "/status/abc"),
    ("GET", "/sessions"),
    ("GET", "/provision/zz-no-such-agent"),
    ("POST", "/input/zz-no-such-task"),
    ("DELETE", "/sessions/zz-no-such-task"),
])
def test_without_token_is_401(bridge, http_base, method, path):
    status, body = _call(http_base + path, method,
                         body={"text": "x"} if method == "POST" else None)
    assert status == 401
    assert json.loads(body) == {"error": "unauthorized"}


def test_wrong_token_is_401_and_not_echoed(bridge, http_base):
    status, body = _call(http_base + "/health", token="wrong-" + TOKEN)
    assert status == 401
    assert "wrong-" not in body and TOKEN not in body


def test_correct_token_is_served(bridge, http_base):
    status, body = _call(http_base + "/health", token=TOKEN)
    assert status == 200
    assert json.loads(body)["status"] == "ok"
    status, body = _call(http_base + "/status/abc", token=TOKEN)
    assert status == 200
    assert json.loads(body) == {"task_id": "abc", "status": "unknown"}


def test_ping_is_open_and_reveals_nothing(bridge, http_base):
    status, body = _call(http_base + "/ping")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_no_token_loaded_fails_closed(bridge, http_base, monkeypatch):
    """BRIDGE_TOKEN unset (e.g. imported, never started) must never mean
    'auth off' — not even an empty header gets through."""
    monkeypatch.setattr(bridge, "BRIDGE_TOKEN", None)
    assert _call(http_base + "/health", token="")[0] == 401
    assert _call(http_base + "/health", token=TOKEN)[0] == 401


def test_rejection_log_never_contains_the_token(bridge, http_base):
    _call(http_base + "/health", token="presented-secret-value")
    log_text = bridge.LOG_FILE.read_text()
    assert "rejected GET /health" in log_text
    assert "presented-secret-value" not in log_text
    assert TOKEN not in log_text


# ── WebSocket PTY ────────────────────────────────────────────────────────


def _ws_attach(bridge, headers: dict | None):
    import websockets

    async def run():
        async with websockets.serve(bridge._pty_ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/some-task", additional_headers=headers or {}
            ) as ws:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except websockets.ConnectionClosed:
                    pass
                return ws.close_code, ws.close_reason

    return asyncio.run(run())


def test_ws_without_token_is_closed_before_any_tmux_call(bridge, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: calls.append(a) or _NoSessions())
    code, reason = _ws_attach(bridge, None)
    assert (code, reason) == (1008, "unauthorized")
    assert calls == []  # no session lookup, no PTY


def test_ws_wrong_token_is_closed(bridge, monkeypatch):
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: _NoSessions())
    code, reason = _ws_attach(bridge, {"X-MC-Bridge-Token": "nope"})
    assert (code, reason) == (1008, "unauthorized")


def test_ws_correct_token_passes_the_gate(bridge, monkeypatch):
    """Past the gate the handler looks for the tmux session; none exists here,
    so the close reason proves the token was accepted."""
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: _NoSessions())
    code, reason = _ws_attach(bridge, {"X-MC-Bridge-Token": TOKEN})
    assert (code, reason) == (1008, "Session not found")


# ── token file + bind ─────────────────────────────────────────────────────


def test_token_file_created_once_0600(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv("CLI_BRIDGE_TOKEN", raising=False)
    path = tmp_path / "secrets" / "cli-bridge.token"
    first = bridge._load_or_create_token(path)
    assert len(first) >= 40
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert bridge._load_or_create_token(path) == first  # reused, not rotated
    assert first not in bridge.LOG_FILE.read_text()


def test_token_file_open_permissions_are_tightened(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv("CLI_BRIDGE_TOKEN", raising=False)
    path = tmp_path / "cli-bridge.token"
    path.write_text("abc\n")
    path.chmod(0o644)
    assert bridge._load_or_create_token(path) == "abc"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_env_token_wins_over_file(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_BRIDGE_TOKEN", "from-env")
    path = tmp_path / "cli-bridge.token"
    assert bridge._load_or_create_token(path) == "from-env"
    assert not path.exists()


def test_default_bind_is_loopback(bridge):
    assert bridge.BIND == "127.0.0.1"
    src = _SCRIPT.read_text()
    assert '"0.0.0.0"' not in src
