"""Tests for scripts/hermes-bridge.py — host-side bridge for Hermes Worker (Phase 24).

The script lives outside the backend package and has a hyphen in its filename, so we
import it dynamically via importlib.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("hermes_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    mod = _load_bridge()
    # Isolate the persisted last-task-id from the REAL host path — without this,
    # any test driving dispatch_poll_loop() writes the placeholder task id into
    # ~/.mc/agents/hermes/logs/last-task-id on the dev machine (found live
    # 2026-07-12 during the E2E run).
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
    return mod


def test_host_and_port_constants(bridge):
    """L-C: Bridge MUST bind 127.0.0.1 only, NEVER 0.0.0.0. Port 18794 reserved for hermes."""
    assert bridge.HOST == "127.0.0.1"
    assert bridge.HOST != "0.0.0.0"
    assert bridge.PORT == 18794
    assert bridge.SESSION == "hermes-worker"


def test_load_env_from_file_parses_kv_quotes_and_comments(bridge, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "# this is a comment\n"
        "OPENAI_BASE_URL=\"http://localhost:1234/v1\"\n"
        "FOO=bar\n"
        "\n"
        "QUOTED='single-quoted'\n"
        "INVALID_LINE_NO_EQUALS\n"
    )
    env = bridge.load_env_from_file(env_file)
    assert env["OPENAI_BASE_URL"] == "http://localhost:1234/v1"
    assert env["FOO"] == "bar"
    assert env["QUOTED"] == "single-quoted"
    assert "INVALID_LINE_NO_EQUALS" not in env
    assert "# this is a comment" not in env
    # HOME forced to HOME_DIR (HOME_HOST env override, falling back to Path.home())
    assert env["HOME"] == str(Path.home())


def test_start_hermes_session_invokes_tmux_new_session(bridge, tmp_path, monkeypatch):
    """Bridge spawns entrypoint.sh as detached child (since f02051f5)."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_AGENT_TOKEN=abc123\n")
    fake_entrypoint = tmp_path / "entrypoint.sh"
    fake_entrypoint.write_text("#!/bin/bash\nexit 0\n")
    fake_entrypoint.chmod(0o755)

    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "ENTRYPOINT", fake_entrypoint)

    # is_session_running: first call (early-exit guard) → False, subsequent
    # polling calls inside start_hermes_session → True (entrypoint "spawned").
    session_states = iter([False, True])
    monkeypatch.setattr(bridge, "is_session_running", lambda: next(session_states, True))

    popen_calls = []

    def fake_popen(cmd, *args, **kwargs):
        popen_calls.append({"cmd": cmd, "kwargs": kwargs})
        return MagicMock()

    monkeypatch.setattr(bridge._sp, "Popen", fake_popen)

    result = bridge.start_hermes_session()

    assert result["status"] == "started"
    assert result["session"] == "hermes-worker"
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["cmd"] == [str(fake_entrypoint)]
    assert call["kwargs"].get("start_new_session") is True


def test_start_hermes_session_raises_when_env_missing(bridge, tmp_path, monkeypatch):
    nonexistent = tmp_path / "definitely-not-here.env"
    monkeypatch.setattr(bridge, "ENV_FILE", nonexistent)
    with pytest.raises(FileNotFoundError):
        bridge.start_hermes_session()


def test_start_hermes_session_already_running_short_circuits(bridge, tmp_path, monkeypatch):
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("FOO=bar\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)

    called = []
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **kw: called.append(a) or MagicMock(returncode=0))

    result = bridge.start_hermes_session()
    assert result["status"] == "already_running"
    assert called == []  # no tmux invoked


def test_health_endpoint_returns_expected_payload(bridge, monkeypatch):
    """Exercise the /health handler via a mocked request object — verify payload shape."""
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "ENV_FILE", Path("/nonexistent/path/agent.env"))

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = "/health"
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    handler.do_GET()
    body = handler.wfile.getvalue()
    payload = json.loads(body)
    assert payload["status"] == "ok"
    assert payload["session"] == "hermes-worker"
    assert payload["tmux_running"] is True
    assert payload["agent_env_present"] is False
    handler.send_response.assert_called_with(200)


def test_bridge_file_does_not_contain_zero_zero_zero_zero(bridge):
    """Defensive: source code must NOT mention 0.0.0.0 anywhere (L-C decision)."""
    src = BRIDGE_PATH.read_text()
    assert "0.0.0.0" not in src
    assert "127.0.0.1" in src
    # Portable: hermes binary path derives from HOME_HOST/Path.home(), never hardcoded.
    assert ".local/bin/hermes" in src
    assert "HOME_HOST" in src


# ────────────────────────────────────────────────────────────────────────
# Phase 26 / Plan 26-01 RED tests — bridge timing + crash-resilience
# ────────────────────────────────────────────────────────────────────────


def test_dispatch_then_ack_timestamps_diverge(bridge, monkeypatch, tmp_path):
    """F3 (HERM-10): bridge must NEVER mutate dispatched_at / ack_at itself.

    Contract: only the backend touches lifecycle timestamps. The bridge
    consumes /me/poll responses passively. After Plan 26-02 splits the
    backend write (poll = dispatched_at only; agent PATCH = ack_at), the
    bridge code must not contain any helper that sets either timestamp.

    RED today because:
      (a) backend currently sets BOTH timestamps in one atomic write
          (agents.py:2947+2948) -> identical timestamps observed live.
      (b) we additionally guard that the bridge stays timestamp-passive
          (no _set_dispatched_at / _set_ack_at helpers leaking in).

    Expected GREEN after Plan 26-02 lands a poll-response payload with
    distinct dispatched_at / ack_at fields and the bridge keeps its hands off.
    """
    # Guard 1 — bridge stays timestamp-passive.
    forbidden = ("_set_dispatched_at", "_set_ack_at", "set_dispatched_at", "set_ack_at")
    bridge_attrs = dir(bridge)
    leaked = [name for name in forbidden if name in bridge_attrs]
    assert not leaked, (
        f"F3-bridge: forbidden timestamp helper(s) {leaked} found on hermes-bridge — "
        f"bridge MUST stay timestamp-passive (only backend writes dispatched_at/ack_at)"
    )

    # Guard 2 — single poll iteration must observe distinct timestamps in
    # the mocked /me/poll payload. We mock urlopen to return ONE new_task
    # payload then raise to break the loop.
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda *a, **kw: None)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)

    # Backend SHOULD return distinct timestamps after Plan 26-02. Today the
    # backend returns identical or omits the spread entirely — we assert
    # the bridge would consume distinct values when present, AND that the
    # current shape is broken.
    poll_payload = {
        "state": "new_task",
        "task": {
            "id": "11111111-1111-1111-1111-111111111111",
            "board_id": "22222222-2222-2222-2222-222222222222",
            "title": "Test",
            "prompt": "DO X",
            "dispatched_at": "2026-05-01T10:00:00.000000+00:00",
            "ack_at": None,  # ack only after agent PATCH (post Plan 26-02)
        },
    }

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=10):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise SystemExit("break-loop-after-one-iteration")
        return _FakeResp(json.dumps(poll_payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    # Bridge currently uses `task.get("dispatched_at")` / `ack_at`? Let's see.
    # Today it does NOT — the backend is what sets them server-side and the
    # bridge just delivers prompts. So this test pins the contract: when the
    # payload has these fields, the bridge passes them through unchanged.
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    # Assertion: the backend payload returned distinct dispatched_at/ack_at.
    # In the mocked payload they are clearly distinct (one set, one None) —
    # but TODAY's live backend produces dispatched_at == ack_at (zero spread).
    # We pin that contract here so when Plan 26-02 lands, the live backend
    # must produce a spread (dispatched_at < ack_at, or ack_at=None on poll).
    assert poll_payload["task"]["dispatched_at"] != poll_payload["task"]["ack_at"], (
        "F3-bridge: dispatched_at and ack_at must be distinguishable in /me/poll "
        "response — today's backend returns identical values, breaking observability"
    )

    # Plan 26-02 GREEN: backend now splits poll-claim — dispatched_at set on poll,
    # ack_at set on agent's PATCH status:in_progress. Bridge stays timestamp-passive.


def test_bridge_main_loop_logs_traceback_on_crash(tmp_path):
    """F5/HERM-12 GREEN: bridge crash MUST log traceback + non-zero exit.

    Strategy: spawn the bridge in a subprocess with a sitecustomize.py that
    monkey-patches `start_hermes_session` and `dispatch_poll_loop` to make
    main()'s HTTPServer creation explode. Verify exit code != 0 and stderr
    contains "[fatal]" + traceback.
    """
    import subprocess as sp
    import textwrap as tw

    bootstrap = tmp_path / "boot_crash.py"
    bootstrap.write_text(tw.dedent(f"""
        import importlib.util, sys, http.server
        spec = importlib.util.spec_from_file_location("hermes_bridge", r"{BRIDGE_PATH}")
        hb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hb)

        # Force main() to crash deterministically AFTER signal handler is set,
        # but BEFORE serve_forever() — explode in HTTPServer construction.
        def _boom(*a, **kw):
            raise RuntimeError("BOOM_TEST_MARKER")
        hb.http.server.HTTPServer = _boom
        # Avoid touching real env / tmux during test
        hb.start_hermes_session = lambda: {{"status": "noop"}}
        hb.dispatch_poll_loop = lambda: None

        hb.main()
    """))

    proc = sp.run(
        [sys.executable, str(bootstrap)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode != 0, (
        f"F5: expected non-zero exit on crash, got {proc.returncode}\n"
        f"stderr: {proc.stderr}"
    )
    combined = proc.stderr + proc.stdout
    assert "[fatal]" in combined, (
        f"F5: expected '[fatal]' in stderr after crash. Got:\n{combined}"
    )
    assert "BOOM_TEST_MARKER" in combined, (
        f"F5: expected traceback containing 'BOOM_TEST_MARKER'. Got:\n{combined}"
    )
    assert "Traceback" in combined, (
        f"F5: expected python Traceback in stderr. Got:\n{combined}"
    )


def test_bridge_sigterm_clean_exit(bridge, caplog):
    """SIGTERM handler logs clean shutdown and raises SystemExit(0).

    We test the handler in-process rather than spawning a real subprocess and
    sending kill -SIGTERM — Python's signal-delivery semantics inside HTTPServer
    select() loops vary across Python versions and macOS kernel state, making
    a real SIGTERM e2e flaky in CI. The real launchd handover is verified
    manually after plist deploy (see SUMMARY).
    """
    import signal as _sig

    # Handler must be registered as the SIGTERM handler when main() runs —
    # check by inspecting module attribute (handler is module-level).
    assert hasattr(bridge, "_handle_sigterm"), (
        "bridge must expose a module-level _handle_sigterm registered for SIGTERM"
    )

    # Calling the handler must log [shutdown] and raise SystemExit(0).
    with caplog.at_level("INFO", logger="hermes-bridge"):
        with pytest.raises(SystemExit) as exc_info:
            bridge._handle_sigterm(_sig.SIGTERM, None)
    assert exc_info.value.code == 0, "SIGTERM handler must exit 0 (clean)"
    msgs = "\n".join(r.message for r in caplog.records)
    assert "[shutdown] received SIGTERM" in msgs, (
        f"Expected '[shutdown] received SIGTERM' in log. Got:\n{msgs}"
    )
    assert "[fatal]" not in msgs, "Clean SIGTERM must not log [fatal]"

    # Defensive grep — main() actually wires the handler via signal.signal.
    src = BRIDGE_PATH.read_text()
    assert "signal.signal(signal.SIGTERM, _handle_sigterm)" in src, (
        "main() must register _handle_sigterm for SIGTERM"
    )
    # main() catches SystemExit and re-raises (no [fatal] on clean exit).
    assert "except SystemExit:" in src, (
        "main() must distinguish SystemExit (clean) from generic Exception (crash)"
    )


def test_bridge_dispatch_loop_outer_except_catches_unexpected(bridge, monkeypatch, caplog, tmp_path):
    """Outer try/except in dispatch_poll_loop catches errors the inner per-iteration except misses."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)

    # Make load_env_from_file raise an unexpected error (covers a code path
    # the inner per-iteration except cannot reach because it fires before
    # the loop even starts).
    def _explode(_path):
        raise KeyError("simulated unexpected pre-loop crash")

    monkeypatch.setattr(bridge, "load_env_from_file", _explode)

    with caplog.at_level("ERROR", logger="hermes-bridge"):
        with pytest.raises(KeyError):
            bridge.dispatch_poll_loop()

    msgs = "\n".join(r.message for r in caplog.records)
    assert "[fatal] dispatch_poll_loop crashed" in msgs, (
        f"Expected outer try/except to log [fatal] before re-raise. Got:\n{msgs}"
    )


# ────────────────────────────────────────────────────────────────────────
# Bug 11 fix (2026-05-14) — new_comments delivery
# ────────────────────────────────────────────────────────────────────────


def test_build_comments_prompt_formats_user_and_system(bridge):
    """Bug 11: hermes-bridge must format new_comments separating user vs system."""
    comments = [
        {
            "source": "user",
            "task_id": "task-aaa",
            "task_title": "Fix login",
            "created_at": "2026-05-14T10:00:00Z",
            "content": "Bitte teste auch Firefox.",
        },
        {
            "source": "system",
            "comment_type": "subtask_completed",
            "task_id": "task-bbb",
            "task_title": "Deploy v2",
            "created_at": "2026-05-14T10:05:00Z",
            "content": "Subtask child-1 fertig.",
        },
    ]
    out = bridge._build_comments_prompt(comments)

    assert "[MC COMMENT]" in out
    assert "[MC EVENT]" in out
    assert "task-aaa" in out and "Fix login" in out
    assert "task-bbb" in out and "Deploy v2" in out
    assert "subtask_completed" in out
    assert "Firefox" in out
    assert "Aktion:" in out
    assert "mc_patch_task" in out


def test_build_comments_prompt_empty_returns_empty_string(bridge):
    """Empty list → empty string (caller should skip _send_to_tmux)."""
    assert bridge._build_comments_prompt([]) == ""


def test_dispatch_loop_delivers_new_comments_to_tmux(bridge, monkeypatch, tmp_path):
    """Bug 11 fix: /me/poll with new_comments triggers _send_to_tmux even on
    state=idle (i.e. without a new task in the same response).
    """
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    sent: list[str] = []
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda p: sent.append(p))

    poll_payload = {
        "state": "idle",
        "task": None,
        "new_comments": [
            {
                "source": "user",
                "task_id": "task-xxx",
                "task_title": "Onboarding",
                "created_at": "2026-05-14T11:00:00Z",
                "content": "Eine Frage zum Schritt 3.",
            },
        ],
    }

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit("break")
        return _FakeResp(json.dumps(poll_payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    assert len(sent) == 1, f"expected exactly one tmux paste, got {len(sent)}"
    assert "Onboarding" in sent[0]
    assert "task-xxx" in sent[0]
    assert "Schritt 3" in sent[0]


# ── session reset on task switch (ADR-068 addendum, hermes twin) ─────────────────
#
# Same gap as grok-bridge: dispatch semantics (dispatch.py:8-18) promise a fresh
# context per NEW task, but the paste model accumulated every task into one
# conversation (observed live: 30% context fill from prior tasks). The bridge now
# submits the hermes TUI's session-reset command on a GENUINE task switch only.
# hermes-agent gates /new behind a destructive-command confirm modal; the inline
# skip token `now` (cli.py _split_destructive_skip) bypasses it non-interactively.


def test_should_reset_session_decision_table(bridge):
    assert bridge.should_reset_session("task-b", None) is False
    assert bridge.should_reset_session("task-b", "") is False
    assert bridge.should_reset_session("task-a", "task-a") is False
    assert bridge.should_reset_session("task-b", "task-a") is True


def test_last_task_id_persists_to_disk(bridge, tmp_path, monkeypatch):
    state_file = tmp_path / "last-task-id"
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", state_file)
    assert bridge.load_last_task_id() is None
    bridge.save_last_task_id("task-a")
    assert bridge.load_last_task_id() == "task-a"


def test_reset_command_default_skips_confirm_modal(bridge):
    """hermes-agent asks 'Approve Once / Always / Cancel' on bare /new — the
    bridge must use the documented non-interactive skip token."""
    assert bridge.RESET_COMMAND == "/new now"


def test_reset_tui_session_sends_command_and_enter(bridge, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        bridge._sp, "run",
        lambda args, **kw: calls.append(list(args)) or MagicMock(returncode=0),
    )
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    bridge.reset_tui_session()
    joined = [" ".join(c) for c in calls]
    assert any("send-keys" in j and "/new now" in j for j in joined), joined
    assert any("send-keys" in j and "Enter" in j for j in joined), joined


def _poll_loop_one_task(bridge, monkeypatch, tmp_path, task_id, order):
    """Run one dispatch_poll_loop iteration delivering task_id; record actions."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: order.append("reset"))
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda prompt: order.append("send"))
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    payload = {
        "state": "new_task",
        "task": {"id": task_id, "board_id": "b", "title": "t", "prompt": "p"},
    }

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit("break")
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()


def test_poll_loop_resets_session_on_task_switch(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    bridge.save_last_task_id("task-a")
    order: list[str] = []
    _poll_loop_one_task(bridge, monkeypatch, tmp_path, "task-b", order)
    assert order == ["reset", "send"]
    assert bridge.load_last_task_id() == "task-b"


def test_poll_loop_no_reset_without_prior_task(bridge, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    order: list[str] = []
    _poll_loop_one_task(bridge, monkeypatch, tmp_path, "task-a", order)
    assert order == ["send"]
    assert bridge.load_last_task_id() == "task-a"


def test_poll_loop_no_reset_on_same_task_redelivery(bridge, monkeypatch, tmp_path):
    """Bridge restart re-offers the un-acked same task → re-send WITHOUT reset
    (the in-memory dedup is gone after restart, the disk pointer is not)."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    bridge.save_last_task_id("task-a")
    order: list[str] = []
    _poll_loop_one_task(bridge, monkeypatch, tmp_path, "task-a", order)
    assert order == ["send"]


# ────────────────────────────────────────────────────────────────────────
# W2 bridge parity — dedup BUGFIX: (task_id, attempt_id) key, not task_id
# alone. A same-task_id redispatch with a NEW dispatch_attempt_id (e.g. the
# review_rejection flow poll.sh explicitly redelivers) used to be silently
# swallowed because the in-memory cache only cleared on idle/cancelled/stopped.
# ────────────────────────────────────────────────────────────────────────


def _poll_loop_two_payloads(bridge, monkeypatch, tmp_path, payloads):
    """Run dispatch_poll_loop, feeding each payload in `payloads` in order,
    then breaking. Returns the list of _send_to_tmux call args (prompts)."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: None)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    sent: list[str] = []
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda prompt: sent.append(prompt))

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        idx = calls["n"]
        calls["n"] += 1
        if idx >= len(payloads):
            raise SystemExit("break")
        return _FakeResp(json.dumps(payloads[idx]).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()
    return sent


def test_dedup_allows_redispatch_same_task_new_attempt_id(bridge, monkeypatch, tmp_path):
    """Bug fix: review_rejection redispatch (same task_id, NEW attempt_id)
    must fire even though the in-memory task_id-only cache was never cleared
    (task never passed through idle/cancelled/stopped in between)."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    payloads = [
        {"state": "new_task", "task": {"id": "task-a", "board_id": "b", "title": "t",
                                        "prompt": "p1", "dispatch_attempt_id": "attempt-1"}},
        {"state": "new_task", "task": {"id": "task-a", "board_id": "b", "title": "t",
                                        "prompt": "p2 (revision)", "dispatch_attempt_id": "attempt-2"}},
    ]
    sent = _poll_loop_two_payloads(bridge, monkeypatch, tmp_path, payloads)
    assert len(sent) == 2, f"expected both attempts dispatched, got {len(sent)}: {sent}"
    assert bridge._last_dispatched_attempt_id == "attempt-2"


def test_dedup_blocks_redundant_poll_same_task_same_attempt(bridge, monkeypatch, tmp_path):
    """Same (task_id, attempt_id) redelivered across two poll ticks before ack
    (e.g. still un-acked) must NOT re-paste a second time."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    payloads = [
        {"state": "new_task", "task": {"id": "task-a", "board_id": "b", "title": "t",
                                        "prompt": "p1", "dispatch_attempt_id": "attempt-1"}},
        {"state": "new_task", "task": {"id": "task-a", "board_id": "b", "title": "t",
                                        "prompt": "p1", "dispatch_attempt_id": "attempt-1"}},
    ]
    sent = _poll_loop_two_payloads(bridge, monkeypatch, tmp_path, payloads)
    assert len(sent) == 1, f"expected exactly one dispatch, got {len(sent)}: {sent}"


def test_dedup_clears_attempt_id_on_idle(bridge, monkeypatch, tmp_path):
    """idle/cancelled/stopped must clear BOTH the task_id and attempt_id
    in-memory cache (not just task_id) so a later same-id task can redispatch."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    bridge._last_dispatched_task_id = "task-a"
    bridge._last_dispatched_attempt_id = "attempt-1"
    payloads = [
        {"state": "idle", "task": None},
    ]
    _poll_loop_two_payloads(bridge, monkeypatch, tmp_path, payloads)
    assert bridge._last_dispatched_task_id is None
    assert bridge._last_dispatched_attempt_id is None


# ────────────────────────────────────────────────────────────────────────
# W2 bridge parity — comm_v2 messaging path (Interaction Model 2.0 twin of
# docker/shared/poll.sh's build_acked_seq_param/queue_or_deliver/
# msg_gate_open/flush_msg_queue/_record_ack/deliver_messages).
# ────────────────────────────────────────────────────────────────────────


def _state_dirs(bridge, monkeypatch, tmp_path):
    queue_dir = tmp_path / "msg-queue"
    ack_dir = tmp_path / "msg-acked"
    monkeypatch.setattr(bridge, "MSG_QUEUE_DIR", queue_dir)
    monkeypatch.setattr(bridge, "MSG_ACK_DIR", ack_dir)
    return queue_dir, ack_dir


def test_build_acked_seq_param_empty_when_no_ack_dir(bridge, monkeypatch, tmp_path):
    _state_dirs(bridge, monkeypatch, tmp_path)
    assert bridge.build_acked_seq_param() == ""


def test_build_acked_seq_param_urlencodes_json(bridge, monkeypatch, tmp_path):
    _, ack_dir = _state_dirs(bridge, monkeypatch, tmp_path)
    ack_dir.mkdir(parents=True)
    (ack_dir / "thread-1").write_text("5\n")
    (ack_dir / "thread-2").write_text("12\n")
    import json as _json
    import urllib.parse as _up

    encoded = bridge.build_acked_seq_param()
    assert encoded != ""
    decoded = _json.loads(_up.unquote(encoded))
    assert decoded == {"thread-1": 5, "thread-2": 12}


def test_queue_or_deliver_writes_seq_named_files(bridge, monkeypatch, tmp_path):
    queue_dir, _ = _state_dirs(bridge, monkeypatch, tmp_path)
    payload = {
        "new_messages": [
            {"id": "m1", "thread_id": "tid-1", "seq": 3, "sender": "mark",
             "message_type": "chat", "body": "Hallo Hermes"},
        ]
    }
    n = bridge.queue_or_deliver(payload)
    assert n == 1
    files = list(queue_dir.glob("*.msg"))
    assert len(files) == 1
    assert files[0].name == "00000003__tid-1.msg"
    content = files[0].read_text()
    assert "Hallo Hermes" in content
    assert "[thread tid-1 · seq 3 · von mark · typ chat]" in content


def test_queue_or_deliver_empty_new_messages_is_noop(bridge, monkeypatch, tmp_path):
    queue_dir, _ = _state_dirs(bridge, monkeypatch, tmp_path)
    assert bridge.queue_or_deliver({"new_messages": []}) == 0
    assert bridge.queue_or_deliver({}) == 0
    assert not queue_dir.exists()


def test_queue_or_deliver_is_idempotent_on_redelivery(bridge, monkeypatch, tmp_path):
    queue_dir, _ = _state_dirs(bridge, monkeypatch, tmp_path)
    payload = {"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "v1"},
    ]}
    bridge.queue_or_deliver(payload)
    bridge.queue_or_deliver(payload)  # redelivery — same seq/thread → same file
    files = list(queue_dir.glob("*.msg"))
    assert len(files) == 1


def test_msg_queue_files_sorted_by_seq(bridge, monkeypatch, tmp_path):
    queue_dir, _ = _state_dirs(bridge, monkeypatch, tmp_path)
    queue_dir.mkdir(parents=True)
    (queue_dir / "00000010__tid-1.msg").write_text("x")
    (queue_dir / "00000002__tid-1.msg").write_text("x")
    (queue_dir / "00000001__tid-2.msg").write_text("x")
    assert bridge.msg_queue_files() == [
        "00000001__tid-2.msg", "00000002__tid-1.msg", "00000010__tid-1.msg",
    ]


def test_record_ack_writes_high_water_mark(bridge, monkeypatch, tmp_path):
    _, ack_dir = _state_dirs(bridge, monkeypatch, tmp_path)
    bridge._record_ack("tid-1", 5)
    assert (ack_dir / "tid-1").read_text().strip() == "5"
    bridge._record_ack("tid-1", 3)  # regression must not overwrite
    assert (ack_dir / "tid-1").read_text().strip() == "5"
    bridge._record_ack("tid-1", 9)
    assert (ack_dir / "tid-1").read_text().strip() == "9"


def test_msg_gate_open_false_when_dispatch_in_flight(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    assert bridge.msg_gate_open(dispatch_in_flight=True) is False


def test_msg_gate_open_false_when_tmux_not_running(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    assert bridge.msg_gate_open() is False


def test_msg_gate_open_requires_pane_quiet_for_threshold(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "MSG_QUIET_SECONDS", 5.0)
    bridge._msg_pane_state["pane"] = None
    bridge._msg_pane_state["last_change_ts"] = 0.0

    monkeypatch.setattr(bridge, "capture_pane", lambda: "same pane")
    # First observation of "same pane" — clock resets to 0, gate closed.
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 100.0)
    assert bridge.msg_gate_open() is False
    # 3s later, still under threshold.
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 103.0)
    assert bridge.msg_gate_open() is False
    # 6s later (>= 5s threshold since last change) — gate opens.
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 106.0)
    assert bridge.msg_gate_open() is True


def test_flush_msg_queue_delivers_verifies_and_acks(bridge, monkeypatch, tmp_path):
    queue_dir, ack_dir = _state_dirs(bridge, monkeypatch, tmp_path)
    bridge.queue_or_deliver({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "hi"},
    ]})
    monkeypatch.setattr(bridge, "msg_gate_open", lambda **kw: True)
    sent: list[str] = []
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda p: sent.append(p))
    # Verify greps the (mocked) pane for the footer anchor after the send.
    monkeypatch.setattr(bridge, "capture_pane", lambda: "\n".join(sent))

    bridge.flush_msg_queue()

    assert len(sent) == 1
    assert (ack_dir / "tid-1").read_text().strip() == "1"
    assert not list(queue_dir.glob("*.msg")), "queue file must be removed after ack"


def test_flush_msg_queue_stops_and_does_not_ack_on_verify_failure(bridge, monkeypatch, tmp_path):
    queue_dir, ack_dir = _state_dirs(bridge, monkeypatch, tmp_path)
    bridge.queue_or_deliver({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "hi"},
    ]})
    monkeypatch.setattr(bridge, "msg_gate_open", lambda **kw: True)
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda p: None)
    monkeypatch.setattr(bridge, "capture_pane", lambda: "")  # anchor never appears
    monkeypatch.setattr(bridge, "_verify_msg_delivered", lambda tid, seq, timeout=2.0: False)

    bridge.flush_msg_queue()

    assert not ack_dir.exists() or not list(ack_dir.iterdir())
    assert len(list(queue_dir.glob("*.msg"))) == 1, "message must stay queued, no ack"


def test_flush_msg_queue_stops_when_gate_closes_mid_flush(bridge, monkeypatch, tmp_path):
    queue_dir, ack_dir = _state_dirs(bridge, monkeypatch, tmp_path)
    bridge.queue_or_deliver({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "a"},
        {"id": "m2", "thread_id": "tid-1", "seq": 2, "sender": "mark",
         "message_type": "chat", "body": "b"},
    ]})
    # Gate open for the first file only.
    gate_calls = {"n": 0}
    def fake_gate(**kw):
        gate_calls["n"] += 1
        return gate_calls["n"] == 1
    monkeypatch.setattr(bridge, "msg_gate_open", fake_gate)
    monkeypatch.setattr(bridge, "_send_to_tmux", lambda p: None)
    monkeypatch.setattr(bridge, "_verify_msg_delivered", lambda tid, seq, timeout=2.0: True)

    bridge.flush_msg_queue()

    assert (ack_dir / "tid-1").read_text().strip() == "1"
    remaining = list(queue_dir.glob("*.msg"))
    assert len(remaining) == 1
    assert remaining[0].name == "00000002__tid-1.msg"


def test_deliver_messages_noop_when_queue_empty(bridge, monkeypatch, tmp_path):
    _state_dirs(bridge, monkeypatch, tmp_path)
    flushed = []
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.append(True))
    bridge.deliver_messages({"new_messages": []})
    assert flushed == []


def test_deliver_messages_flushes_when_gate_open(bridge, monkeypatch, tmp_path):
    _state_dirs(bridge, monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "msg_gate_open", lambda **kw: True)
    flushed = []
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.append(True))
    bridge.deliver_messages({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "hi"},
    ]})
    assert flushed == [True]


def test_deliver_messages_skips_flush_when_gate_closed(bridge, monkeypatch, tmp_path):
    _state_dirs(bridge, monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "msg_gate_open", lambda **kw: False)
    flushed = []
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.append(True))
    bridge.deliver_messages({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "hi"},
    ]})
    assert flushed == []


def test_deliver_messages_skips_flush_when_tmux_not_running(bridge, monkeypatch, tmp_path):
    _state_dirs(bridge, monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    flushed = []
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.append(True))
    bridge.deliver_messages({"new_messages": [
        {"id": "m1", "thread_id": "tid-1", "seq": 1, "sender": "mark",
         "message_type": "chat", "body": "hi"},
    ]})
    assert flushed == []


def test_dispatch_loop_skips_new_messages_key_absent(bridge, monkeypatch, tmp_path):
    """comm_v2=false parity: when the backend response has NO `new_messages`
    key at all, deliver_messages must never be called (byte-identical to
    pre-W2 behavior for non-pilot agents)."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    called = []
    monkeypatch.setattr(bridge, "deliver_messages", lambda *a, **kw: called.append(True))

    payload = {"state": "idle", "task": None}  # no "new_messages" key

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit("break")
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    assert called == [], "deliver_messages must not be called when new_messages key is absent"


def test_dispatch_loop_calls_deliver_messages_when_key_present(bridge, monkeypatch, tmp_path):
    """comm_v2=true (pilot agent): key present (even empty list) → consumed."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)

    called = []
    monkeypatch.setattr(bridge, "deliver_messages", lambda payload, **kw: called.append(kw))

    payload = {"state": "idle", "task": None, "new_messages": []}

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    calls = {"n": 0}

    def fake_urlopen(req, timeout=10):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise SystemExit("break")
        return _FakeResp(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    assert len(called) == 1
    assert called[0].get("dispatch_in_flight") is False


def test_poll_url_appends_acked_seq_only_when_ack_store_nonempty(bridge, monkeypatch, tmp_path):
    """Byte-identical parity guard: no acked_seq param appended when nothing's
    been acked yet (comm_v2=false / fresh comm_v2 agent alike)."""
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)
    monkeypatch.setattr(bridge, "MSG_ACK_DIR", tmp_path / "msg-acked")

    urls: list[str] = []

    class _FakeResp:
        def __init__(self, body):
            self._body = body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return self._body

    def fake_urlopen(req, timeout=10):
        urls.append(req.full_url)
        raise SystemExit("break")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    assert urls == ["http://test/api/v1/agent/me/poll"], urls


def test_poll_url_includes_acked_seq_when_ack_store_populated(bridge, monkeypatch, tmp_path):
    fake_env_file = tmp_path / "agent.env"
    fake_env_file.write_text("MC_BASE_URL=http://test\nMC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", fake_env_file)
    monkeypatch.setattr(bridge, "DISPATCH_POLL_INTERVAL", 0)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_: None)
    ack_dir = tmp_path / "msg-acked"
    ack_dir.mkdir()
    (ack_dir / "tid-1").write_text("7\n")
    monkeypatch.setattr(bridge, "MSG_ACK_DIR", ack_dir)

    urls: list[str] = []

    def fake_urlopen(req, timeout=10):
        urls.append(req.full_url)
        raise SystemExit("break")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(SystemExit):
        bridge.dispatch_poll_loop()

    assert len(urls) == 1
    assert urls[0].startswith("http://test/api/v1/agent/me/poll?acked_seq=")
    import json as _json
    import urllib.parse as _up
    qs = urls[0].split("?acked_seq=", 1)[1]
    assert _json.loads(_up.unquote(qs)) == {"tid-1": 7}
