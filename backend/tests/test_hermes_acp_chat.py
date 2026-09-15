"""PR D — Hermes chat over ACP (`HERMES_DRIVER=acp`).

The host bridge gets a SECOND driver: instead of typing into the native TUI in
tmux, it holds one long-lived `hermes acp` session through the very same
`ChatSession` the omp container runs (docker/omp-bridge/acp_chat.py), and the
backend reaches it over `POST /chat/<op>` (the HTTP twin of the container's
`acp_chat_ctl.py`).

Two things these tests must pin, because both were bought expensively:

1. The transcript lands EXACTLY where the backend reads it
   (``~/.mc/agents/hermes/omp-sessions/<encoded-cwd>/``) — one character off
   and the Sessions chat stays empty while every log says "delivered".
2. With the driver unset NOTHING changes: the native tmux path must stay
   byte-identical (sabotage test below), because that path is what the live
   agent runs today.

The bridge script lives outside the backend package and has a hyphen in its
name, so it is loaded via importlib — same pattern as
test_hermes_bridge.py / test_hermes_dispatcher.py. The ACP child is NEVER
spawned: every test injects a fake session/client.
"""
from __future__ import annotations

import importlib.util
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock

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
    """Bridge module with every host path redirected into tmp_path."""
    mod = _load_bridge()
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
    monkeypatch.setattr(mod, "WORKSPACE", tmp_path / "agents" / "hermes")
    monkeypatch.setattr(mod, "MSG_QUEUE_DIR", tmp_path / "msg-queue")
    monkeypatch.setattr(mod, "MSG_ACK_DIR", tmp_path / "msg-acked")
    monkeypatch.delenv("HERMES_DRIVER", raising=False)
    return mod


class FakeSession:
    """In-memory stand-in for acp_chat.ChatSession (no child process)."""

    def __init__(self, *, busy: bool = False):
        self.prompts: list[str] = []
        self.configs: list[tuple] = []
        self.cancelled = 0
        self.closed = 0
        self.started = 0
        self.waited: list[float] = []
        self._busy = busy
        #: A running turn that ENDS while the caller waits (the live case);
        #: False = it never ends within the wait (timeout).
        self.idle_after_wait = True

    def start(self):
        self.started += 1

    def close(self):
        self.closed += 1

    def prompt(self, text):
        self.prompts.append(text)
        if self._busy:
            return {"ok": False, "error": "busy"}
        return {"ok": True, "turn": len(self.prompts)}

    def cancel(self):
        self.cancelled += 1
        return {"ok": True}

    def config(self, option_id, value):
        self.configs.append((option_id, value))
        return {"ok": True, "configOptions": []}

    def state(self):
        return {"version": 1, "driver": "hermes", "busy": self._busy, "turn": 0}

    def wait_idle(self, timeout=30.0):
        self.waited.append(timeout)
        if self.idle_after_wait:
            self._busy = False
        return not self._busy


def _daemon(bridge, session):
    """A started ChatDaemon in front of `session`."""
    daemon = bridge.hermes_acp_chat.ChatDaemon(lambda: session)
    daemon.start()
    return daemon


# ── driver selection ────────────────────────────────────────────────────────


def test_driver_defaults_to_native(bridge, monkeypatch):
    """No HERMES_DRIVER → native. The live agent runs without the variable."""
    assert bridge.hermes_acp_chat.driver() == "native"
    assert bridge.driver_is_acp() is False


def test_driver_acp_is_recognised_case_insensitively(bridge, monkeypatch):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    assert bridge.driver_is_acp() is True
    monkeypatch.setenv("HERMES_DRIVER", " ACP ")
    assert bridge.driver_is_acp() is True
    monkeypatch.setenv("HERMES_DRIVER", "native")
    assert bridge.driver_is_acp() is False


# ── the path the backend reads ──────────────────────────────────────────────


class _HermesAgentStub:
    slug = "hermes"
    agent_runtime = "host"
    harness = "hermes"


def test_sessions_dir_is_exactly_what_the_backend_reads(bridge, tmp_path, monkeypatch):
    """``<workspace>/omp-sessions/<encoded-cwd>`` — verified against the
    backend's own resolver, not against a hand-written string."""
    from app.services import omp_chat

    monkeypatch.setenv("HOME_HOST", str(tmp_path))
    workspace = tmp_path / ".mc" / "agents" / "hermes"
    cwd = str(tmp_path / "ws" / "hermes")

    path = bridge.hermes_acp_chat.sessions_dir(workspace=workspace, cwd=cwd)

    encoded = "--" + cwd.strip("/").replace("/", "-") + "--"
    assert path == omp_chat.resolve_transcript_dir(_HermesAgentStub()) / encoded
    assert path.is_dir(), "session_dir must create the directory (fail-closed otherwise)"
    assert bridge.hermes_acp_chat.SESSIONS_DIRNAME == omp_chat._SESSIONS_DIRNAME


def test_build_session_wires_hermes_acp_child_and_paths(bridge, tmp_path, monkeypatch):
    """client_factory → `hermes acp --accept-hooks` in the agent's cwd, with
    agent.env merged over os.environ; transcript + socket next to it."""
    captured = {}

    class _FakeClient:
        def __init__(self, command=None, cwd=None, env=None):
            captured["command"] = command
            captured["cwd"] = cwd
            captured["env"] = env

    workspace = tmp_path / ".mc" / "agents" / "hermes"
    cwd = str(tmp_path / "ws")
    session = bridge.hermes_acp_chat.build_session(
        workspace=workspace,
        hermes_bin="/opt/hermes",
        env={"MC_AGENT_TOKEN": "t"},
        cwd=cwd,
        client_cls=_FakeClient,
    )
    session._client_factory()

    assert captured["command"] == ["/opt/hermes", "acp", "--accept-hooks"]
    assert captured["cwd"] == cwd
    assert captured["env"]["MC_AGENT_TOKEN"] == "t"
    assert "PATH" in captured["env"], "agent.env must be merged OVER os.environ"
    assert session.state_file.parent == workspace / "omp-sessions" / (
        "--" + cwd.strip("/").replace("/", "-") + "--"
    )
    assert bridge.hermes_acp_chat.socket_path(workspace) == workspace / "acp-chat.sock"


# ── HTTP: POST /chat/<op> ───────────────────────────────────────────────────


def _post(bridge, path, body=None):
    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = path
    raw = json.dumps(body).encode() if body is not None else b""
    handler.rfile = BytesIO(raw)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.do_POST()
    status = handler.send_response.call_args[0][0]
    return status, json.loads(handler.wfile.getvalue())


def test_chat_prompt_forwards_to_the_session(bridge, monkeypatch):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))

    status, payload = _post(bridge, "/chat/prompt", {"text": "hallo"})

    assert status == 200
    assert payload == {"ok": True, "turn": 1}
    assert session.prompts == ["hallo"]


def test_chat_config_and_cancel_reach_the_session(bridge, monkeypatch):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    daemon = _daemon(bridge, session)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: daemon)

    assert _post(bridge, "/chat/config", {"id": "thinking", "value": "high"})[0] == 200
    assert _post(bridge, "/chat/cancel")[0] == 200
    status, payload = _post(bridge, "/chat/state")
    assert status == 200 and payload["ok"] is True and payload["driver"] == "hermes"
    assert session.configs == [("thinking", "high")]
    assert session.cancelled == 1


def test_chat_prompt_while_busy_is_409(bridge, monkeypatch):
    """`busy` is an ANSWER, not a transport failure — the backend's
    HttpCtlTransport reads the JSON body on anything below 500."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession(busy=True)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))

    status, payload = _post(bridge, "/chat/prompt", {"text": "zweite"})

    assert status == 409
    assert payload == {"ok": False, "error": "busy"}


def test_chat_without_running_daemon_is_502(bridge, monkeypatch):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    daemon = bridge.hermes_acp_chat.ChatDaemon(lambda: FakeSession())  # never started
    monkeypatch.setattr(bridge, "chat_daemon", lambda: daemon)

    status, payload = _post(bridge, "/chat/prompt", {"text": "hallo"})

    assert status == 502
    assert payload["ok"] is False


def test_chat_is_502_while_the_driver_is_native(bridge, monkeypatch):
    """No ACP daemon exists on the native path — say so instead of pretending."""
    status, payload = _post(bridge, "/chat/prompt", {"text": "hallo"})
    assert status == 502
    assert payload["ok"] is False


def test_health_reports_driver_and_daemon(bridge, monkeypatch):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)

    handler = bridge.Handler.__new__(bridge.Handler)
    handler.path = "/health"
    handler.wfile = BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.do_GET()
    payload = json.loads(handler.wfile.getvalue())

    assert payload["driver"] == "acp"
    assert payload["chat_daemon_running"] is True
    assert payload["tmux_running"] is False  # no TUI under the ACP driver


def test_start_under_acp_starts_the_daemon_not_tmux(bridge, monkeypatch, tmp_path):
    """No double brain: the TUI stays down when the chat daemon drives."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_AGENT_TOKEN=abc\n")
    monkeypatch.setattr(bridge, "ENV_FILE", env_file)
    tmux = MagicMock()
    monkeypatch.setattr(bridge._sp, "run", tmux)
    monkeypatch.setattr(bridge._sp, "Popen", tmux)
    session = FakeSession()
    daemon = bridge.hermes_acp_chat.ChatDaemon(lambda: session)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: daemon)

    result = bridge.start_hermes_session()

    assert session.started == 1
    assert result["status"] == "started"
    assert tmux.call_count == 0, "the ACP driver must NEVER spawn the tmux TUI"
    assert bridge.start_hermes_session()["status"] == "already_running"


# ── task dispatch ───────────────────────────────────────────────────────────


class _StopLoop(Exception):
    """Ends dispatch_poll_loop after exactly one iteration."""


def _run_one_poll(bridge, monkeypatch, tmp_path, payload):
    env_file = tmp_path / "agent.env"
    env_file.write_text("MC_BASE_URL=http://localhost\nMC_AGENT_TOKEN=secret\n")
    monkeypatch.setattr(bridge, "ENV_FILE", env_file)
    monkeypatch.setattr(bridge, "agent_running", lambda: True)
    monkeypatch.setattr(bridge, "is_session_running", lambda: True)

    body = json.dumps(payload).encode()

    def fake_urlopen(req, timeout=10):
        m = MagicMock()
        m.read.return_value = body
        m.__enter__ = lambda self: m
        m.__exit__ = lambda self, *a: None
        return m

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def stop(*_a, **_kw):
        raise _StopLoop()

    # dispatch_poll_loop's end-of-iteration wait is _shutdown_event.wait(...),
    # not time.sleep() (13.09.2026 SIGTERM-shutdown fix) — that's the call
    # that must raise to end the loop after one iteration.
    monkeypatch.setattr(bridge._shutdown_event, "wait", stop)
    with pytest.raises(_StopLoop):
        bridge.dispatch_poll_loop()


_TASK_PAYLOAD = {
    "state": "new_task",
    "task": {"id": "task-1", "board_id": "b1", "title": "T", "prompt": "do it"},
}


def test_dispatch_under_acp_goes_through_the_chat_session(
    bridge, monkeypatch, tmp_path
):
    """The operator must SEE the work: the dispatch prompt travels the same
    ACP session the chat uses, and the loop waits for the turn to end before
    polling for the next task."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))
    tmux = MagicMock()
    monkeypatch.setattr(bridge, "_send_to_tmux", tmux)

    _run_one_poll(bridge, monkeypatch, tmp_path, _TASK_PAYLOAD)

    assert tmux.call_count == 0, "ACP dispatch must not type into tmux"
    assert len(session.prompts) == 1
    assert "task-1" in session.prompts[0] and "do it" in session.prompts[0]
    assert session.waited, "the loop must wait for the turn to end"
    assert bridge._last_dispatched_task_id == "task-1"


def test_dispatch_under_acp_waits_for_a_running_turn_instead_of_knocking(
    bridge, monkeypatch, tmp_path
):
    """Live 13.09.2026: while a chat turn ran, the loop offered the task every
    poll (5 s) and every refusal wrote a red `busy` card into the operator's
    chat — six of them for one reply. The daemon knows when the turn ends
    (wait_idle); the loop must wait there, then deliver exactly once."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession(busy=True)  # a chat turn is running; ends on wait
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))

    _run_one_poll(bridge, monkeypatch, tmp_path, _TASK_PAYLOAD)

    assert session.waited, "the loop must wait for the running turn first"
    assert len(session.prompts) == 1, "delivered once, after the turn — no refused knock"
    assert bridge._last_dispatched_task_id == "task-1"


def test_dispatch_under_acp_stays_redeliverable_when_the_turn_never_ends(
    bridge, monkeypatch, tmp_path
):
    """A turn that outlives the wait is NOT a dispatch: no prompt is sent
    (no `busy` card), the task stays on the board for the next poll."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession(busy=True)
    session.idle_after_wait = False
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))

    _run_one_poll(bridge, monkeypatch, tmp_path, _TASK_PAYLOAD)

    assert session.waited
    assert session.prompts == [], "a busy daemon is never knocked on"
    assert bridge._last_dispatched_task_id is None


def test_comments_under_acp_go_through_the_chat_session(
    bridge, monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, session))
    tmux = MagicMock()
    monkeypatch.setattr(bridge, "_send_to_tmux", tmux)

    _run_one_poll(bridge, monkeypatch, tmp_path, {
        "state": "idle",
        "new_comments": [
            {"source": "user", "task_id": "t1", "task_title": "T", "content": "hi"}
        ],
    })

    assert tmux.call_count == 0
    assert len(session.prompts) == 1 and "hi" in session.prompts[0]


def test_native_dispatch_is_untouched(bridge, monkeypatch, tmp_path):
    """SABOTAGE: with the driver unset the loop must behave exactly as before
    — tmux paste, no chat daemon anywhere near it."""
    session = FakeSession()

    def _boom():
        raise AssertionError("native dispatch must never touch the chat daemon")

    monkeypatch.setattr(bridge, "chat_daemon", _boom)
    tmux = MagicMock()
    monkeypatch.setattr(bridge, "_send_to_tmux", tmux)

    _run_one_poll(bridge, monkeypatch, tmp_path, _TASK_PAYLOAD)

    assert tmux.call_count == 1
    assert "task-1" in tmux.call_args[0][0]
    assert session.prompts == []
    assert bridge._last_dispatched_task_id == "task-1"


def test_message_gate_uses_the_daemon_busy_flag_under_acp(bridge, monkeypatch):
    """No pane to watch under ACP — the turn boundary is the daemon's own
    busy flag, and a busy daemon keeps messages queued (same semantics as the
    pane-quiet gate it replaces)."""
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    monkeypatch.setattr(bridge, "_last_dispatched_task_id", None)
    idle = FakeSession()
    busy = FakeSession(busy=True)

    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, idle))
    assert bridge.msg_gate_open() is True
    monkeypatch.setattr(bridge, "chat_daemon", lambda: _daemon(bridge, busy))
    assert bridge.msg_gate_open() is False
    assert bridge.msg_gate_open(dispatch_in_flight=True) is False


# ── the contract, end to end ────────────────────────────────────────────────


async def test_backend_transport_talks_to_a_real_bridge_server(bridge, monkeypatch):
    """The backend's own HttpCtlTransport against a REAL bridge HTTP server.

    Status codes are the whole protocol here — 409 must read as an ANSWER
    (``busy``) and 502 as "nobody there" (AcpChatUnreachableError). A mocked
    transport would happily agree with a wrong contract; this one cannot.
    """
    import http.server
    import threading

    from app.services import acp_chat_transport

    monkeypatch.setenv("HERMES_DRIVER", "acp")
    session = FakeSession()
    daemon = _daemon(bridge, session)
    monkeypatch.setattr(bridge, "chat_daemon", lambda: daemon)

    server = http.server.HTTPServer(("127.0.0.1", 0), bridge.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    transport = acp_chat_transport.HttpCtlTransport(
        f"http://127.0.0.1:{server.server_port}"
    )
    try:
        assert await transport.prompt("hallo") == {"ok": True, "turn": 1}
        assert await transport.cancel() == {"ok": True}
        assert (await transport.config("thinking", "high"))["ok"] is True
        assert (await transport.state())["driver"] == "hermes"

        session._busy = True  # busy is an answer, not a transport failure
        assert await transport.prompt("zweite") == {"ok": False, "error": "busy"}

        daemon.stop()  # daemon gone → the backend must hear "unreachable"
        with pytest.raises(acp_chat_transport.AcpChatUnreachableError):
            await transport.prompt("dritte")
    finally:
        server.shutdown()
        server.server_close()

    assert session.prompts == ["hallo", "zweite"]
