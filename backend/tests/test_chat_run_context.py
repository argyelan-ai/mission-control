"""A chat turn is told what its agent's own task run is doing (round-1 user
test t1, MUST-1, 22.10.2026).

The chat session of an omp agent (ACP chat daemon) is a different session
than its task run. Asked "how far are you?" it did not know the run, did not
find its files (it looked in its own workspace, not ``/deliverables/<id>/``)
and did the task a second time. The day before it answered "0 % — no task is
running" seconds after a run ended. Contract:

* ``chat_run_context.run_context`` names every task a run of this agent is
  working on, or finished within the last 30 minutes (also after a human
  review handoff cleared the assignee): title, id, status, checklist, the
  run's folder, its deliverables, its last note, and the rule "answer, do not
  touch". Nothing to say → ``None``;
* the composer hands that block to the chat daemon as ``context`` — the
  operator's ``text`` stays exactly what was typed (it is their bubble);
* only for ``acp-docker`` (omp: chat and run are different sessions). Hermes
  (``acp-http``) runs tasks inside its chat session — no block there;
* both transports put ``context`` on the wire only when set.

Ids, names and slugs are made up.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 22, 22, 21, 0, tzinfo=timezone.utc)


async def _agent(**kw):
    from app.models.agent import Agent
    from app.models.board import Board

    board = Board(id=uuid.uuid4(), name="Ctx", slug=f"ctx-{uuid.uuid4().hex[:6]}")
    agent = Agent(id=uuid.uuid4(), name="Probe", board_id=board.id,
                  agent_runtime=kw.pop("agent_runtime", "cli-bridge"),
                  harness=kw.pop("harness", "omp"), slug=f"probe-{uuid.uuid4().hex[:6]}", **kw)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(board)
        await s.commit()
        s.add(agent)
        await s.commit()
    return agent


async def _running_task(agent, *, deliverable: bool = True, note: bool = True):
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task, TaskComment, TaskEvent

    task = Task(id=uuid.uuid4(), board_id=agent.board_id, title="[Probe] Mini page",
                status="in_progress", assigned_agent_id=agent.id,
                started_at=NOW - timedelta(minutes=2), checklist_total=4, checklist_done=1)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(task)
        await s.commit()
        s.add(TaskEvent(task_id=task.id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=agent.id))
        if deliverable:
            s.add(TaskDeliverable(task_id=task.id, agent_id=agent.id, deliverable_type="file",
                                  title="Mini page (index.html)",
                                  path=f"/deliverables/{task.id}/index.html"))
        if note:
            s.add(TaskComment(task_id=task.id, author_agent_id=agent.id, author_type="agent",
                              comment_type="progress",
                              content="Update: created index.html with three squares"))
        await s.commit()
    return task


async def _handed_off_task(agent, *, left_at: datetime):
    """Worked by ``agent``, then handed to human review (assignee cleared)."""
    from app.models.task import Task, TaskEvent

    task = Task(id=uuid.uuid4(), board_id=agent.board_id, title="[Probe] Finished page",
                status="review", assigned_agent_id=None, checklist_total=4, checklist_done=4)
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(task)
        await s.commit()
        s.add(TaskEvent(task_id=task.id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=agent.id, created_at=left_at - timedelta(minutes=3)))
        s.add(TaskEvent(task_id=task.id, from_status="in_progress", to_status="review",
                        changed_by="agent", agent_id=agent.id, created_at=left_at))
        await s.commit()
    return task


async def _context(agent, now=NOW):
    from app.services import chat_run_context

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return await chat_run_context.run_context(s, agent, now=now)


async def test_no_run_no_context():
    agent = await _agent()
    assert await _context(agent) is None


async def test_running_task_is_named_with_folder_deliverables_and_rule():
    agent = await _agent()
    task = await _running_task(agent)
    text = await _context(agent)
    assert text is not None
    assert str(task.id) in text and "[Probe] Mini page" in text
    assert "in_progress" in text and "checklist 1/4" in text
    # The folder the chat session could not find on 22.10.
    assert f"/deliverables/{task.id}/" in text
    assert f"/deliverables/{task.id}/index.html" in text
    assert "created index.html with three squares" in text
    assert "Do NOT work on these tasks" in text
    assert "mc finish" in text
    assert "mc ask --blocking" in text


async def test_just_finished_run_is_named_after_review_handoff():
    """The 22.10. answer "0 % — no task is running": the run had ended a
    moment before. Within 30 minutes the chat hears about it."""
    agent = await _agent()
    task = await _handed_off_task(agent, left_at=NOW - timedelta(minutes=5))
    text = await _context(agent)
    assert text is not None
    assert str(task.id) in text and "finished recently" in text
    assert "status review" in text


async def test_an_old_run_is_not_named():
    agent = await _agent()
    await _handed_off_task(agent, left_at=NOW - timedelta(hours=2))
    assert await _context(agent) is None


class _FakeTransport:
    def __init__(self):
        self.calls: list[dict] = []

    async def prompt(self, text, mode=None, origin=None, context=None):
        self.calls.append({"text": text, "mode": mode, "origin": origin, "context": context})
        return {"ok": True, "turn": 1}


@pytest.fixture
def transport(monkeypatch):
    from app.services import agent_chat_input

    fake = _FakeTransport()
    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: fake)
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state",
                        lambda agent: {"version": 1, "busy": False, "commands": []})
    return fake


async def test_composer_hands_the_run_context_to_an_omp_chat(auth_client, transport):
    agent = await _agent()
    task = await _running_task(agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input",
                                  json={"text": "[Usertest] Wie weit bist du?"})
    assert resp.status_code == 204, resp.text
    [call] = transport.calls
    # The operator's bubble stays what they typed.
    assert call["text"] == "[Usertest] Wie weit bist du?"
    assert call["origin"] == "operator"
    assert call["context"] is not None and str(task.id) in call["context"]


async def test_composer_sends_no_context_without_a_run(auth_client, transport):
    agent = await _agent()
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "hi"})
    assert resp.status_code == 204, resp.text
    assert transport.calls == [{"text": "hi", "mode": "queue", "origin": "operator", "context": None}]


async def test_hermes_gets_no_context_its_tasks_run_in_the_chat(auth_client, transport, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "hermes_driver", "acp")
    agent = await _agent(agent_runtime="host", harness="hermes")
    await _running_task(agent)
    resp = await auth_client.post(f"/api/v1/agents/{agent.id}/chat/input", json={"text": "status?"})
    assert resp.status_code == 204, resp.text
    assert transport.calls[0]["context"] is None


class _FakeProc:
    def __init__(self, stdout: bytes):
        self.returncode = 0
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""


async def test_docker_transport_puts_context_on_the_wire_only_when_set(monkeypatch):
    from app.services import acp_chat_transport

    calls: list[tuple] = []

    async def _exec(*argv, **_kw):
        calls.append(argv)
        return _FakeProc(b'{"ok": true}')

    monkeypatch.setattr(acp_chat_transport.asyncio, "create_subprocess_exec", _exec)
    transport = acp_chat_transport.DockerCtlTransport("ctx-probe")
    await transport.prompt("hi", mode="queue", origin="operator", context="CTX")
    await transport.prompt("hi", mode="queue", origin="operator")
    assert json.loads(calls[0][-1]) == {"text": "hi", "mode": "queue", "origin": "operator",
                                        "context": "CTX"}
    assert json.loads(calls[1][-1]) == {"text": "hi", "mode": "queue", "origin": "operator"}


async def test_http_transport_puts_context_on_the_wire_only_when_set():
    from app.services import acp_chat_transport

    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = acp_chat_transport.HttpCtlTransport(
        "http://host.example:18794", transport=httpx.MockTransport(_handler)
    )
    await transport.prompt("hi", mode="queue", context="CTX")
    await transport.prompt("hi", mode="queue")
    assert json.loads(seen[0].content) == {"text": "hi", "mode": "queue", "context": "CTX"}
    assert json.loads(seen[1].content) == {"text": "hi", "mode": "queue"}
