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


# ── dispatch_task / deliver_comment_nudge integration (subprocess + mc-cli mocked) ─
#
# These hit the exact integration paths the adversarial review flagged as untested:
# the terminal guarantee after `mc ack`, and routing the nudge outcome through the
# lifecycle. `mc` is mocked via bridge._sp.run; grok is mocked via bridge._sp.Popen.


def _mc_recorder(monkeypatch, bridge):
    """Record every `mc <subcommand>` invocation; each returns rc=0."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    monkeypatch.setattr(bridge._sp, "run", fake_run)
    return calls


def _fake_popen(stream_text: str, returncode: int = 0):
    class _P:
        def __init__(self, cmd, **kwargs):
            self.stdout = StringIO(stream_text)
            self.stderr = StringIO("")
            self.returncode = returncode
            self.pid = 424242

        def poll(self):
            return returncode  # already exited → watchdog/cancel threads no-op

        def wait(self, timeout=None):
            return returncode

        def terminate(self):
            pass

        def kill(self):
            pass

    return _P


def _mc_subcommands(calls):
    return [c[1] for c in calls if len(c) > 1]


def test_dispatch_missing_binary_ends_blocked_never_in_progress(bridge, tmp_path, monkeypatch):
    """CRITICAL regression: grok binary missing → task ends blocked, not hung in_progress."""
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)
    monkeypatch.setattr(bridge, "WORKSPACE", tmp_path)
    calls = _mc_recorder(monkeypatch, bridge)

    def boom(cmd, **kwargs):
        raise FileNotFoundError("grok: command not found")

    monkeypatch.setattr(bridge._sp, "Popen", boom)

    action = bridge.dispatch_task(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1", "title": "x", "description": "y"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok", "HOME": str(tmp_path)},
    )

    subs = _mc_subcommands(calls)
    assert "ack" in subs                       # ack ran (task moved to in_progress)
    assert "blocked" in subs                    # ...and a terminal blocker followed
    assert "finish" not in subs                 # never finished
    assert action.action == "blocked"
    # No stale session id left behind for a colliding re-dispatch.
    assert "t1" not in bridge._task_sessions


def test_dispatch_happy_path_acks_and_finishes(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)
    monkeypatch.setattr(bridge, "WORKSPACE", tmp_path)
    calls = _mc_recorder(monkeypatch, bridge)
    monkeypatch.setattr(bridge._sp, "Popen", _fake_popen(GOLDEN_STREAM))

    action = bridge.dispatch_task(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1", "title": "x", "description": "y"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok", "HOME": str(tmp_path)},
    )
    subs = _mc_subcommands(calls)
    assert "ack" in subs
    assert "finish" in subs
    assert action.action == "finish"
    # Session persisted from the confirmed end event → available for nudge resume.
    assert bridge._task_sessions["t1"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_dispatch_no_end_blocks_and_persists_no_session(bridge, tmp_path, monkeypatch):
    """No `end` event → blocked, and NO session id stored (Fix 5: no `-s` collision on retry)."""
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)
    monkeypatch.setattr(bridge, "WORKSPACE", tmp_path)
    calls = _mc_recorder(monkeypatch, bridge)
    monkeypatch.setattr(bridge._sp, "Popen", _fake_popen('{"type":"text","data":"partial"}\n'))

    action = bridge.dispatch_task(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok", "HOME": str(tmp_path)},
    )
    assert action.action == "blocked"
    assert "blocked" in _mc_subcommands(calls)
    assert "t1" not in bridge._task_sessions


def test_dispatch_unexpected_exception_still_blocks(bridge, tmp_path, monkeypatch):
    """Terminal guarantee: an exception AFTER ack (here in map_lifecycle) → mc blocked."""
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)
    monkeypatch.setattr(bridge, "WORKSPACE", tmp_path)
    calls = _mc_recorder(monkeypatch, bridge)
    monkeypatch.setattr(bridge._sp, "Popen", _fake_popen(GOLDEN_STREAM))

    def boom(*a, **kw):
        raise RuntimeError("mapping exploded")

    monkeypatch.setattr(bridge, "map_lifecycle", boom)

    action = bridge.dispatch_task(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok", "HOME": str(tmp_path)},
    )
    subs = _mc_subcommands(calls)
    assert "ack" in subs
    assert "blocked" in subs
    assert "finish" not in subs
    assert action.action == "blocked"


def test_nudge_routes_clean_outcome_to_comment(bridge, monkeypatch):
    """HIGH regression: a clean nudge posts grok's reply via mc comment + resumes -r."""
    bridge._task_sessions["t1"] = "sess-1"
    bridge._task_ctx["t1"] = {"board_id": "b1", "attempt_id": "a1"}
    calls = _mc_recorder(monkeypatch, bridge)

    captured = {}

    def fake_run(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return bridge.GrokOutcome(saw_end=True, stop_reason="EndTurn",
                                  final_text="I checked Firefox, all good", exit_code=0)

    monkeypatch.setattr(bridge, "run_grok_dispatch", fake_run)

    out = bridge.deliver_comment_nudge(
        {"id": "t1", "board_id": "b1"},
        {"source": "user", "task_id": "t1", "content": "Teste auch Firefox"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok"},
    )
    assert out is not None
    # Resumes the stored session, not a fresh -s.
    assert captured["kwargs"].get("resume_session") == "sess-1"
    # Nudge prompt explicitly instructs mc comment (unlike the dispatch prompt).
    assert "mc comment" in captured["prompt"]
    # Reply surfaced to Mark as a comment; no blocker on a clean turn.
    subs = _mc_subcommands(calls)
    assert "comment" in subs
    assert "blocked" not in subs
    assert any("all good" in " ".join(c) for c in calls)


def test_nudge_unclean_stop_blocks(bridge, monkeypatch):
    """A nudge that crashes/times out must not vanish — it posts a comment + blocker."""
    bridge._task_sessions["t1"] = "sess-1"
    bridge._task_ctx["t1"] = {"board_id": "b1", "attempt_id": "a1"}
    calls = _mc_recorder(monkeypatch, bridge)

    monkeypatch.setattr(
        bridge, "run_grok_dispatch",
        lambda prompt, **kw: bridge.GrokOutcome(saw_end=False, exit_code=0),
    )
    bridge.deliver_comment_nudge(
        {"id": "t1", "board_id": "b1"},
        {"source": "user", "task_id": "t1", "content": "hi"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok"},
    )
    assert "blocked" in _mc_subcommands(calls)


def test_nudge_without_session_returns_none(bridge, monkeypatch):
    """No stored session → nothing to resume; leave it for the next full dispatch."""
    calls = _mc_recorder(monkeypatch, bridge)
    out = bridge.deliver_comment_nudge(
        {"id": "unknown", "board_id": "b1"},
        {"source": "user", "task_id": "unknown", "content": "hi"},
        {"MC_BASE_URL": "http://backend", "MC_AGENT_TOKEN": "tok"},
    )
    assert out is None
    assert calls == []


def test_map_lifecycle_cancelled_vs_watchdog(bridge):
    """Fix 6: a /stop cancel is reported distinctly from a timeout watchdog kill."""
    cancelled = bridge.map_lifecycle(bridge.GrokOutcome(cancelled=True, watchdog_killed=True))
    assert cancelled.action == "blocked" and cancelled.reason == "cancelled"
    timeout = bridge.map_lifecycle(bridge.GrokOutcome(watchdog_killed=True))
    assert timeout.action == "blocked" and timeout.reason == "watchdog"
