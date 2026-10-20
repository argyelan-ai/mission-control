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
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cli-bridge.py"
TOKEN = "test-token-0123456789abcdef0123456789abcdef"
# The helper's `_sp` IS the subprocess module, and the autouse fixture below
# stubs it out; tests that really start a child process use this original.
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen


@pytest.fixture
def real_subprocess(monkeypatch):
    """Undo the host-side-effect stub for tests that start the script as a
    separate process (it only reads env and exits)."""
    monkeypatch.setattr(subprocess, "run", _REAL_RUN)
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)


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
    server = bridge._make_http_server("127.0.0.1", 0)
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
    """Liveness plus the update door's API version (ADR-094) — so MC can say
    "this bridge is too old for the update button" — and nothing else."""
    status, body = _call(http_base + "/ping")
    assert status == 200
    assert json.loads(body) == {"status": "ok", "api_version": bridge._door_mod.API_VERSION}


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


def _ws_handshake(bridge, headers: dict | None):
    """Connect to the helper's real PTY server definition (_ws_serve).
    Returns ("http", status) when the handshake is refused, or
    ("ws", close_code, close_reason) when the WebSocket opened."""
    import websockets

    async def run():
        async with bridge._ws_serve("127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/some-task", additional_headers=headers or {}
                ) as ws:
                    try:
                        await asyncio.wait_for(ws.recv(), timeout=5)
                    except websockets.ConnectionClosed:
                        pass
                    return ("ws", ws.close_code, ws.close_reason)
            except websockets.InvalidStatus as e:
                return ("http", e.response.status_code)

    return asyncio.run(run())


def test_ws_without_token_is_refused_in_the_handshake(bridge, monkeypatch):
    calls = []
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: calls.append(a) or _NoSessions())
    assert _ws_handshake(bridge, None) == ("http", 401)
    assert calls == []  # no session lookup, no PTY


def test_ws_wrong_token_is_refused_in_the_handshake(bridge):
    assert _ws_handshake(bridge, {"X-MC-Bridge-Token": "nope"}) == ("http", 401)


def test_ws_correct_token_passes_the_gate(bridge):
    """Past the gate the handler looks for the tmux session; none exists here,
    so the close reason proves the token was accepted."""
    assert _ws_handshake(bridge, {"X-MC-Bridge-Token": TOKEN}) == ("ws", 1008, "Session not found")


def test_ws_handler_itself_also_checks_the_token(bridge, monkeypatch):
    """Defence in depth: served WITHOUT the handshake gate, the handler still
    closes 1008 before any tmux call."""
    import websockets

    calls = []
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **k: calls.append(a) or _NoSessions())

    async def run():
        async with websockets.serve(bridge._pty_ws_handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}/x") as ws:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except websockets.ConnectionClosed:
                    pass
                return ws.close_code, ws.close_reason

    assert asyncio.run(run()) == (1008, "unauthorized")
    assert calls == []


# ── threading: a stuck client cannot block the helper ─────────────────────


def test_stuck_client_does_not_block_other_requests(bridge):
    server = bridge._make_http_server("127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    stuck = socket.create_connection((host, port), timeout=5)
    try:
        stuck.sendall(b"GET /ping HTTP/1.1\r\nHost: x\r\n")  # never finishes
        time.sleep(0.2)
        started = time.monotonic()
        status, body = _call(f"http://{host}:{port}/ping")
        assert status == 200 and json.loads(body)["status"] == "ok"
        assert time.monotonic() - started < 3
    finally:
        stuck.close()
        server.shutdown()
        server.server_close()


def test_handler_has_a_socket_timeout(bridge):
    assert 0 < bridge.Handler.timeout <= 60


# ── token file ────────────────────────────────────────────────────────────


LONG = "x" * 40


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
    path.write_text(LONG + "\n")
    path.chmod(0o644)
    assert bridge._load_or_create_token(path) == LONG
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_env_token_wins_over_file(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_BRIDGE_TOKEN", "e" * 32)
    path = tmp_path / "cli-bridge.token"
    assert bridge._load_or_create_token(path) == "e" * 32
    assert not path.exists()


def test_short_env_token_refuses_to_start(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("CLI_BRIDGE_TOKEN", "e" * 31)
    with pytest.raises(bridge.WeakTokenError) as exc:
        bridge._load_or_create_token(tmp_path / "cli-bridge.token")
    assert "e" * 31 not in str(exc.value)


def test_short_token_file_refuses_to_start(bridge, tmp_path, monkeypatch):
    monkeypatch.delenv("CLI_BRIDGE_TOKEN", raising=False)
    path = tmp_path / "cli-bridge.token"
    path.write_text("short\n")
    with pytest.raises(bridge.WeakTokenError):
        bridge._load_or_create_token(path)


def test_short_token_exits_the_process_nonzero(tmp_path, real_subprocess):
    """End to end: the script itself refuses to start (exit 2), logs why,
    and never writes the value anywhere."""
    env = {**os.environ, "CLI_BRIDGE_TOKEN": "too-short-value",
           "CLI_BRIDGE_LOG_DIR": str(tmp_path), "HOME": str(tmp_path)}
    r = _REAL_RUN([sys.executable, str(_SCRIPT), "--init-token"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2
    assert "refusing to start" in r.stdout
    assert "too-short-value" not in r.stdout + r.stderr
    assert "too-short-value" not in (tmp_path / "cli-bridge.log").read_text()


# ── bind ──────────────────────────────────────────────────────────────────


def _non_loopback_ipv4() -> str | None:
    """This machine's primary non-loopback address (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1, routing lookup only
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


def test_default_bind_is_loopback(bridge):
    assert bridge.BIND == "127.0.0.1"
    assert '"0.0.0.0"' not in _SCRIPT.read_text()


def test_server_on_default_bind_listens_on_loopback_only(bridge):
    server = bridge._make_http_server(bridge.BIND, 0)
    try:
        assert server.socket.getsockname()[0] == "127.0.0.1"
        port = server.server_address[1]
        ip = _non_loopback_ipv4()
        if ip is None:
            pytest.skip("no non-loopback IPv4 address on this machine")
        with pytest.raises(OSError):  # refused: nothing listens on that address
            socket.create_connection((ip, port), timeout=2).close()
    finally:
        server.server_close()


def test_bind_env_is_honoured(tmp_path, real_subprocess):
    env = {**os.environ, "CLI_BRIDGE_BIND": "0.0.0.0", "CLI_BRIDGE_LOG_DIR": str(tmp_path)}
    code = (
        "import importlib.util,sys;"
        f"s=importlib.util.spec_from_file_location('b', {str(_SCRIPT)!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(m.BIND)"
    )
    r = _REAL_RUN([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == "0.0.0.0", r.stderr
