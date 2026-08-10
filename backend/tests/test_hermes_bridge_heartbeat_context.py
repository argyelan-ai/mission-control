"""CTX-01 Nachzug Teil 2 (2026-08-10): tests for scripts/hermes-bridge.py's
_heartbeat_body() — the context_pct scrape that now rides along on the
existing Hermes heartbeat loop (previously always POSTed an empty body).

Loader mirrors test_hermes_bridge.py's `bridge` fixture (hyphenated filename,
loaded via importlib rather than a normal import).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("hermes_bridge_ctx", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge():
    return _load_bridge()


def test_heartbeat_body_includes_context_pct_from_real_statusline(bridge, monkeypatch):
    """Real Hermes statusline (8% bar, from the CTX-01 live capture)."""
    monkeypatch.setattr(
        bridge, "capture_pane",
        lambda: " ⚕ deepseek-v4-flash-0731 │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s",
    )
    body = json.loads(bridge._heartbeat_body())
    assert body == {"context_pct": 8.0}


def test_heartbeat_body_omits_context_pct_when_no_value(bridge, monkeypatch):
    """Fresh session (`ctx --`) must NOT report 0 — field omitted entirely,
    body degrades to the pre-fix empty `{}`."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    body = json.loads(bridge._heartbeat_body())
    assert body == {}


def test_heartbeat_body_survives_capture_pane_raising(bridge, monkeypatch):
    """Best-effort contract: a broken pane-capture must never break the
    heartbeat — body degrades to the old empty payload, no exception escapes."""
    def _boom():
        raise RuntimeError("tmux is on fire")
    monkeypatch.setattr(bridge, "capture_pane", _boom)
    body = json.loads(bridge._heartbeat_body())
    assert body == {}


def test_heartbeat_body_genuine_zero_percent_is_reported(bridge, monkeypatch):
    """A real 0% must come through as 0.0, not be conflated with 'no value'."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ 0/1M │ [░░░░░░░░░░] 0% │")
    body = json.loads(bridge._heartbeat_body())
    assert body == {"context_pct": 0.0}
