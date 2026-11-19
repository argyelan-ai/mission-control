"""Hermes over ACP reports the chat's context fill on its heartbeat.

Round-2 user test (t4 S-neu-3, 23.10.2026): Hermes' agent page showed
"Kontext —" while its chat said "2 % belegt". Under the ACP driver Hermes has
no TUI pane, so `_heartbeat_body()`'s statusline scrape found nothing and the
heartbeat carried no `context_pct`. The chat daemon now keeps the agent's last
`usage_update` (docker/omp-bridge/acp_chat.py `state()["context"]`) and the
bridge falls back to it — the same figure the chat ring shows.

A scrape hit still wins (native path unchanged); a daemon without a reading,
or one that raises, adds nothing and never breaks the heartbeat.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"
ACP_CHAT_PATH = REPO_ROOT / "scripts" / "hermes_acp_chat.py"

NO_STATUSLINE = "│ ctx -- │ [░░░░░░░░░░] -- │"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge():
    return _load(BRIDGE_PATH, "hermes_bridge_acp_ctx")


class _Daemon:
    def __init__(self, *, pct=None, raises=False):
        self._pct = pct
        self._raises = raises

    def busy(self):
        return False

    def context_pct(self):
        if self._raises:
            raise RuntimeError("state unreadable")
        return self._pct


def test_acp_heartbeat_reports_the_chat_fill(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "capture_pane", lambda: NO_STATUSLINE)
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _Daemon(pct=2.0))
    body = json.loads(bridge._heartbeat_body())
    assert body == {"context_pct": 2.0, "status": "idle"}


def test_a_statusline_reading_still_wins(bridge, monkeypatch):
    monkeypatch.setattr(
        bridge, "capture_pane",
        lambda: " ⚕ model │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s",
    )
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _Daemon(pct=2.0))
    assert json.loads(bridge._heartbeat_body())["context_pct"] == 8.0


@pytest.mark.parametrize("daemon", [_Daemon(pct=None), _Daemon(raises=True)])
def test_no_reading_adds_nothing(bridge, monkeypatch, daemon):
    monkeypatch.setattr(bridge, "capture_pane", lambda: NO_STATUSLINE)
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: daemon)
    body = json.loads(bridge._heartbeat_body())
    assert "context_pct" not in body
    assert body.get("status") == "idle"


def test_native_driver_never_asks_the_daemon(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "capture_pane", lambda: NO_STATUSLINE)
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: False)

    def _boom():
        raise AssertionError("native path must not touch the ACP daemon")

    monkeypatch.setattr(bridge, "chat_daemon", _boom)
    assert json.loads(bridge._heartbeat_body()) == {}


class _Session:
    def __init__(self, context):
        self._context = context

    def state(self):
        return {"busy": False, "context": self._context}


def test_chat_daemon_reads_pct_from_the_session_state():
    acp = _load(ACP_CHAT_PATH, "hermes_acp_chat_ctx")
    daemon = acp.ChatDaemon(lambda: None)
    assert daemon.context_pct() is None  # not started
    daemon._session = _Session({"used": 20_972, "size": 1_048_576, "pct": 2.0})
    assert daemon.context_pct() == 2.0
    daemon._session = _Session(None)
    assert daemon.context_pct() is None
