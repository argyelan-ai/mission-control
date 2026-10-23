#!/usr/bin/env python3
"""The chat turn knows about the agent's own task run — and says it is the chat
(round-1 user test t1, MUST-1, 22.10.2026).

The omp agent's chat runs in this daemon; its tasks run in OTHER sessions the
bridge opens. Asked "how far are you?" during a run, the chat session took
the task for "not started" and did it a second time. Contract here:

* a `prompt` may carry `context` (MC's run-context block, backend
  ``chat_run_context``): the model gets it in front of the operator's text,
  the chat transcript (the operator's bubble) gets the operator's text ONLY;
* without `context` the model gets the text byte-identical to before;
* a queued prompt keeps its context until it runs, and the state mirror the
  backend reads never carries it;
* the socket op passes `context` through;
* the `omp acp` child of the chat runs with ``MC_SESSION_ROLE=chat`` (the
  ``mc`` CLI sends it as ``X-MC-Session-Role``; the server refuses run-state
  writes from it) and its own ``MC_CONTEXT_ENV_PATH`` — it never reads the
  run's task context file, so it never holds the run's attempt id.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat  # noqa: E402
import acp_client  # noqa: E402
from test_acp_chat import (  # noqa: E402
    SESSION_RESULT,
    FakeClient,
    make_session,
    read_lines,
    user_texts,
)

CONTEXT = "[Mission Control context]\nTask X runs in its own session.\n[End of context]"


def _prompts(client: FakeClient) -> list[str]:
    return [c[2] for c in client.calls if c[0] == "prompt"]


def test_context_reaches_the_model_but_not_the_bubble(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    assert sess.prompt("Wie weit bist du?", origin="operator", context=CONTEXT)["ok"] is True
    assert sess.wait_idle(timeout=5)
    sess.close()

    assert _prompts(client) == [f"{CONTEXT}\n\nWie weit bist du?"]
    assert user_texts(read_lines(sess.transcript_path)) == ["Wie weit bist du?"]


def test_without_context_the_model_gets_the_text_unchanged(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("hi", origin="operator")
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert _prompts(client) == ["hi"]


def test_a_queued_prompt_keeps_its_context_and_the_mirror_never_shows_it(tmp_path):
    gate = threading.Event()
    client = FakeClient(session_result=SESSION_RESULT,
                        turns=[{"gate": gate, "chunks": ["one"]}, {"chunks": ["two"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    sess.prompt("first", origin="operator")
    queued = sess.prompt("second", mode="queue", origin="operator", context=CONTEXT)
    assert queued.get("queued") is True
    mirror = sess.state()
    assert mirror["queue"] and all("context" not in job for job in mirror["queue"])
    assert CONTEXT not in str(mirror)
    gate.set()
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert _prompts(client) == ["first", f"{CONTEXT}\n\nsecond"]
    assert user_texts(read_lines(sess.transcript_path)) == ["first", "second"]


def test_the_socket_op_passes_context_through(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    answer = acp_chat.dispatch(sess, {"op": "prompt", "text": "status?",
                                      "origin": "operator", "context": CONTEXT})
    assert answer["ok"] is True
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert _prompts(client) == [f"{CONTEXT}\n\nstatus?"]


def test_a_non_text_context_is_ignored(tmp_path):
    client = FakeClient(session_result=SESSION_RESULT, turns=[{"chunks": ["ok"]}])
    sess = make_session(tmp_path, client)
    sess.start()
    acp_chat.dispatch(sess, {"op": "prompt", "text": "hi", "context": {"x": 1}})
    assert sess.wait_idle(timeout=5)
    sess.close()
    assert _prompts(client) == ["hi"]


def test_chat_child_env_marks_the_chat_and_hides_the_runs_context_file():
    base = {"PATH": "/usr/bin", "MC_AGENT_TOKEN": "tok", "X_DISPATCH_ATTEMPT_ID": "stale"}
    env = acp_chat.chat_child_env(base)
    assert env["MC_SESSION_ROLE"] == "chat"
    assert env["MC_CONTEXT_ENV_PATH"] == acp_chat.CHAT_CONTEXT_ENV_PATH
    assert env["MC_CONTEXT_ENV_PATH"] != "/tmp/mc-context.env"
    # The run's ids never leak into the chat's own commands.
    assert "X_DISPATCH_ATTEMPT_ID" not in env
    assert env["PATH"] == "/usr/bin" and env["MC_AGENT_TOKEN"] == "tok"
    assert base == {"PATH": "/usr/bin", "MC_AGENT_TOKEN": "tok", "X_DISPATCH_ATTEMPT_ID": "stale"}


def test_build_session_spawns_the_chat_child_with_the_chat_env(tmp_path, monkeypatch):
    seen: dict = {}

    class _Capture:
        def __init__(self, command=None, cwd=None, env=None):
            seen["cwd"], seen["env"] = cwd, env

    monkeypatch.setattr(acp_client, "ACPClient", _Capture)
    monkeypatch.setattr(acp_chat, "default_cwd", lambda: str(tmp_path / "work"))
    monkeypatch.setattr(acp_chat.acp_model, "current_selector", lambda env=None, **kw: "mc/model")
    monkeypatch.setattr(acp_chat.acp_chat_events, "session_dir", lambda **kw: tmp_path / "s")
    sess = acp_chat.build_session()
    sess._client_factory()
    assert seen["env"]["MC_SESSION_ROLE"] == "chat"
    assert seen["env"]["MC_CONTEXT_ENV_PATH"] == acp_chat.CHAT_CONTEXT_ENV_PATH


def test_comment_wakeup_in_the_chat_says_read_and_report_not_work():
    """Under ACP the comment wake-up lands in the CHAT session (the run's turn
    is over — the gate waits for that). It used to say "then keep working
    right away", which made the chat work the task itself. Now: read, report
    to the operator, do not change the task. The native wording (into the
    run's own pane) is unchanged."""
    import bridge

    chat = bridge.build_comment_nudge_text({"t-1": 2}, chat=True)
    assert "mc task-get t-1" in chat
    assert "arbeite sofort weiter" not in chat
    assert "eigenen Lauf" in chat
    native = bridge.build_comment_nudge_text({"t-1": 2})
    assert native.endswith("dann arbeite sofort weiter.")


def test_acp_delivery_sends_the_chat_wording(tmp_path):
    import bridge
    from test_comment_nudge import TID, _comment
    from test_msg_nudge import _StubTui, _delivery

    sent: list[str] = []
    dv = _delivery(str(tmp_path), _StubTui())
    dv._acp_prompt = lambda text: sent.append(text) or True
    dv.note_comments([_comment()])
    dv.nudge_comments()
    assert len(sent) == 1 and f"mc task-get {TID}" in sent[0]
    assert "arbeite sofort weiter" not in sent[0]


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))


def test_mc_task_get_runs_in_the_chat_childs_env(tmp_path, monkeypatch):
    """Round-2 review r1 S1: the block and the guard's 409 tell the chat to
    read with `mc task-get <id>`. With the chat child's env (no run ids, its
    own empty context file, no BOARD_ID) the real CLI must get through: the
    board comes from the agent's own `/agent/me`."""
    import io
    import os
    from contextlib import redirect_stdout

    repo_root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repo_root / "scripts" / "mc-cli"))
    from mc_cli import commands as mc_commands
    from mc_cli.config import Config

    tid = "d3e397c0-9983-4d6a-b866-af984d5c534e"
    board = "b0a7d000-0000-4000-8000-000000000003"
    monkeypatch.setattr(acp_chat, "CHAT_CONTEXT_ENV_PATH", str(tmp_path / "none.env"))
    boot = {"MC_AGENT_TOKEN": "tok", "MC_API_URL": "http://backend.invalid",
            "TASK_ID": "run-task", "X_DISPATCH_ATTEMPT_ID": "run-attempt"}
    child = acp_chat.chat_child_env(boot)
    monkeypatch.setattr(os, "environ", dict(child))
    cfg = Config.from_env().with_task_id(tid)
    assert cfg.session_role == "chat" and cfg.task_id == tid and not cfg.board_id

    calls: list = []

    class _Client:
        def request(self, method, path, **kw):
            calls.append((method, path))
            if path == "/api/v1/agent/me":
                return {"id": "a1", "board_id": board}
            return {"id": tid, "board_id": board, "status": "in_progress"}

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert mc_commands._cmd_task_get(None, _Client(), cfg) == 0
    assert calls[-1] == ("GET", f"/api/v1/agent/boards/{board}/tasks/{tid}/detail")
    assert '"status": "in_progress"' in buf.getvalue()
