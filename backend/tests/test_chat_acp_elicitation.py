"""Chat package P3 — question cards and plan mode for headless (ACP) agents.

Three seams:
  * the transcript parser (``omp_chat``) turns the chat daemon's lines into
    ``elicitation`` / ``elicitation_result`` / ``mode`` events;
  * ``agent_chat_input.answer_elicitation`` / ``set_mode`` /
    ``mode_capabilities`` speak to the daemon through the transport;
  * the router endpoints with their refusals.

The transport is a double throughout — no ``docker exec``. Slug ``acp-one``
is made up.
"""
from __future__ import annotations

import json

import pytest

from app.services import omp_chat


class _StubAgent:
    def __init__(self, slug="acp-one", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness
        self.id = slug


class _Transport:
    def __init__(self, answers=None):
        self.calls: list[tuple] = []
        self._answers = answers or {}

    async def answer(self, id, action, content=None):
        self.calls.append(("answer", id, action, content))
        return self._answers.get("answer", {"ok": True, "status": "answered"})

    async def config(self, id, value):
        self.calls.append(("config", id, value))
        return self._answers.get("config", {"ok": True})


SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["Approve and execute", "Refine plan"]}},
    "required": ["value"],
}

MODE_STATE = {
    "configOptions": [
        {"id": "mode", "currentValue": "default",
         "options": [{"value": "default", "name": "Default"}, {"value": "plan", "name": "Plan"}]},
    ]
}


@pytest.fixture
def acp_agent():
    return _StubAgent()


def _install(monkeypatch, transport, state=None):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: transport)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: state)
    return transport


def _parse(line: dict) -> list[dict]:
    return omp_chat.OmpLineParser()(json.dumps(line))


# ══════════════════════════════════════════════════════════════════════════
# parser
# ══════════════════════════════════════════════════════════════════════════


def test_elicitation_line_becomes_a_card_event():
    line = {
        "type": "custom_message", "customType": "acp-elicitation",
        "content": 'Approve plan "Login"?', "display": True, "attribution": "agent",
        "id": "acp00010", "parentId": None, "timestamp": "2026-10-13T10:00:00Z",
        "data": {"elicitationId": "el-0123456789ab", "message": 'Approve plan "Login"?',
                 "schema": SCHEMA},
    }
    assert _parse(line) == [{
        "kind": "elicitation", "uuid": "acp00010", "ts": "2026-10-13T10:00:00Z",
        "elicitationId": "el-0123456789ab", "message": 'Approve plan "Login"?',
        "schema": SCHEMA, "planApproval": False,
    }]


def test_elicitation_without_schema_stays_a_visible_notice():
    """Never lose the question: a broken card falls back to the plain
    system notice an older frontend would show anyway."""
    line = {
        "type": "custom_message", "customType": "acp-elicitation",
        "content": "Which one?", "display": True,
        "id": "acp00011", "parentId": None, "timestamp": "2026-10-13T10:00:00Z",
        "data": {"elicitationId": "el-0123456789ab"},
    }
    events = _parse(line)
    assert len(events) == 1
    assert events[0]["kind"] == "message" and events[0]["text"] == "Which one?"


def test_result_and_mode_lines():
    result = {"type": "custom", "customType": "acp-elicitation-result",
              "id": "acp00012", "parentId": None, "timestamp": "2026-10-13T10:01:00Z",
              "data": {"elicitationId": "el-0123456789ab", "status": "answered",
                       "content": {"value": "Refine plan"}}}
    assert _parse(result) == [{
        "kind": "elicitation_result", "uuid": "acp00012", "ts": "2026-10-13T10:01:00Z",
        "elicitationId": "el-0123456789ab", "status": "answered",
        "content": {"value": "Refine plan"},
    }]
    expired = {**result, "data": {"elicitationId": "el-0123456789ab", "status": "expired"}}
    assert _parse(expired)[0]["status"] == "expired"
    assert _parse({**result, "data": {"elicitationId": "x", "status": "weird"}}) == []

    mode = {"type": "custom", "customType": "acp-mode", "id": "acp00013",
            "parentId": None, "timestamp": "2026-10-13T10:02:00Z", "data": {"modeId": "plan"}}
    assert _parse(mode) == [{"kind": "mode", "uuid": "acp00013",
                             "ts": "2026-10-13T10:02:00Z", "modeId": "plan"}]


# ══════════════════════════════════════════════════════════════════════════
# service
# ══════════════════════════════════════════════════════════════════════════


async def test_answer_goes_through_the_transport(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    t = _install(monkeypatch, _Transport())
    outcome = await agent_chat_input.answer_elicitation(
        acp_agent, "el-0123456789ab", "accept", {"value": "Refine plan"})
    assert outcome == {"status": "answered", "stopped": False}
    assert t.calls == [("answer", "el-0123456789ab", "accept", {"value": "Refine plan"})]


async def test_cancel_sends_no_content(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    t = _install(monkeypatch, _Transport({"answer": {"ok": True, "status": "cancelled"}}))
    assert await agent_chat_input.answer_elicitation(
        acp_agent, "el-0123456789ab", "cancel", {"value": "x"}) == {"status": "cancelled", "stopped": False}
    assert t.calls == [("answer", "el-0123456789ab", "cancel", None)]


async def test_answer_refusals(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _install(monkeypatch, _Transport({"answer": {"ok": False, "error": "not_pending"}}))
    with pytest.raises(agent_chat_input.ElicitationNotPendingError):
        await agent_chat_input.answer_elicitation(acp_agent, "el-0123456789ab", "accept", {})

    _install(monkeypatch, _Transport({"answer": {"ok": False, "error": "bad_content",
                                                 "detail": "'value' is required"}}))
    with pytest.raises(ValueError, match="required"):
        await agent_chat_input.answer_elicitation(acp_agent, "el-0123456789ab", "accept", {})

    _install(monkeypatch, _Transport({"answer": {"ok": False, "error": "unknown_op"}}))
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.answer_elicitation(acp_agent, "el-0123456789ab", "decline")

    t = _install(monkeypatch, _Transport())
    with pytest.raises(ValueError):
        await agent_chat_input.answer_elicitation(acp_agent, "el-0123456789ab", "maybe")
    with pytest.raises(ValueError):
        await agent_chat_input.answer_elicitation(acp_agent, "el-0123456789ab", "accept", None)
    assert t.calls == []


async def test_answer_is_not_supported_on_a_tui_agent(monkeypatch):
    from app.services import agent_chat_input

    t = _install(monkeypatch, _Transport())
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.answer_elicitation(
            _StubAgent(harness="claude"), "el-0123456789ab", "cancel")
    assert t.calls == []


async def test_mode_capabilities_and_switch(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    t = _install(monkeypatch, _Transport(), MODE_STATE)
    assert await agent_chat_input.mode_capabilities(acp_agent) == {
        "modeOptions": [{"value": "default", "name": "Default"},
                        {"value": "plan", "name": "Plan"}],
        "mode": "default",
    }
    await agent_chat_input.set_mode(acp_agent, "plan")
    assert t.calls == [("config", "mode", "plan")]
    with pytest.raises(ValueError):
        await agent_chat_input.set_mode(acp_agent, "yolo")


async def test_no_switch_without_two_reported_modes(acp_agent, monkeypatch):
    """omp reports only `default` while plan mode is off in its settings —
    a one-position switch is useless, so there is none."""
    from app.services import agent_chat_input

    only_default = {"configOptions": [{"id": "mode", "currentValue": "default",
                                       "options": [{"value": "default", "name": "Default"}]}]}
    t = _install(monkeypatch, _Transport(), only_default)
    assert await agent_chat_input.mode_capabilities(acp_agent) == {"modeOptions": [], "mode": None}
    with pytest.raises(agent_chat_input.InputNotSupportedError):
        await agent_chat_input.set_mode(acp_agent, "default")
    _install(monkeypatch, t, None)
    assert await agent_chat_input.mode_capabilities(acp_agent) == {"modeOptions": [], "mode": None}
    assert await agent_chat_input.mode_capabilities(_StubAgent(harness="claude")) == {
        "modeOptions": [], "mode": None}
    assert t.calls == []


async def test_refused_mode_switch_raises(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _install(monkeypatch, _Transport({"config": {"ok": False, "error": "rpc_error"}}), MODE_STATE)
    with pytest.raises(agent_chat_input.AcpChatRefusedError):
        await agent_chat_input.set_mode(acp_agent, "plan")


# ══════════════════════════════════════════════════════════════════════════
# router
# ══════════════════════════════════════════════════════════════════════════


async def test_router_answers_a_card(auth_client, make_agent, monkeypatch):
    import app.routers.agent_chat as mod

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")
    seen = []

    async def _answer(a, eid, action, content):
        seen.append((eid, action, content))
        return {"status": "answered", "stopped": False}

    monkeypatch.setattr(mod, "answer_elicitation", _answer)
    resp = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/elicitation/el-0123456789ab",
        json={"action": "accept", "content": {"value": "Refine plan"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "answered", "stopped": False}
    assert seen == [("el-0123456789ab", "accept", {"value": "Refine plan"})]


async def test_router_card_refusals(auth_client, make_agent, monkeypatch):
    import app.routers.agent_chat as mod
    from app.services.acp_chat_transport import AcpChatUnreachableError
    from app.services.agent_chat_input import ElicitationNotPendingError

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")
    url = f"/api/v1/agents/{agent.id}/chat/elicitation/el-0123456789ab"

    async def _gone(*a):
        raise ElicitationNotPendingError()

    monkeypatch.setattr(mod, "answer_elicitation", _gone)
    resp = await auth_client.post(url, json={"action": "cancel"})
    assert resp.status_code == 409 and resp.json() == {"reason": "elicitation_not_pending"}

    async def _bad(*a):
        raise ValueError("'value' is required")

    monkeypatch.setattr(mod, "answer_elicitation", _bad)
    assert (await auth_client.post(url, json={"action": "accept", "content": {}})).status_code == 422

    async def _dead(*a):
        raise AcpChatUnreachableError("socket weg")

    monkeypatch.setattr(mod, "answer_elicitation", _dead)
    resp = await auth_client.post(url, json={"action": "cancel"})
    assert resp.status_code == 502 and resp.json()["reason"] == "acp_unreachable"

    # the id travels into a docker exec argument — shape-checked first
    bad_id = f"/api/v1/agents/{agent.id}/chat/elicitation/el-x;rm"
    assert (await auth_client.post(bad_id, json={"action": "cancel"})).status_code == 422
    huge = {"action": "accept", "content": {"value": "x" * 40_000}}
    assert (await auth_client.post(url, json=huge)).status_code == 422


async def test_router_card_needs_the_operator(client, make_agent):
    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")
    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/elicitation/el-0123456789ab",
        json={"action": "cancel"},
    )
    assert resp.status_code in (401, 403)
    resp = await client.post(f"/api/v1/agents/{agent.id}/chat/mode", json={"mode": "plan"})
    assert resp.status_code in (401, 403)


async def test_router_mode_switch(auth_client, make_agent, monkeypatch):
    import app.routers.agent_chat as mod
    from app.services.agent_chat_input import InputNotSupportedError

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")
    seen = []

    async def _set(a, mode):
        seen.append(mode)

    monkeypatch.setattr(mod, "set_mode", _set)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/mode", json={"mode": "plan"})
    assert resp.status_code == 204, resp.text
    assert seen == ["plan"]

    async def _none(a, mode):
        raise InputNotSupportedError()

    monkeypatch.setattr(mod, "set_mode", _none)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/mode", json={"mode": "plan"})
    assert resp.status_code == 409 and resp.json()["reason"] == "input_not_supported"


# ══════════════════════════════════════════════════════════════════════════
# round 3 (live 13.10.2026)
# ══════════════════════════════════════════════════════════════════════════


def test_plan_approval_flag_reaches_the_card_event():
    line = {
        "type": "custom_message", "customType": "acp-elicitation",
        "content": 'Approve plan "Login"?', "display": True,
        "id": "acp00020", "parentId": None, "timestamp": "2026-10-13T10:00:00Z",
        "data": {"elicitationId": "el-0123456789ab", "message": 'Approve plan "Login"?',
                 "schema": SCHEMA, "planApproval": True},
    }
    assert _parse(line)[0]["planApproval"] is True
    line["data"]["planApproval"] = "yes"  # only a real boolean counts
    assert _parse(line)[0]["planApproval"] is False


async def test_cancel_on_plan_card_reports_the_stop(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _install(monkeypatch, _Transport({"answer": {"ok": True, "status": "cancelled", "stopped": True}}))
    assert await agent_chat_input.answer_elicitation(
        acp_agent, "el-0123456789ab", "cancel") == {"status": "cancelled", "stopped": True}


async def test_mode_switch_refused_mid_turn_and_with_open_question(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _install(monkeypatch, _Transport({"config": {"ok": False, "error": "question_open"}}), MODE_STATE)
    with pytest.raises(agent_chat_input.QuestionOpenError):
        await agent_chat_input.set_mode(acp_agent, "default")
    _install(monkeypatch, _Transport({"config": {"ok": False, "error": "busy"}}), MODE_STATE)
    with pytest.raises(agent_chat_input.AgentBusyError):
        await agent_chat_input.set_mode(acp_agent, "default")


async def test_router_mode_refusals_are_409_with_reason(auth_client, make_agent, monkeypatch):
    import app.routers.agent_chat as mod
    from app.services.agent_chat_input import AgentBusyError, QuestionOpenError

    agent = await make_agent(name="Acp One", agent_runtime="cli-bridge")
    url = f"/api/v1/agents/{agent.id}/chat/mode"

    async def _open(a, mode):
        raise QuestionOpenError()

    monkeypatch.setattr(mod, "set_mode", _open)
    resp = await auth_client.post(url, json={"mode": "default"})
    assert resp.status_code == 409 and resp.json() == {"reason": "question_open"}

    async def _busy(a, mode):
        raise AgentBusyError()

    monkeypatch.setattr(mod, "set_mode", _busy)
    resp = await auth_client.post(url, json={"mode": "default"})
    assert resp.status_code == 409 and resp.json() == {"reason": "agent_busy"}


def test_open_question_reads_as_waiting_for_the_operator(monkeypatch):
    from app.services import acp_chat_transport as t

    state = {"busy": True, "elicitations": [{"id": "el-0123456789ab"}]}
    monkeypatch.setattr(t, "read_acp_chat_state", lambda agent: state)
    assert t.read_acp_turn_status(object(), with_questions=True) == "waiting_input"
    # other callers (voice stop) keep the plain answer
    assert t.read_acp_turn_status(object()) == "working"
    state["elicitations"] = []
    assert t.read_acp_turn_status(object(), with_questions=True) == "working"


async def test_chat_status_line_uses_the_question_aware_state(monkeypatch):
    import app.services.transcript_chat as tc

    seen = {}

    def _status(agent, *, with_questions=False, with_task=False):
        seen["with_questions"] = with_questions
        # The chat tailer also reads the task channel (follow-up to ADR-097).
        seen["with_task"] = with_task
        return "waiting_input"

    monkeypatch.setattr(tc, "read_acp_turn_status", _status)

    async def _alive(agent, path, adapter=None):
        return "active"

    monkeypatch.setattr(tc, "resolve_aliveness", _alive)
    agent = _StubAgent()
    session = __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "s.jsonl"
    session.write_text("{}\n")
    state = await tc.ChatTailerManager()._compute_pane_state(agent, session)
    assert state["status"] == "waiting_input"
    assert seen["with_questions"] is True
    assert seen["with_task"] is True
