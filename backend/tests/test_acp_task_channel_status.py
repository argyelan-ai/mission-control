"""The agent chat says what the TASK channel does (follow-up to ADR-097).

Live finding: while an ACP agent ran a task, its chat said "Ready". The
chat's only state source was the chat daemon's ``busy`` flag, and tasks run
through the bridge's own ACP client, not through the chat daemon.

The bridge now writes ``acp-task-state.json`` at the root of the agent's omp
sessions tree (``docker/omp-bridge/bridge.py::TaskStateFile``):
``delivered`` (picked up, the model has not answered), ``working`` (the model
answered) or ``idle``. The heartbeater re-stamps ``updatedAt`` while a run
lasts; a stale phase (crashed bridge) is ignored.

The chat (``read_acp_turn_status(..., with_task=True)``) shows
  * ``task_working`` for a running task turn (the chat itself stays free),
  * ``waiting_model`` for a delivered task whose model has not answered,
and the chat daemon's own busy turn still wins.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


class _StubAgent:
    def __init__(self, slug="acp-one", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness


def _iso(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=delta_s)).isoformat()


@pytest.fixture
def root(tmp_path, monkeypatch):
    from app.services import omp_chat

    monkeypatch.setattr(omp_chat, "_host_home", lambda: tmp_path)
    r = tmp_path / ".mc" / "agents" / "acp-one" / "omp-sessions"
    (r / "--workspace--").mkdir(parents=True)
    return r


def _chat(root, busy: bool) -> None:
    (root / "--workspace--" / "acp-chat-state.json").write_text(json.dumps({"busy": busy}))


def _task(root, phase: str, age_s: float = 0.0) -> None:
    (root / "acp-task-state.json").write_text(json.dumps({
        "version": 1, "phase": phase, "taskId": "T1",
        "since": _iso(age_s), "updatedAt": _iso(age_s),
    }))


def _status(**kw):
    from app.services.acp_chat_transport import read_acp_turn_status

    return read_acp_turn_status(_StubAgent(), with_questions=True, **kw)


def test_a_running_task_turn_shows_task_working_while_the_chat_is_idle(root):
    """task_working, not working: the chat itself is free — no Stop button,
    no "queued" echo for a message sent now."""
    _chat(root, busy=False)
    _task(root, "working")
    assert _status(with_task=True) == "task_working"


def test_a_delivered_task_shows_waiting_for_the_model(root):
    _chat(root, busy=False)
    _task(root, "delivered")
    assert _status(with_task=True) == "waiting_model"


def test_a_task_state_without_a_chat_state_still_counts(root):
    _task(root, "working")
    assert _status(with_task=True) == "task_working"


def test_the_chat_turn_wins_over_the_task_channel(root):
    _chat(root, busy=True)
    _task(root, "delivered")
    assert _status(with_task=True) == "working"


def test_a_stale_task_phase_is_ignored(root):
    """A crashed bridge never writes idle — its last phase must age out."""
    _chat(root, busy=False)
    _task(root, "working", age_s=600)
    assert _status(with_task=True) == "idle"


def test_idle_task_channel_is_idle(root):
    _chat(root, busy=False)
    _task(root, "idle")
    assert _status(with_task=True) == "idle"


def test_callers_without_the_opt_in_keep_the_old_answer(root):
    """Voice stop and other callers read only the chat daemon, as before."""
    _chat(root, busy=False)
    _task(root, "working")
    assert _status() == "idle"


def test_garbage_task_state_is_ignored(root):
    _chat(root, busy=False)
    (root / "acp-task-state.json").write_text("{not json")
    assert _status(with_task=True) == "idle"


def test_a_future_stamp_is_not_trusted_forever(root):
    """A clock jump must not pin a phase: only fresh-in-the-past counts."""
    _chat(root, busy=False)
    _task(root, "working", age_s=-3600)
    assert _status(with_task=True) == "idle"


def test_the_busy_check_for_file_writes_counts_a_task_turn(monkeypatch):
    """Rewind/revert in the chat must not race a TASK turn's edits either."""
    import uuid

    from app.routers import agent_chat

    aid = uuid.uuid4()
    monkeypatch.setattr(agent_chat.tailer_manager, "last_state",
                        lambda _a: {"status": "task_working"})
    assert agent_chat._agent_busy(aid) is True


def test_voice_reads_the_task_states_as_words():
    from app.services.voice_live_tools import build_status

    assert build_status({"status": "task_working"}, None, [])["state"] == "working"
    assert build_status({"status": "waiting_model"}, None, [])["state"] == "waiting_for_model"
