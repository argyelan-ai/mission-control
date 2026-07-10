"""Tests for scripts/grok-bridge.py — host-side bridge for the Grok Build CLI (ADR-066).

The script lives outside the backend package and has a hyphen in its filename, so we
import it dynamically via importlib (same pattern as test_hermes_bridge.py).

Grok differs from Hermes: it is a HEADLESS per-dispatch subprocess emitting streaming
NDJSON, not a persistent tmux TUI. These tests pin the NDJSON reducer, the deterministic
stream→lifecycle mapping, the `grok` argv construction (incl. session continuity), the
mc-context.env 3-key contract, the health payload, and the SIGTERM/crash contracts.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "grok-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("grok_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Register in sys.modules BEFORE exec: the @dataclass decorator resolves the
    # `from __future__ import annotations` string annotations against
    # sys.modules[cls.__module__], which would be None otherwise.
    sys.modules["grok_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge():
    return _load_bridge()


# The exact event stream from the verified spike (2026-07-10).
GOLDEN_STREAM = "\n".join([
    json.dumps({"type": "thought", "data": "Let me plan the change."}),
    json.dumps({"type": "text", "data": "Editing the file"}),
    json.dumps({"type": "text", "data": " and running tests."}),
    json.dumps({"type": "end", "stopReason": "EndTurn",
                "sessionId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "requestId": "req-123"}),
]) + "\n"


# ── constants / security ────────────────────────────────────────────────────────


def test_host_and_port_constants(bridge):
    """Bridge MUST bind 127.0.0.1 only, never 0.0.0.0. Port 18795 reserved for grok."""
    assert bridge.HOST == "127.0.0.1"
    assert bridge.HOST != "0.0.0.0"
    assert bridge.PORT == 18795
    assert bridge.HARNESS == "grok"


def test_source_has_no_zero_zero_zero_zero(bridge):
    src = BRIDGE_PATH.read_text()
    assert "0.0.0.0" not in src
    assert "127.0.0.1" in src
    # streaming-json is the load-bearing output mode.
    assert "streaming-json" in src


# ── NDJSON reducer ───────────────────────────────────────────────────────────────


def test_reduce_golden_stream_endturn(bridge):
    outcome = bridge.GrokOutcome()
    events = bridge.iter_grok_events(StringIO(GOLDEN_STREAM), outcome)
    bridge.reduce_grok_stream(events, outcome)
    assert outcome.saw_end is True
    assert outcome.stop_reason == "EndTurn"
    assert outcome.session_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert outcome.final_text == "Editing the file and running tests."
    assert outcome.thought_chunks == 1
    assert outcome.text_chunks == 2
    assert outcome.parse_failures == 0


def test_iter_events_counts_malformed_without_raising(bridge):
    stream = StringIO(
        '{"type":"text","data":"ok"}\n'
        "this is not json\n"
        "\n"
        '{"type":"end","stopReason":"EndTurn"}\n'
    )
    outcome = bridge.GrokOutcome()
    events = list(bridge.iter_grok_events(stream, outcome))
    assert outcome.parse_failures == 1
    assert len(events) == 2  # text + end (blank + malformed skipped)


def test_reduce_captures_error_event(bridge):
    stream = StringIO(
        '{"type":"error","data":"rate limited"}\n'
        '{"type":"end","stopReason":"Error"}\n'
    )
    outcome = bridge.reduce_grok_stream(bridge.iter_grok_events(StringIO(stream.getvalue())))
    assert outcome.error_message == "rate limited"


# ── lifecycle mapping (deterministic, bridge-owned) ─────────────────────────────


def test_map_lifecycle_endturn_finishes(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="EndTurn", final_text="done", exit_code=0)
    action = bridge.map_lifecycle(o)
    assert action.action == "finish"
    assert action.reason == "end_turn"
    assert action.detail == "done"
    assert action.review is True


def test_map_lifecycle_no_review_when_board_disables(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="EndTurn", exit_code=0)
    action = bridge.map_lifecycle(o, board_requires_review=False)
    assert action.action == "finish"
    assert action.review is False


def test_map_lifecycle_watchdog_blocks(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="EndTurn", watchdog_killed=True, exit_code=-15)
    action = bridge.map_lifecycle(o)
    assert action.action == "blocked"
    assert action.reason == "watchdog"


def test_map_lifecycle_missing_end_blocks(bridge):
    o = bridge.GrokOutcome(saw_end=False, exit_code=0)
    action = bridge.map_lifecycle(o)
    assert action.action == "blocked"
    assert action.reason == "no_end"


def test_map_lifecycle_error_blocks(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="Error", error_message="boom", exit_code=1)
    action = bridge.map_lifecycle(o)
    assert action.action == "blocked"
    assert action.reason == "grok_error"
    assert "boom" in action.detail


def test_map_lifecycle_unclean_stop_blocks(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="MaxTurns", exit_code=0)
    action = bridge.map_lifecycle(o)
    assert action.action == "blocked"
    assert action.reason == "unclean_stop"


def test_map_lifecycle_nonzero_exit_blocks(bridge):
    o = bridge.GrokOutcome(saw_end=True, stop_reason="EndTurn", exit_code=3)
    action = bridge.map_lifecycle(o)
    assert action.action == "blocked"
    assert action.reason == "nonzero_exit"


# ── grok argv construction + session continuity ─────────────────────────────────


def test_build_grok_command_new_session(bridge):
    cmd = bridge.build_grok_command(
        prompt_file="/tmp/p.txt", workspace="/ws", session_id="11111111-2222-3333-4444-555555555555",
    )
    assert cmd[0] == bridge.GROK_BIN
    assert "--output-format" in cmd and "streaming-json" in cmd
    assert "--cwd" in cmd and "/ws" in cmd
    assert "--permission-mode" in cmd and "acceptEdits" in cmd
    assert "--prompt-file" in cmd and "/tmp/p.txt" in cmd
    # New session → -s <uuid>, never -r
    assert "-s" in cmd
    assert cmd[cmd.index("-s") + 1] == "11111111-2222-3333-4444-555555555555"
    assert "-r" not in cmd


def test_build_grok_command_resume_takes_precedence(bridge):
    cmd = bridge.build_grok_command(
        prompt_file="/tmp/p.txt", workspace="/ws",
        session_id="new-id", resume_session="resume-id",
    )
    # Resume wins → -r <id>, no -s
    assert "-r" in cmd
    assert cmd[cmd.index("-r") + 1] == "resume-id"
    assert "-s" not in cmd


# ── mc-context.env (3-key contract) ─────────────────────────────────────────────


def test_write_task_context_env_three_keys(bridge, tmp_path):
    path = tmp_path / "mc-context.env"
    ok = bridge.write_task_context_env(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1"}, path=str(path),
    )
    assert ok is True
    content = path.read_text()
    assert "TASK_ID=t1" in content
    assert "BOARD_ID=b1" in content
    assert "X_DISPATCH_ATTEMPT_ID=a1" in content


def test_write_task_context_env_missing_fields_blank(bridge, tmp_path):
    path = tmp_path / "mc-context.env"
    bridge.write_task_context_env({"id": "t1"}, path=str(path))
    content = path.read_text()
    assert "TASK_ID=t1" in content
    assert "BOARD_ID=\n" in content
    assert "X_DISPATCH_ATTEMPT_ID=\n" in content


# ── dispatch prompt ─────────────────────────────────────────────────────────────


def test_build_dispatch_prompt_has_ids_and_no_token(bridge):
    prompt = bridge.build_dispatch_prompt({
        "id": "task-123", "board_id": "board-9", "dispatch_attempt_id": "att-7",
        "title": "Fix login", "description": "Do the thing",
    })
    assert "task_id=task-123" in prompt
    assert "board_id=board-9" in prompt
    assert "attempt_id=att-7" in prompt
    assert "Fix login" in prompt
    assert "Do the thing" in prompt
    # Bridge owns the terminal transition — the agent must NOT self-finish.
    assert "Do NOT run" in prompt
    assert "mc deliverable" in prompt
    # SECURITY: never materialize the literal token.
    assert "MC_AGENT_TOKEN" not in prompt or "$MC_AGENT_TOKEN" in prompt


# ── env-file parsing (byte-identical to backend escaping) ───────────────────────


def test_load_env_from_file_unquotes(bridge, tmp_path):
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "# comment\n"
        "MC_AGENT_TOKEN='keepme'\n"
        "MC_BASE_URL='http://backend:8000'\n"
        "NO_EQUALS_LINE\n"
    )
    env = bridge.load_env_from_file(env_file)
    assert env["MC_AGENT_TOKEN"] == "keepme"
    assert env["MC_BASE_URL"] == "http://backend:8000"
    assert "NO_EQUALS_LINE" not in env


def test_unquote_env_value_roundtrip_quotes(bridge):
    # Mirror the backend's escaping and confirm the reader reverses it.
    assert bridge._unquote_env_value("'has'\"'\"'quote'") == "has'quote"
    assert bridge._unquote_env_value("'plain'") == "plain"


# ── HTTP control ─────────────────────────────────────────────────────────────────


def test_health_endpoint_payload(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "ENV_FILE", Path("/nonexistent/agent.env"))
    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = "/health"
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue())
    assert payload["status"] == "ok"
    assert payload["harness"] == "grok"
    assert payload["agent_env_present"] is False
    assert payload["dispatching"] is False
    handler.send_response.assert_called_with(200)


def test_restart_clears_session_cache(bridge):
    bridge._task_sessions["t1"] = "sess-1"
    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = "/restart"
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    handler.do_POST()
    assert bridge._task_sessions == {}
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] is True


# ── SIGTERM / crash contracts ───────────────────────────────────────────────────


def test_sigterm_clean_exit(bridge, caplog):
    import signal as _sig
    assert hasattr(bridge, "_handle_sigterm")
    with caplog.at_level("INFO", logger="grok-bridge"):
        with pytest.raises(SystemExit) as exc:
            bridge._handle_sigterm(_sig.SIGTERM, None)
    assert exc.value.code == 0
    msgs = "\n".join(r.message for r in caplog.records)
    assert "[shutdown] received SIGTERM" in msgs
    assert "[fatal]" not in msgs
    src = BRIDGE_PATH.read_text()
    assert "signal.signal(signal.SIGTERM, _handle_sigterm)" in src
    assert "except SystemExit:" in src


def test_main_logs_traceback_on_crash(tmp_path):
    import subprocess as sp
    import textwrap as tw

    bootstrap = tmp_path / "boot_crash.py"
    bootstrap.write_text(tw.dedent(f"""
        import importlib.util, sys
        spec = importlib.util.spec_from_file_location("grok_bridge", r"{BRIDGE_PATH}")
        gb = importlib.util.module_from_spec(spec)
        sys.modules["grok_bridge"] = gb
        spec.loader.exec_module(gb)

        def _boom(*a, **kw):
            raise RuntimeError("BOOM_TEST_MARKER")
        gb.http.server.HTTPServer = _boom
        gb.dispatch_poll_loop = lambda: None
        gb.heartbeat_loop = lambda: None
        gb.main()
    """))
    proc = sp.run([sys.executable, str(bootstrap)], capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    combined = proc.stderr + proc.stdout
    assert "[fatal]" in combined
    assert "BOOM_TEST_MARKER" in combined
    assert "Traceback" in combined


# ── run_grok_dispatch (subprocess injected) ─────────────────────────────────────


def test_run_grok_dispatch_reduces_stream(bridge, tmp_path, monkeypatch):
    """A fake Popen streaming the golden NDJSON reduces to an EndTurn outcome."""
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)

    class _FakeProc:
        def __init__(self):
            self.stdout = StringIO(GOLDEN_STREAM)
            self.stderr = StringIO("")
            self.returncode = 0
        def poll(self):
            return 0  # already exited → cancel watcher stops immediately
        def wait(self, timeout=None):
            return 0
        def terminate(self):
            pass
        def kill(self):
            pass

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    outcome = bridge.run_grok_dispatch(
        "do it", workspace=str(tmp_path), env={"HOME": str(tmp_path)},
        session_id="11111111-2222-3333-4444-555555555555", _popen=fake_popen,
    )
    assert outcome.saw_end is True
    assert outcome.stop_reason == "EndTurn"
    assert outcome.exit_code == 0
    # argv carried the streaming-json + session flags.
    assert "streaming-json" in captured["cmd"]
    assert "-s" in captured["cmd"]
