"""CTX-01 Nachzug Teil 2 (2026-08-10): tests for scripts/grok-bridge.py's
_heartbeat_body() — the context_pct scrape that now rides along on the
existing grok heartbeat loop (previously always POSTed an empty body).

grok's own statusline format is NOT live-verified (unlike Hermes/omp) — see
the task briefing — so _heartbeat_body() calls scrape_context_pct() with
harness=None, running the FULL pattern fallback instead of a specific,
unconfirmed regex. These tests exercise that fallback with statuslines from
OTHER known harnesses (proving the fallback still finds them) plus the
best-effort/no-value contracts shared with the other two bridges.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "grok-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("grok_bridge_ctx", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge():
    return _load_bridge()


def test_heartbeat_body_finds_context_via_fallback_no_harness_known(bridge, monkeypatch):
    """grok's format is unconfirmed, so harness=None — but a bar-percent
    statusline (Hermes-shaped, used here only as a stand-in fixture) is still
    found via the generic fallback chain."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "[█░░░░░░░░░] 8% │ some grok status")
    body = json.loads(bridge._heartbeat_body())
    assert body == {"context_pct": 8.0}


def test_heartbeat_body_omits_context_pct_when_nothing_recognized(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "capture_pane", lambda: "some unrecognized grok prompt, no numbers")
    body = json.loads(bridge._heartbeat_body())
    assert body == {}


def test_heartbeat_body_survives_capture_pane_raising(bridge, monkeypatch):
    def _boom():
        raise RuntimeError("tmux capture failed")
    monkeypatch.setattr(bridge, "capture_pane", _boom)
    body = json.loads(bridge._heartbeat_body())
    assert body == {}
