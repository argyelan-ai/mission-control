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
def bridge(monkeypatch, tmp_path):
    mod = _load_bridge()
    # Reset module-scoped state between tests (import caches it).
    mod._active_task = None
    mod._last_pane = ""
    mod._last_progress_ts = 0.0
    mod._nudges_sent = 0
    mod._last_dispatched_task_id = None
    mod._last_dispatched_attempt_id = None
    # Isolate the persisted last-task-id from the REAL host path — without this,
    # any test driving dispatch_task() writes ~/.mc/agents/grok/logs/last-task-id
    # on the dev machine (found live 2026-07-12: placeholder "t1" leaked into the
    # production bridge state and skewed the E2E fresh-dispatch scenario).
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
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


def test_launch_shell_cmd_sources_agent_env_in_window_shell(bridge):
    """Regression: tmux windows inherit env from the tmux SERVER, not the
    new-session client — a stale server-global once poisoned MC_AGENT_TOKEN
    (4.4 KB grown token). The window command must source agent.env itself."""
    line = bridge._grok_launch_shell_cmd()
    assert line.startswith("set -a; . ")
    assert str(bridge.ENV_FILE) in line
    assert "; set +a; exec " in line
    assert not line.startswith("sh -c")  # tmux already runs the line via sh -c


# ── session reset on task switch (ADR-068 addendum) ──────────────────────────────
#
# Dispatch semantics (dispatch.py:8-18): a NEW task must start with a fresh
# context. The v2 TUI paste model lost that property — tasks were pasted into
# the accumulated conversation. The bridge now sends the TUI's session-reset
# slash command (/new, verified live: instant, no picker outside a git repo)
# on a GENUINE task switch only:
#   - different task id than the last dispatched one  → reset
#   - same task id, new attempt (revision/request_changes/re-dispatch) → NO reset
#   - no known last task (first dispatch ever)        → NO reset
# The last dispatched task id is persisted to disk so a bridge restart cannot
# erase switch detection (in-memory dedup resets on restart; the file does not).


def test_should_reset_session_decision_table(bridge):
    """Reset ONLY on a genuine task switch: known last task, different new task."""
    # First dispatch ever (no persisted last task) → no reset.
    assert bridge.should_reset_session("task-b", None) is False
    assert bridge.should_reset_session("task-b", "") is False
    # Same task re-dispatch (revision / request_changes / recovery) → no reset.
    assert bridge.should_reset_session("task-a", "task-a") is False
    # Genuine switch → reset.
    assert bridge.should_reset_session("task-b", "task-a") is True


def test_last_task_id_persists_to_disk(bridge, tmp_path, monkeypatch):
    """Switch detection must survive a bridge restart → file-backed, not memory."""
    state_file = tmp_path / "last-task-id"
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", state_file)
    assert bridge.load_last_task_id() is None
    bridge.save_last_task_id("task-a")
    assert state_file.read_text(encoding="utf-8").strip() == "task-a"
    assert bridge.load_last_task_id() == "task-a"


def test_reset_tui_session_sends_literal_command_then_cr(bridge, monkeypatch):
    """The reset command goes in as LITERAL keys + raw CR (-H 0d) — never through
    the bracketed-paste path (a paste-mode TUI would swallow the slash command;
    CR is the universal submit, see poll.sh Bug 2026-05-15)."""
    calls = _tmux_recorder(monkeypatch, bridge, running=True, pane="❯")
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    bridge.reset_tui_session()
    sends = [c for c in calls if c and c[0] == "send-keys"]
    assert any("-l" in c and bridge.RESET_COMMAND in c for c in sends), sends
    assert any("-H" in c and "0d" in c for c in sends), sends
    # No load-buffer/paste-buffer for the slash command.
    assert not any(c[0] in ("load-buffer", "paste-buffer") for c in calls)


def test_dispatch_task_resets_session_on_task_switch(bridge, tmp_path, monkeypatch):
    """Task A done → task B arrives: reset fires BEFORE context+paste of B."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    bridge.save_last_task_id("task-a")
    order: list[str] = []
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: order.append("reset"))
    monkeypatch.setattr(bridge, "deliver_task_context", lambda task: order.append("context"))
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: order.append("paste"))

    ok = bridge.dispatch_task({"id": "task-b", "title": "x", "prompt": "y"}, {})
    assert ok is True
    assert order == ["reset", "context", "paste"]
    # Persisted pointer moved on to task-b.
    assert bridge.load_last_task_id() == "task-b"


def test_dispatch_task_no_reset_on_same_task_revision(bridge, tmp_path, monkeypatch):
    """Same task, fresh attempt id (operator revision / request_changes):
    the agent keeps its context — NO reset."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    bridge.save_last_task_id("task-a")
    order: list[str] = []
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: order.append("reset"))
    monkeypatch.setattr(bridge, "deliver_task_context", lambda task: order.append("context"))
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: order.append("paste"))

    ok = bridge.dispatch_task(
        {"id": "task-a", "dispatch_attempt_id": "attempt-2", "title": "x", "prompt": "y"}, {},
    )
    assert ok is True
    assert order == ["context", "paste"]


def test_dispatch_task_no_reset_on_first_dispatch(bridge, tmp_path, monkeypatch):
    """No persisted last task (fresh install / first ever dispatch) → no reset."""
    monkeypatch.setattr(bridge, "LAST_TASK_FILE", tmp_path / "last-task-id")
    order: list[str] = []
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: order.append("reset"))
    monkeypatch.setattr(bridge, "deliver_task_context", lambda task: order.append("context"))
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: order.append("paste"))

    ok = bridge.dispatch_task({"id": "task-a", "title": "x", "prompt": "y"}, {})
    assert ok is True
    assert order == ["context", "paste"]
    assert bridge.load_last_task_id() == "task-a"


def test_reset_command_default_is_new(bridge):
    """grok TUI: /new starts a fresh session (verified live 2026-07-12 — instant,
    no worktree picker because the grok workspace is not a git repo)."""
    assert bridge.RESET_COMMAND == "/new"


# ── W2 bridge parity: comm_v2 message queue + turn-gate + flush ─────────────────
#
# Mirrors docker/shared/poll.sh's Interaction-Model-2.0 section: acked_seq poll
# param, crash-safe queue-before-paste, a pane-quiet turn-gate (grok has no
# native turn-state signal), verified flush (footer-anchor search), at-least-
# once ack semantics, and the /clear-on-done bugfix.


@pytest.fixture(autouse=True)
def _isolate_msg_state(bridge, tmp_path, monkeypatch):
    """Every comm_v2 test gets its own queue/ack/reset-marker dirs — without
    this, tests would read/write the real ~/.mc/agents/grok state."""
    monkeypatch.setattr(bridge, "MSG_QUEUE_DIR", tmp_path / "msg-queue")
    monkeypatch.setattr(bridge, "MSG_ACK_DIR", tmp_path / "msg-acked")
    monkeypatch.setattr(bridge, "LAST_RESET_TASK_ID_FILE", tmp_path / "last-reset-task-id")
    bridge._last_reset_task_id = None
    bridge._dispatch_in_flight = False
    bridge._msg_gate_last_pane = ""
    bridge._msg_gate_last_change_ts = 0.0
    return bridge


def _msg(seq=1, tid="thread-1", body="hi", sender="user", mtype="text"):
    return {"id": f"m{seq}", "thread_id": tid, "seq": seq, "sender": sender,
            "message_type": mtype, "body": body, "question_meta": None}


def test_build_acked_seq_param_empty_when_no_acks(bridge):
    assert bridge.build_acked_seq_param() == ""


def test_build_acked_seq_param_urlencoded_json(bridge):
    bridge._record_ack("thread-1", 3)
    bridge._record_ack("thread-2", 7)
    enc = bridge.build_acked_seq_param()
    assert enc != ""
    import urllib.parse
    decoded = json.loads(urllib.parse.unquote(enc))
    assert decoded == {"thread-1": 3, "thread-2": 7}


def test_record_ack_is_high_water_mark(bridge):
    bridge._record_ack("t1", 5)
    bridge._record_ack("t1", 3)  # lower seq must not regress the mark
    assert (bridge.MSG_ACK_DIR / "t1").read_text().strip() == "5"
    bridge._record_ack("t1", 9)
    assert (bridge.MSG_ACK_DIR / "t1").read_text().strip() == "9"


def test_queue_new_messages_writes_seq_named_files_with_footer(bridge):
    n = bridge.queue_new_messages([_msg(seq=2, tid="th-a"), _msg(seq=10, tid="th-b")])
    assert n == 2
    files = sorted(p.name for p in bridge.MSG_QUEUE_DIR.glob("*.msg"))
    assert files == ["00000002__th-a.msg", "00000010__th-b.msg"]
    content = (bridge.MSG_QUEUE_DIR / "00000002__th-a.msg").read_text()
    assert "[thread th-a · seq 2 · von user · typ text]" in content
    assert "hi" in content


def test_queue_new_messages_idempotent_redelivery(bridge):
    bridge.queue_new_messages([_msg(seq=1, tid="t1", body="first")])
    bridge.queue_new_messages([_msg(seq=1, tid="t1", body="first")])  # redelivery
    assert len(list(bridge.MSG_QUEUE_DIR.glob("*.msg"))) == 1


def test_queue_new_messages_skips_malformed_entries(bridge):
    n = bridge.queue_new_messages([{"body": "no seq or thread_id"}, _msg(seq=1)])
    assert n == 1


def test_msg_queue_files_sorted_by_seq(bridge):
    bridge.queue_new_messages([_msg(seq=10, tid="a"), _msg(seq=2, tid="b"), _msg(seq=1, tid="c")])
    names = [p.name for p in bridge.msg_queue_files()]
    assert names == ["00000001__c.msg", "00000002__b.msg", "00000010__a.msg"]


def test_msg_gate_closed_when_dispatch_in_flight(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "capture_pane", lambda: "same")
    bridge._dispatch_in_flight = True
    assert bridge.msg_gate_open() is False


def test_msg_gate_closed_when_session_not_running(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    assert bridge.msg_gate_open() is False


def test_msg_gate_pane_quiet_requires_stability_window(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "MSG_QUIET_SECONDS", 10)
    # First observation of a pane always resets the clock → not quiet yet.
    assert bridge._msg_gate_pane_quiet(now=100.0, pane="X") is False
    # Same pane, not enough time elapsed.
    assert bridge._msg_gate_pane_quiet(now=105.0, pane="X") is False
    # Same pane, quiet window elapsed → open.
    assert bridge._msg_gate_pane_quiet(now=111.0, pane="X") is True
    # Pane changes → clock resets.
    assert bridge._msg_gate_pane_quiet(now=112.0, pane="Y") is False


def test_msg_gate_open_end_to_end(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "capture_pane", lambda: "❯ ")
    monkeypatch.setattr(bridge, "MSG_QUIET_SECONDS", 1)
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 1000.0)
    assert bridge.msg_gate_open() is False  # first sight
    monkeypatch.setattr(bridge.time, "monotonic", lambda: 1002.0)
    assert bridge.msg_gate_open() is True  # stable past the window


def test_flush_msg_queue_verified_paste_acks_and_removes_file(bridge, monkeypatch):
    bridge.queue_new_messages([_msg(seq=1, tid="th-1", body="hello")])
    pasted = []
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: True)
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: pasted.append(text))
    monkeypatch.setattr(bridge, "capture_pane", lambda: "...\n[thread th-1 · seq 1 · von user · typ text]\n❯")
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a, **_k: None)

    bridge.flush_msg_queue()

    assert len(pasted) == 1 and "hello" in pasted[0]
    assert (bridge.MSG_ACK_DIR / "th-1").read_text().strip() == "1"
    assert list(bridge.MSG_QUEUE_DIR.glob("*.msg")) == []
    assert bridge._dispatch_in_flight is False  # flag cleared after flush


def test_flush_msg_queue_verify_fail_stops_flush_no_ack(bridge, monkeypatch):
    bridge.queue_new_messages([_msg(seq=1, tid="th-1"), _msg(seq=2, tid="th-1")])
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: True)
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: None)
    # Pane never shows the footer anchor — verify fails.
    monkeypatch.setattr(bridge, "capture_pane", lambda: "nothing useful here")
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a, **_k: None)

    bridge.flush_msg_queue()

    assert not (bridge.MSG_ACK_DIR / "th-1").exists()
    # Both files remain queued (at-least-once — nothing acked, nothing deleted).
    assert len(list(bridge.MSG_QUEUE_DIR.glob("*.msg"))) == 2


def test_flush_msg_queue_gate_closing_mid_flush_stops_remaining(bridge, monkeypatch):
    bridge.queue_new_messages([_msg(seq=1, tid="th-1"), _msg(seq=2, tid="th-1")])
    gate_calls = {"n": 0}

    def fake_gate():
        gate_calls["n"] += 1
        return gate_calls["n"] == 1  # open for the first file only

    monkeypatch.setattr(bridge, "msg_gate_open", fake_gate)
    monkeypatch.setattr(bridge, "paste_and_submit", lambda text: None)
    monkeypatch.setattr(
        bridge, "capture_pane",
        lambda: "[thread th-1 · seq 1 · von user · typ text]",
    )
    monkeypatch.setattr(bridge.time, "sleep", lambda *_a, **_k: None)

    bridge.flush_msg_queue()

    # seq 1 delivered + acked; seq 2 stayed queued because the gate closed.
    assert (bridge.MSG_ACK_DIR / "th-1").read_text().strip() == "1"
    remaining = [p.name for p in bridge.MSG_QUEUE_DIR.glob("*.msg")]
    assert remaining == ["00000002__th-1.msg"]


def test_deliver_messages_noop_when_field_absent(bridge, monkeypatch):
    """comm_v2=false byte-identical behavior: no `new_messages` key ⇒ no queue dir
    is even created, no gate check happens."""
    called = {"gate": False}
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: called.__setitem__("gate", True) or True)
    bridge.deliver_messages({"state": "idle", "new_comments": []})
    assert called["gate"] is False
    assert not bridge.MSG_QUEUE_DIR.exists()


def test_deliver_messages_queues_and_flushes_when_gate_open(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: True)
    flushed = {"n": 0}
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.__setitem__("n", flushed["n"] + 1))
    bridge.deliver_messages({"state": "idle", "new_messages": [_msg(seq=1)]})
    assert flushed["n"] == 1
    assert len(bridge.msg_queue_files()) == 1  # flush is mocked, file stays


def test_deliver_messages_queues_only_when_gate_closed(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: False)
    flushed = {"n": 0}
    monkeypatch.setattr(bridge, "flush_msg_queue", lambda: flushed.__setitem__("n", flushed["n"] + 1))
    bridge.deliver_messages({"state": "idle", "new_messages": [_msg(seq=1)]})
    assert flushed["n"] == 0
    assert len(bridge.msg_queue_files()) == 1


def test_deliver_messages_empty_list_is_noop_for_gate(bridge, monkeypatch):
    """An empty `new_messages: []` (field present, nothing new) with an
    already-empty queue must not touch the gate — nothing to flush."""
    called = {"gate": False}
    monkeypatch.setattr(bridge, "msg_gate_open", lambda: called.__setitem__("gate", True) or True)
    bridge.deliver_messages({"state": "idle", "new_messages": []})
    assert called["gate"] is False


# ── /clear-on-done bugfix ────────────────────────────────────────────────────────


def test_maybe_reset_on_done_fires_once_per_finished_task(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    resets = []
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: resets.append(1))

    bridge.maybe_reset_on_done("task-a")
    assert resets == [1]
    # Same finished task id again (e.g. repeated idle polls) → no re-fire.
    bridge.maybe_reset_on_done("task-a")
    assert resets == [1]
    # A DIFFERENT finished task → fires again.
    bridge.maybe_reset_on_done("task-b")
    assert resets == [1, 1]


def test_maybe_reset_on_done_noop_when_no_finished_task(bridge, monkeypatch):
    resets = []
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: resets.append(1))
    bridge.maybe_reset_on_done(None)
    assert resets == []


def test_maybe_reset_on_done_noop_when_session_not_running(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    resets = []
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: resets.append(1))
    bridge.maybe_reset_on_done("task-a")
    assert resets == []


def test_maybe_reset_on_done_idempotency_survives_restart_via_disk(bridge, monkeypatch):
    """The in-memory marker resets on bridge restart; the disk file must not,
    so a stale bridge process restart doesn't re-fire /new for an already-
    reset finished task."""
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    resets = []
    monkeypatch.setattr(bridge, "reset_tui_session", lambda: resets.append(1))
    bridge.maybe_reset_on_done("task-a")
    assert resets == [1]
    assert bridge.LAST_RESET_TASK_ID_FILE.read_text().strip() == "task-a"

    # Simulate a bridge restart: in-memory marker forgotten, disk persists.
    bridge._last_reset_task_id = None
    bridge.maybe_reset_on_done("task-a")
    assert resets == [1]  # still no re-fire — disk marker caught it


def test_dispatch_task_sets_and_clears_dispatch_in_flight(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)
    monkeypatch.setattr(bridge, "deliver_task_context", lambda task: None)
    seen_in_flight = {}

    def fake_paste(text):
        seen_in_flight["during_paste"] = bridge._dispatch_in_flight

    monkeypatch.setattr(bridge, "paste_and_submit", fake_paste)

    bridge.dispatch_task({"id": "t1", "title": "x", "prompt": "y"}, {})
    assert seen_in_flight["during_paste"] is True
    assert bridge._dispatch_in_flight is False  # cleared after dispatch


def test_reset_tui_session_sets_dispatch_in_flight_during_reset(bridge, monkeypatch):
    calls = _tmux_recorder(monkeypatch, bridge, running=True, pane="❯")
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)
    seen = {}

    def fake_wait(*a, **k):
        seen["during_reset"] = bridge._dispatch_in_flight
        return True

    monkeypatch.setattr(bridge, "wait_for_agent_healthy", fake_wait)
    bridge.reset_tui_session()
    assert seen["during_reset"] is True
    assert bridge._dispatch_in_flight is False
