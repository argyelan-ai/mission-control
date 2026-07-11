"""Tests for scripts/grok-bridge.py — host-side bridge for the Grok Build CLI.

v2 TUI paste model (ADR-068): grok runs as a persistent interactive TUI in a tmux
session; the bridge PASTES each dispatch into it and the grok agent drives its own
MC lifecycle. There is NO headless `-p` / streaming-json subprocess anymore.

The script lives outside the backend package and has a hyphen in its filename, so we
import it dynamically via importlib (same pattern as test_hermes_bridge.py). These
tests pin: the localhost/port/no-`-p` constants, the mc-context.env 3-key contract,
the dispatch/comment prompts, the tmux paste mechanic + session autostart + readiness,
the ordering guarantee (context BEFORE paste), the no-progress watchdog nudge, the
health/restart/stop control endpoints, and the SIGTERM/crash contracts.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "grok-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("grok_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["grok_bridge"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge():
    mod = _load_bridge()
    # Reset module-scoped state between tests (import caches it).
    mod._active_task = None
    mod._last_pane = ""
    mod._last_progress_ts = 0.0
    mod._nudges_sent = 0
    mod._last_dispatched_task_id = None
    mod._last_dispatched_attempt_id = None
    return mod


def _tmux_recorder(monkeypatch, bridge, *, running=True, pane=""):
    """Record every tmux invocation; has-session returns `running`, capture-pane `pane`."""
    calls: list[list[str]] = []

    def fake_tmux(args, **kwargs):
        calls.append(list(args))
        rc = 0
        out = ""
        if args and args[0] == "has-session":
            rc = 0 if running else 1
        elif args and args[0] == "capture-pane":
            out = pane
        m = MagicMock()
        m.returncode = rc
        m.stdout = out
        m.stderr = ""
        return m

    monkeypatch.setattr(bridge, "_tmux", fake_tmux)
    return calls


# ── constants / security ────────────────────────────────────────────────────────


def test_host_and_port_constants(bridge):
    """Bridge MUST bind 127.0.0.1 only, never 0.0.0.0. Port 18795 reserved for grok."""
    assert bridge.HOST == "127.0.0.1"
    assert bridge.HOST != "0.0.0.0"
    assert bridge.PORT == 18795
    assert bridge.HARNESS == "grok"
    assert bridge.SESSION == "grok"


def test_source_binds_localhost_and_uses_paste_model(bridge):
    src = BRIDGE_PATH.read_text()
    assert "0.0.0.0" not in src
    assert "127.0.0.1" in src
    # The paste mechanic is load-bearing.
    assert "paste-buffer" in src
    assert "load-buffer" in src


def test_grok_launch_cmd_is_interactive_never_headless(bridge):
    """ADR-068: grok is launched as an interactive TUI — none of the forbidden
    headless print flags may appear in the actual launch argv."""
    cmd = bridge._grok_launch_cmd()
    joined = " ".join(cmd)
    assert cmd[0] == bridge.GROK_BIN
    assert "--no-alt-screen" in cmd
    assert "--permission-mode" in cmd
    # Forbidden headless / print-mode flags — must never be launched.
    for forbidden in ("-p", "--single", "--prompt-file", "--prompt-json", "--output-format"):
        assert forbidden not in cmd, f"{forbidden} must not be in the grok launch argv"
    assert "streaming-json" not in joined


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


# ── dispatch prompt (agent-owned lifecycle) ─────────────────────────────────────


def test_build_dispatch_prompt_has_ids_and_agent_self_finishes(bridge):
    prompt = bridge.build_dispatch_prompt({
        "id": "task-123", "board_id": "board-9", "dispatch_attempt_id": "att-7",
        "title": "Fix login", "prompt": "Do the thing",
    })
    assert "task_id=task-123" in prompt
    assert "board_id=board-9" in prompt
    assert "attempt_id=att-7" in prompt
    assert "Fix login" in prompt
    assert "Do the thing" in prompt
    # ADR-068: the AGENT owns the lifecycle — ack + finish itself.
    assert "mc ack task-123" in prompt
    assert "mc finish task-123" in prompt
    assert "mc deliverable" in prompt
    # And the bridge explicitly does NOT close tasks.
    assert "NOT close tasks" in prompt
    # SECURITY: never materialize the literal token.
    assert "MC_AGENT_TOKEN" not in prompt or "$MC_AGENT_TOKEN" in prompt


def test_build_dispatch_prompt_prefers_backend_prompt_over_description(bridge):
    """task.prompt (backend-built, carries SOUL/TOOLS) wins over a plain description."""
    p = bridge.build_dispatch_prompt({"id": "t", "prompt": "RICH", "description": "PLAIN"})
    assert "RICH" in p and "PLAIN" not in p


def test_build_comments_prompt_user_and_system(bridge):
    prompt = bridge.build_comments_prompt([
        {"source": "user", "task_id": "t1", "task_title": "T", "content": "Teste Firefox"},
        {"source": "system", "comment_type": "blocker", "task_id": "t1",
         "task_title": "T", "content": "blocked"},
    ])
    assert "User-Kommentare" in prompt
    assert "Teste Firefox" in prompt
    assert "System-Events" in prompt
    assert "mc comment" in prompt


def test_build_comments_prompt_empty_returns_blank(bridge):
    assert bridge.build_comments_prompt([]) == ""


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
    assert bridge._unquote_env_value("'has'\"'\"'quote'") == "has'quote"
    assert bridge._unquote_env_value("'plain'") == "plain"


# ── tmux paste mechanic ─────────────────────────────────────────────────────────


def test_paste_and_submit_uses_load_buffer_then_end_marker_then_enter(bridge, tmp_path, monkeypatch):
    """The exact poll.sh sequence: load-buffer → paste-buffer → 201~ marker → Enter."""
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path)
    calls = _tmux_recorder(monkeypatch, bridge)
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a, **_k: None)

    bridge.paste_and_submit("hello dispatch")

    verbs = [c[0] for c in calls]
    assert verbs == ["load-buffer", "paste-buffer", "send-keys", "send-keys"]
    # Buffer file carried the text.
    assert (tmp_path / "dispatch.paste").read_text() == "hello dispatch"
    # Bracketed-paste-end marker before the submitting Enter.
    marker = calls[2]
    assert "-H" in marker and "1b" in marker and "7e" in marker
    assert calls[3][-1] == "Enter"


def test_is_session_running_reads_has_session(bridge, monkeypatch):
    _tmux_recorder(monkeypatch, bridge, running=True)
    assert bridge.is_session_running() is True
    _tmux_recorder(monkeypatch, bridge, running=False)
    assert bridge.is_session_running() is False


def test_capture_pane_returns_stdout(bridge, monkeypatch):
    _tmux_recorder(monkeypatch, bridge, pane="Grok 4.5 (high)\n❯ ")
    assert "❯" in bridge.capture_pane()


def test_wait_for_agent_healthy_detects_glyph(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "capture_pane", lambda: "…\n❯ ")
    assert bridge.wait_for_agent_healthy(timeout=0.1) is True
    monkeypatch.setattr(bridge, "capture_pane", lambda: "still booting")
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a, **_k: None)
    assert bridge.wait_for_agent_healthy(timeout=0.05) is False


# ── session autostart ───────────────────────────────────────────────────────────


def test_start_grok_session_launches_interactive_tui(bridge, tmp_path, monkeypatch):
    """new-session runs the interactive grok TUI (no -p) in the workspace cwd."""
    monkeypatch.setattr(bridge, "ENV_FILE", tmp_path / "agent.env")
    (tmp_path / "agent.env").write_text("MC_BASE_URL='http://b'\nMC_AGENT_TOKEN='t'\n")
    monkeypatch.setattr(bridge, "WORKSPACE", tmp_path / "ws")
    monkeypatch.setattr(bridge, "LOG_DIR", tmp_path / "logs")
    calls = _tmux_recorder(monkeypatch, bridge, running=False)
    monkeypatch.setattr(bridge, "wait_for_agent_healthy", lambda *a, **k: True)

    result = bridge.start_grok_session()
    assert result["status"] == "started"

    new_session = next(c for c in calls if c and c[0] == "new-session")
    joined = " ".join(new_session)
    assert "-d" in new_session and "-s" in new_session and "grok" in new_session
    # cwd is the task workspace.
    assert str(tmp_path / "ws") in new_session
    # Interactive grok TUI, NOT headless.
    assert "--no-alt-screen" in joined
    assert "--permission-mode" in joined
    assert "-p" not in new_session and "--prompt-file" not in joined


def test_start_grok_session_missing_env_raises(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ENV_FILE", tmp_path / "nope.env")
    with pytest.raises(FileNotFoundError):
        bridge.start_grok_session()


def test_start_grok_session_already_running_is_noop(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "ENV_FILE", tmp_path / "agent.env")
    (tmp_path / "agent.env").write_text("MC_BASE_URL='http://b'\n")
    calls = _tmux_recorder(monkeypatch, bridge, running=True)
    result = bridge.start_grok_session()
    assert result["status"] == "already_running"
    assert not any(c and c[0] == "new-session" for c in calls)


# ── dispatch flow: context BEFORE paste ─────────────────────────────────────────


def test_dispatch_task_writes_context_before_paste(bridge, tmp_path, monkeypatch):
    """CRITICAL ordering: mc-context.env + tmux env are set BEFORE the paste, so
    the agent's very first `mc ack` in its turn already resolves its context."""
    monkeypatch.setattr(bridge, "MC_CONTEXT_ENV_PATH", str(tmp_path / "mc-context.env"))
    order: list[str] = []

    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(
        bridge, "write_task_context_env",
        lambda task, path=None: order.append("context") or True,
    )

    def fake_tmux(args, **kwargs):
        if args and args[0] == "set-environment":
            order.append("set-env")
        m = MagicMock(); m.returncode = 0; m.stdout = ""; m.stderr = ""
        return m

    monkeypatch.setattr(bridge, "_tmux", fake_tmux)
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: order.append("paste"))

    ok = bridge.dispatch_task(
        {"id": "t1", "board_id": "b1", "dispatch_attempt_id": "a1", "title": "x", "prompt": "y"},
        {"MC_BASE_URL": "http://b", "MC_AGENT_TOKEN": "tok"},
    )
    assert ok is True
    # context + tmux env come before the paste.
    assert order.index("context") < order.index("paste")
    assert order.index("set-env") < order.index("paste")
    # Active-task tracking armed for the watchdog.
    assert bridge._active_task is not None and bridge._active_task["id"] == "t1"


def test_dispatch_task_autostarts_session_when_down(bridge, monkeypatch):
    started = {"n": 0}
    state = {"running": False}
    monkeypatch.setattr(bridge, "is_session_running", lambda: state["running"])

    def fake_start():
        started["n"] += 1
        state["running"] = True
        return {"status": "started"}

    monkeypatch.setattr(bridge, "start_grok_session", fake_start)
    monkeypatch.setattr(bridge, "deliver_task_context", lambda task: None)
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: None)

    ok = bridge.dispatch_task({"id": "t1"}, {})
    assert ok is True
    assert started["n"] == 1


def test_dispatch_task_returns_false_if_session_wont_start(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)

    def boom():
        raise RuntimeError("tmux new-session failed")

    monkeypatch.setattr(bridge, "start_grok_session", boom)
    pasted = {"n": 0}
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: pasted.__setitem__("n", pasted["n"] + 1))
    ok = bridge.dispatch_task({"id": "t1"}, {})
    assert ok is False
    assert pasted["n"] == 0


# ── no-progress watchdog ─────────────────────────────────────────────────────────


def test_should_nudge_decision_table(bridge):
    f = bridge.should_nudge
    assert f(active=True, idle_seconds=400, idle_threshold=300, nudges_sent=0, max_nudges=2) is True
    # Not idle enough.
    assert f(active=True, idle_seconds=100, idle_threshold=300, nudges_sent=0, max_nudges=2) is False
    # No active task.
    assert f(active=False, idle_seconds=999, idle_threshold=300, nudges_sent=0, max_nudges=2) is False
    # Budget exhausted.
    assert f(active=True, idle_seconds=999, idle_threshold=300, nudges_sent=2, max_nudges=2) is False


def test_watchdog_tick_progress_resets_and_no_nudge(bridge):
    bridge._active_task = {"id": "t1"}
    bridge._last_pane = "old"
    bridge._last_progress_ts = 0.0
    # Pane changed → progress → reset clock, no nudge.
    out = bridge._watchdog_tick(now=1000.0, pane="new content")
    assert out is None
    assert bridge._last_pane == "new content"
    assert bridge._last_progress_ts == 1000.0


def test_watchdog_tick_stall_emits_nudge_then_respects_budget(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "NUDGE_IDLE_TIMEOUT", 300)
    monkeypatch.setattr(bridge, "NUDGE_MAX", 1)
    bridge._active_task = {"id": "t1"}
    bridge._last_pane = "frozen"
    bridge._last_progress_ts = 0.0
    # Same pane, idle past threshold → nudge.
    out = bridge._watchdog_tick(now=400.0, pane="frozen")
    assert out is not None
    assert "mc finish t1" in out
    assert bridge._nudges_sent == 1
    # Idle window reset; budget now exhausted → no second nudge.
    out2 = bridge._watchdog_tick(now=800.0, pane="frozen")
    assert out2 is None


def test_watchdog_tick_no_active_task_is_noop(bridge):
    bridge._active_task = None
    assert bridge._watchdog_tick(now=999.0, pane="anything") is None


# ── HTTP control ─────────────────────────────────────────────────────────────────


def _make_handler(bridge, path, method="GET"):
    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = path
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    return handler


def test_health_endpoint_payload(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "ENV_FILE", Path("/nonexistent/agent.env"))
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    handler = _make_handler(bridge, "/health")
    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue())
    assert payload["status"] == "ok"
    assert payload["harness"] == "grok"
    assert payload["session"] == "grok"
    assert payload["tmux_running"] is False
    assert payload["agent_env_present"] is False
    handler.send_response.assert_called_with(200)


def test_restart_kills_and_restarts_session(bridge, monkeypatch):
    calls = _tmux_recorder(monkeypatch, bridge, running=False)
    monkeypatch.setattr(bridge, "start_grok_session", lambda: {"status": "started"})
    bridge._active_task = {"id": "t1"}
    handler = _make_handler(bridge, "/restart", "POST")
    handler.do_POST()
    assert any(c and c[0] == "kill-session" for c in calls)
    assert bridge._active_task is None  # cleared on restart
    payload = json.loads(handler.wfile.getvalue())
    assert payload["ok"] is True


def test_stop_sends_escape(bridge, monkeypatch):
    calls = _tmux_recorder(monkeypatch, bridge)
    handler = _make_handler(bridge, "/stop", "POST")
    handler.do_POST()
    esc = next(c for c in calls if c and c[0] == "send-keys")
    assert "Escape" in esc
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
        gb.start_grok_session = lambda: None
        gb.dispatch_poll_loop = lambda: None
        gb.watchdog_loop = lambda: None
        gb.heartbeat_loop = lambda: None
        gb.main()
    """))
    proc = sp.run([sys.executable, str(bootstrap)], capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    combined = proc.stderr + proc.stdout
    assert "[fatal]" in combined
    assert "BOOM_TEST_MARKER" in combined
    assert "Traceback" in combined
