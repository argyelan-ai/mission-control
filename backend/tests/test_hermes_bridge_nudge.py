"""Tests for scripts/hermes-bridge.py MSG_DELIVERY_MODE=nudge (W2.1 nudge+pull).

Port of grok-bridge.py's deliver_messages_nudge contract (itself a port of
poll.sh) to the hermes bridge:
- per-thread high-water dedup (seq is only unique WITHIN a thread)
- immediate nudge on a new higher seq, remind after NUDGE_REMIND_SECONDS
- empty new_messages → state file removed (all fetched+acked via mc inbox)
- gate closed → deferred, state unchanged
- verify via the unique `(bis seq N, EPOCH)` token, submitted vs still
  sitting in the input line — hermes reuses _anchor_was_submitted (no
  composer-box border pattern like grok's `│`)
- the bridge NEVER acks in nudge mode (server cursor advances only through
  the agent's own `mc inbox` call)
- paste mode (default) stays byte-identical: deliver_messages_nudge untouched
- stale paste-mode queue files are cleared when nudge mode runs
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("hermes_bridge_nudge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["hermes_bridge_nudge"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    mod = _load_bridge()
    mod._last_dispatched_task_id = None
    mod._last_dispatched_attempt_id = None
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
    monkeypatch.setattr(mod, "NUDGE_STATE_FILE", tmp_path / "msg-nudge-state")
    monkeypatch.setattr(mod, "MSG_QUEUE_DIR", tmp_path / "msg-queue")
    monkeypatch.setattr(mod, "MSG_ACK_DIR", tmp_path / "msg-acked")
    monkeypatch.setattr(mod, "MSG_DELIVERY_MODE", "nudge")
    # Gate open by default; individual tests override.
    monkeypatch.setattr(mod, "msg_gate_open", lambda **kw: True)
    return mod


def _wire_paste(monkeypatch, bridge, *, echo_token=True):
    """Fake _send_to_tmux + capture_pane: after a nudge, the pane shows the
    nudge line as a submitted transcript line (echo_token=True, followed by
    a fresh prompt row so the anchor has scrolled off the trailing line) or
    shows nothing (echo_token=False → verify must fail). Returns the pasted
    texts."""
    pastes: list[str] = []

    def fake_send(text):
        pastes.append(text)

    def fake_capture():
        if pastes and echo_token:
            return f"transcript\n{pastes[-1]}\n> "
        return "transcript\n> "

    monkeypatch.setattr(bridge, "_send_to_tmux", fake_send)
    monkeypatch.setattr(bridge, "capture_pane", fake_capture)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    return pastes


def _msgs(*pairs):
    return [{"thread_id": t, "seq": s, "body": "x", "sender": "user",
             "message_type": "message"} for t, s in pairs]


# ── per-thread max ──────────────────────────────────────────────────────────────


def test_thread_seqs_per_thread_max_and_malformed_skipped(bridge):
    msgs = _msgs(("a", 1), ("a", 3), ("b", 2)) + [{"seq": "kaputt"}, {}]
    assert bridge._nudge_thread_seqs(msgs) == {"a": 3, "b": 2}


# ── first delivery + state write, no ack ────────────────────────────────────────


def test_first_messages_nudge_immediately_and_write_state(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    bridge.deliver_messages({"new_messages": _msgs(("a", 2), ("b", 5))})
    assert len(pastes) == 1
    assert "mc_inbox" in pastes[0] and "bis seq 5" in pastes[0]
    state = bridge._nudge_state_read()
    assert state["a"][0] == 2 and state["b"][0] == 5
    # NEVER acks locally in nudge mode.
    assert not bridge.MSG_ACK_DIR.exists()


def test_same_seqs_do_not_renudge_before_remind_window(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    payload = {"new_messages": _msgs(("a", 2))}
    bridge.deliver_messages(payload)
    bridge.deliver_messages(payload)  # server redelivers until agent acks
    assert len(pastes) == 1


def test_higher_seq_in_one_thread_renudges(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    bridge.deliver_messages({"new_messages": _msgs(("a", 2), ("a", 3))})
    assert len(pastes) == 2
    assert "bis seq 3" in pastes[1]


def test_remind_after_window_elapsed(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    # Backdate the state file's epoch beyond the remind window.
    tid, (seq, ts) = next(iter(bridge._nudge_state_read().items()))
    bridge.NUDGE_STATE_FILE.write_text(
        f"{tid} {seq} {int(ts - bridge.NUDGE_REMIND_SECONDS - 1)}\n", encoding="utf-8"
    )
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert len(pastes) == 2


def test_empty_new_messages_clears_state(bridge, monkeypatch):
    _wire_paste(monkeypatch, bridge)
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert bridge.NUDGE_STATE_FILE.exists()
    bridge.deliver_messages({"new_messages": []})
    assert not bridge.NUDGE_STATE_FILE.exists()


# ── gate / verify failure paths ─────────────────────────────────────────────────


def test_gate_closed_defers_and_keeps_state_clean(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    monkeypatch.setattr(bridge, "msg_gate_open", lambda **kw: False)
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert pastes == []
    assert not bridge.NUDGE_STATE_FILE.exists()  # retry next poll from scratch


def test_verify_fail_keeps_state_for_retry(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge, echo_token=False)
    monkeypatch.setattr(bridge.time, "monotonic", _fake_monotonic())
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert len(pastes) == 1
    assert not bridge.NUDGE_STATE_FILE.exists()
    # Next poll retries immediately (no state → seq 2 counts as new).
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert len(pastes) == 2


def _fake_monotonic():
    t = [0.0]

    def tick():
        t[0] += 1.0
        return t[0]

    return tick


def test_token_still_in_input_line_is_not_delivery(bridge):
    """hermes has no composer-box border like grok's `│` — the discriminator
    is whether the token has scrolled OFF the trailing non-blank line (see
    _anchor_was_submitted). Still sitting un-sent, it IS the last line."""
    token = "(bis seq 2, 1234)"
    pane_stuck = f"transcript\n📬 Neue Nachrichten {token} — lies sie jetzt mit dem Tool mc_inbox"
    pane_submitted = f"📬 Neue Nachrichten {token} — lies sie jetzt mit dem Tool mc_inbox\n> "
    assert bridge._nudge_token_visible(pane_stuck, token) is False
    assert bridge._nudge_token_visible(pane_submitted, token) is True


# ── mode isolation + cleanup ────────────────────────────────────────────────────


def test_paste_mode_default_never_calls_nudge(monkeypatch, tmp_path):
    mod = _load_bridge()
    monkeypatch.setattr(mod, "MSG_QUEUE_DIR", tmp_path / "q")
    monkeypatch.setattr(mod, "MSG_ACK_DIR", tmp_path / "a")
    assert mod.MSG_DELIVERY_MODE == "paste"  # env default
    called = MagicMock()
    monkeypatch.setattr(mod, "deliver_messages_nudge", called)
    monkeypatch.setattr(mod, "is_session_running", lambda: False)
    mod.deliver_messages({"new_messages": _msgs(("a", 1))})
    called.assert_not_called()
    # paste path queued the message as before
    assert len(list((tmp_path / "q").glob("*.msg"))) == 1


def test_stale_queue_files_cleared_in_nudge_mode(bridge, monkeypatch):
    _wire_paste(monkeypatch, bridge)
    bridge.MSG_QUEUE_DIR.mkdir(parents=True)
    (bridge.MSG_QUEUE_DIR / "00000001__x.msg").write_text("alt", encoding="utf-8")
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert list(bridge.MSG_QUEUE_DIR.glob("*.msg")) == []


def test_no_new_messages_field_is_noop(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    bridge.deliver_messages({})
    assert pastes == []


def test_corrupt_state_file_degrades_to_renudge(bridge, monkeypatch):
    pastes = _wire_paste(monkeypatch, bridge)
    bridge.NUDGE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    bridge.NUDGE_STATE_FILE.write_text("kaputt zeile\nx y\n", encoding="utf-8")
    bridge.deliver_messages({"new_messages": _msgs(("a", 2))})
    assert len(pastes) == 1
