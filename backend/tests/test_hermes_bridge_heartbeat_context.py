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


# ── Bauplan Lauf 2 Teil 1c (2026-09-21): the bridge sends the turn ──────
#
# Today the heartbeat body never carries a `status` — the server default
# ("idle", routers/agents.py AgentHeartbeatPayload) means agent.status is
# stuck on "idle" even mid-turn, which makes Guard 3 (dispatch.py) blind
# for host agents. These tests drive `_heartbeat_body()` to report the
# ACP daemon's busy() state as `status`, only under the ACP driver.


class _FakeDaemon:
    def __init__(self, *, busy=None, raises=False):
        self._busy = busy
        self._raises = raises

    def busy(self):
        if self._raises:
            raise RuntimeError("acp daemon state unreachable")
        return self._busy


def test_heartbeat_body_reports_working_while_turn_runs(bridge, monkeypatch):
    """ACP driver + daemon.busy()=True → body carries status='working'."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=True))
    body = json.loads(bridge._heartbeat_body())
    assert body.get("status") == "working", body


def test_heartbeat_body_reports_idle_between_turns(bridge, monkeypatch):
    """ACP driver + daemon.busy()=False → body carries status='idle'."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=False))
    body = json.loads(bridge._heartbeat_body())
    assert body.get("status") == "idle", body


def test_heartbeat_body_omits_status_on_native_driver(bridge, monkeypatch):
    """Native (non-ACP) path has no turn concept — `status` key absent so
    the server default ('idle') applies, body unchanged from today."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: False)
    body = json.loads(bridge._heartbeat_body())
    assert "status" not in body, body


def test_heartbeat_body_survives_busy_raising(bridge, monkeypatch):
    """busy() throwing must never break the heartbeat — status omitted
    (never guessed), context_pct still comes through if scrapeable."""
    monkeypatch.setattr(
        bridge, "capture_pane",
        lambda: " ⚕ deepseek-v4-flash-0731 │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s",
    )
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(raises=True))
    body = json.loads(bridge._heartbeat_body())
    assert "status" not in body, body
    assert body.get("context_pct") == 8.0, body


def test_heartbeat_body_keeps_context_pct_with_status(bridge, monkeypatch):
    """Regression: the new `status` field must not crowd out `context_pct`
    — both belong in the same body when both are available."""
    monkeypatch.setattr(
        bridge, "capture_pane",
        lambda: " ⚕ deepseek-v4-flash-0731 │ 21.3K/262.1K │ [█░░░░░░░░░] 8% │ 12m │ ⏲ 48s",
    )
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=False))
    body = json.loads(bridge._heartbeat_body())
    assert body == {"context_pct": 8.0, "status": "idle"}


# ── Nacharbeit H2 (21.09.2026): task_id must be the CURRENT turn, not the ──
# previous one. Pruefbericht scratchpad/run2/07-pruefbericht.md: the old
# `_last_dispatched_task_id` is only assigned AFTER deliver_prompt(wait=True)
# returns, so while a turn is running (the whole point of the heartbeat
# reporting task_id) it still names the PREVIOUS task. `_turn_task_id` is the
# fix: set before daemon.prompt(), cleared the instant the caller's deliver
# returns AND self-healed in _heartbeat_body() itself whenever busy() says
# False, so a stale value can never outlive the turn it named.


def test_heartbeat_body_reports_current_turn_task_id_not_previous(bridge, monkeypatch):
    """Mid-turn: _turn_task_id (current) must win over _last_dispatched_task_id
    (the card before this one, still set from the prior dispatch)."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=True))
    monkeypatch.setattr(bridge, "_last_dispatched_task_id", "previous-task-id")
    monkeypatch.setattr(bridge, "_turn_task_id", "current-turn-task-id")
    body = json.loads(bridge._heartbeat_body())
    assert body.get("task_id") == "current-turn-task-id", body


def test_heartbeat_body_omits_task_id_when_no_turn_in_flight(bridge, monkeypatch):
    """Between turns (busy=True but no dispatch loop set _turn_task_id yet,
    e.g. an operator-typed chat message) — no task_id guessed."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=True))
    monkeypatch.setattr(bridge, "_turn_task_id", None)
    body = json.loads(bridge._heartbeat_body())
    assert "task_id" not in body, body


def test_heartbeat_body_self_heals_stale_turn_task_id_after_turn_ends(bridge, monkeypatch):
    """Self-heal: busy()=False means the turn is over — any leftover
    _turn_task_id (e.g. the dispatch thread crashed between prompt()
    returning and its own `finally` clear) must be dropped from the body
    AND from the module state itself, not just this one heartbeat."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _FakeDaemon(busy=False))
    bridge._turn_task_id = "leftover-from-a-crashed-thread"
    body = json.loads(bridge._heartbeat_body())
    assert "task_id" not in body, body
    assert bridge._turn_task_id is None, (
        "stale _turn_task_id must be self-healed, not just hidden from this body"
    )


def test_heartbeat_body_after_restart_reports_idle_explicitly(bridge, monkeypatch):
    """Bridge-Neustart (Nacharbeit-Frage): the DB can still say
    agent.status=='working' from before a crash. A real, freshly-built
    ChatDaemon (session=None, never started — exactly the state right after
    a restart, no monkeypatched busy()) must report status='idle' EXPLICITLY
    in the body (not omit the key), so the server's self-heal (agents.py
    Bug 18) actually gets a chance to correct the stale DB value on the very
    first heartbeat."""
    monkeypatch.setattr(bridge, "capture_pane", lambda: "│ ctx -- │ [░░░░░░░░░░] -- │")
    monkeypatch.setattr(bridge, "driver_is_acp", lambda: True)
    fresh_daemon = bridge.hermes_acp_chat.ChatDaemon(lambda: None)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: fresh_daemon)
    monkeypatch.setattr(bridge, "_turn_task_id", None)
    body = json.loads(bridge._heartbeat_body())
    assert body.get("status") == "idle", body
    assert "task_id" not in body, body
