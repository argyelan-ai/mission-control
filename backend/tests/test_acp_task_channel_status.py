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
and the chat daemon's own busy turn still wins. The chat tailer then asks MC
about the model (``refine_status_by_model``, tests at the end).
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


# ── The model does not answer: the chat says so (review Z2 / Z5, 20.10.2026) ──
#
# The bridge's phase is not the only truth about a task that waits for its
# model. Once omp gives up on a dead endpoint, the card stays delivered; MC's
# ACK ladder returns it for the model and the poll gate then holds it in the
# inbox — while the chat said "Ready" and the task card "waiting for the
# model". And after the claim, an engine that died keeps the phase "working"
# for the whole retry time. The chat tailer therefore also asks MC itself
# (the same cached readiness the poll uses, fail-open): an assigned inbox
# card + a model that does not answer -> waiting_model; a running task turn +
# a model that does not answer -> waiting_model.

import uuid as _uuid
from unittest.mock import AsyncMock, patch


@pytest.fixture
def _db_sessions(monkeypatch):
    """Code that opens its own session (``async_session_maker``) uses the
    test DB."""
    import app.database
    from sqlmodel.ext.asyncio.session import AsyncSession

    from tests.conftest import test_engine

    monkeypatch.setattr(app.database, "async_session_maker",
                        lambda: AsyncSession(test_engine, expire_on_commit=False))
    from app.services import acp_chat_transport

    acp_chat_transport._model_wait_cache.clear()
    yield
    acp_chat_transport._model_wait_cache.clear()


async def _agent_on_local_runtime(make_agent):
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.models.host import Host
    from app.models.runtime import Runtime
    from tests.conftest import test_engine

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        h = Host(slug=f"box-{_uuid.uuid4().hex[:6]}", display_name="Box", kind="ssh")
        s.add(h)
        await s.flush()
        rt = Runtime(slug=f"tc-{_uuid.uuid4().hex[:6]}", display_name="BOX-A :8000",
                     runtime_type="openai_compatible",
                     endpoint="http://192.0.2.10:8000/v1", host_id=h.id)
        s.add(rt)
        await s.commit()
    return await make_agent(name="ACP One", slug="acp-one", agent_runtime="cli-bridge",
                            harness="omp", runtime_id=rt.id)


async def _pane_status(agent, tmp_path, *, serving: bool) -> str:
    import app.services.transcript_chat as tc
    from app.services import runtime_readiness

    async def _alive(agent, path, adapter=None):
        return "active"

    session = tmp_path / "s.jsonl"
    session.write_text("{}\n")
    with patch.object(tc, "resolve_aliveness", _alive), \
         patch.object(runtime_readiness, "is_local_runtime_serving",
                      new=AsyncMock(return_value=serving)), \
         patch.object(runtime_readiness, "_notify_local_down", new=AsyncMock()) as note:
        state = await tc.ChatTailerManager()._compute_pane_state(agent, session)
    note.assert_not_called()  # a read never writes the timeline
    return state["status"]


@pytest.mark.asyncio
async def test_a_held_card_whose_model_does_not_answer_is_waiting_not_ready(
    root, tmp_path, _db_sessions, make_agent, make_board, make_task,
):
    """The 19.10. case after omp gave up: phase idle (or gone), chat free,
    the card returned / held in the inbox, the model off."""
    agent = await _agent_on_local_runtime(make_agent)
    board = await make_board(slug=f"tc-{_uuid.uuid4().hex[:6]}")
    await make_task(board.id, title="Held", status="inbox", assigned_agent_id=agent.id)
    _chat(root, busy=False)
    _task(root, "idle")
    assert await _pane_status(agent, tmp_path, serving=False) == "waiting_model"


@pytest.mark.asyncio
async def test_a_held_card_with_an_answering_model_is_ready(
    root, tmp_path, _db_sessions, make_agent, make_board, make_task,
):
    agent = await _agent_on_local_runtime(make_agent)
    board = await make_board(slug=f"tc-{_uuid.uuid4().hex[:6]}")
    await make_task(board.id, title="Next", status="inbox", assigned_agent_id=agent.id)
    _chat(root, busy=False)
    assert await _pane_status(agent, tmp_path, serving=True) == "idle"


@pytest.mark.asyncio
async def test_no_card_waiting_means_ready_even_with_the_model_off(
    root, tmp_path, _db_sessions, make_agent, make_board, make_task,
):
    """Nothing waits for the model: the chat itself is free ("Ready"). A
    card a lead holds (run_control) never starts by itself — no wait either."""
    agent = await _agent_on_local_runtime(make_agent)
    board = await make_board(slug=f"tc-{_uuid.uuid4().hex[:6]}")
    await make_task(board.id, title="Done", status="done", assigned_agent_id=agent.id)
    await make_task(board.id, title="Held by the lead", status="inbox",
                    assigned_agent_id=agent.id, run_control="manual_hold")
    await make_task(board.id, title="Someone else's", status="inbox")
    _chat(root, busy=False)
    assert await _pane_status(agent, tmp_path, serving=False) == "idle"


@pytest.mark.asyncio
async def test_a_task_turn_whose_model_went_away_is_waiting_for_the_model(
    root, tmp_path, _db_sessions, make_agent,
):
    """Engine died after the claim (task 43788774): omp retries for minutes,
    the phase stays "working" — the chat says the model does not answer."""
    agent = await _agent_on_local_runtime(make_agent)
    _chat(root, busy=False)
    _task(root, "working")
    assert await _pane_status(agent, tmp_path, serving=False) == "waiting_model"


@pytest.mark.asyncio
async def test_a_task_turn_with_an_answering_model_stays_task_working(
    root, tmp_path, _db_sessions, make_agent,
):
    agent = await _agent_on_local_runtime(make_agent)
    _chat(root, busy=False)
    _task(root, "working")
    assert await _pane_status(agent, tmp_path, serving=True) == "task_working"


@pytest.mark.asyncio
async def test_a_busy_chat_turn_is_never_rewritten(
    root, tmp_path, _db_sessions, make_agent, make_board, make_task,
):
    agent = await _agent_on_local_runtime(make_agent)
    board = await make_board(slug=f"tc-{_uuid.uuid4().hex[:6]}")
    await make_task(board.id, title="Held", status="inbox", assigned_agent_id=agent.id)
    _chat(root, busy=True)
    assert await _pane_status(agent, tmp_path, serving=False) == "working"


@pytest.mark.asyncio
async def test_an_agent_without_a_runtime_never_touches_the_db(root, tmp_path, monkeypatch):
    """Cloud / unbound agents: nothing to wait for — and no query per probe."""
    import app.database

    def _boom():
        raise AssertionError("no DB read for an agent without a runtime")

    monkeypatch.setattr(app.database, "async_session_maker", _boom)
    agent = _StubAgent()
    agent.id = _uuid.uuid4()
    agent.runtime_id = None
    _chat(root, busy=False)
    assert await _pane_status(agent, tmp_path, serving=False) == "idle"


@pytest.mark.asyncio
async def test_the_model_facts_are_cached_per_agent(
    root, tmp_path, _db_sessions, make_agent, make_board, make_task,
):
    """The tailer probes every ~2 s per open chat: one DB read + readiness
    per agent per few seconds, not per probe."""
    from app.services import acp_chat_transport

    agent = await _agent_on_local_runtime(make_agent)
    board = await make_board(slug=f"tc-{_uuid.uuid4().hex[:6]}")
    await make_task(board.id, title="Held", status="inbox", assigned_agent_id=agent.id)
    _chat(root, busy=False)
    real = acp_chat_transport._model_wait_facts
    calls = []

    async def _count(a):
        calls.append(1)
        return await real(a)

    with patch.object(acp_chat_transport, "_model_wait_facts", _count):
        for _ in range(3):
            assert await _pane_status(agent, tmp_path, serving=False) == "waiting_model"
    assert len(calls) == 1, calls


@pytest.mark.asyncio
async def test_a_db_error_keeps_the_phase_answer(root, tmp_path, monkeypatch, make_agent):
    """Fail-safe: a broken read never invents a state."""
    import app.database
    from app.services import acp_chat_transport

    acp_chat_transport._model_wait_cache.clear()
    agent = await _agent_on_local_runtime(make_agent)

    def _broken():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(app.database, "async_session_maker", _broken)
    _chat(root, busy=False)
    _task(root, "working")
    assert await _pane_status(agent, tmp_path, serving=False) == "task_working"
