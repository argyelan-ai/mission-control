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

Round 2 (23.10.2026, t1 SHOULD-1 / NICE-3, r1 S1):

* times are the operator's local time (``settings.usage_timezone``), never
  "UTC" — the chat answered "01:04:35 UTC" while the UI said 03:03;
* the status is a word in the agent's ``operator_language`` ("in Arbeit"),
  never the raw code ("in_progress");
* the reading hint ``mc task-get <id>`` is one the chat can run (the CLI
  resolves the board itself, scripts/mc-cli), and the block says to answer
  from it first.

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
    assert "status: in progress" in text and "checklist 1/4" in text
    assert "in_progress" not in text
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
    assert "status: awaits review" in text


@pytest.fixture
def local_zone(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "usage_timezone", "Europe/Berlin")


async def test_times_are_the_operators_local_time(local_zone):
    """22:19 UTC on 22.10. is 00:19 in Berlin (CEST, +2) — the operator's
    screen shows that, so the chat must say that."""
    agent = await _agent()
    await _running_task(agent)
    text = await _context(agent)
    assert "started 00:19" in text
    assert "UTC" not in text
    assert "State as of 00:21 (the operator's local time, Europe/Berlin)" in text


async def test_a_start_on_another_day_carries_the_date(local_zone):
    agent = await _agent()
    task = await _running_task(agent)
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Task, task.id)
        row.started_at = NOW - timedelta(days=1, hours=1)
        s.add(row)
        await s.commit()
    text = await _context(agent)
    assert "started 21.10. 23:21" in text


async def test_status_is_a_word_in_the_operators_language():
    agent = await _agent(operator_language="de")
    await _running_task(agent)
    await _handed_off_task(agent, left_at=NOW - timedelta(minutes=5))
    text = await _context(agent)
    assert "status: in Arbeit" in text
    assert "status: wartet auf Prüfung" in text
    assert "in_progress" not in text and "status review" not in text


async def test_the_block_says_answer_from_here_and_names_a_runnable_read():
    agent = await _agent()
    task = await _running_task(agent)
    text = await _context(agent)
    assert "Answer from this block first" in text
    assert "`mc task-get <id>`" in text
    assert "never raw status codes" in text
    # The three ways that set the run's task to blocked (round-2 r1 S2).
    assert "`mc question`" in text and "`mc help`" in text and "`mc delegate`" in text
    # Run-state comments are named, a plain reply is not forbidden (r1 S3).
    assert "`mc comment progress|resolution|evidence|blocker|reflection`" in text
    assert str(task.id) in text


async def test_the_block_forbids_review_decisions_and_raw_snapshots():
    """Round 3: r1 S1 — `mc review` / `mc review-note` are the run's call;
    t1 NICE-4 — a `task-get` excerpt came as raw JSON in UTC next to 05:42;
    t1 NICE-5 — "since then nothing was written" read like now, while the run
    had already written the file."""
    agent = await _agent()
    await _running_task(agent)
    text = await _context(agent)
    assert "`mc review` / `mc review-note`" in text
    assert "never paste its raw JSON" in text
    assert "times on the operator's clock" in text
    assert "a snapshot" in text and "as of" in text


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


async def test_reviewers_review_card_is_named_as_live():
    """A review handoff gives the `review` card to the reviewer; its review
    run works it however long ago the developer's run let go (round-2 review
    SHOULD-1). The reviewer's chat hears about it as live work, with the
    review run's start — not the developer's — and not as "awaits review"."""
    from app.models.task import Task, TaskEvent

    developer = await _agent()
    reviewer = await _agent(role="reviewer")
    task = Task(id=uuid.uuid4(), board_id=reviewer.board_id, title="[Probe] Review me",
                status="review", assigned_agent_id=reviewer.id, dispatch_intent="review_handoff",
                started_at=NOW - timedelta(hours=3), dispatched_at=NOW - timedelta(minutes=4))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(task)
        await s.commit()
        s.add(TaskEvent(task_id=task.id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=developer.id,
                        created_at=NOW - timedelta(hours=3)))
        s.add(TaskEvent(task_id=task.id, from_status="in_progress", to_status="review",
                        changed_by="agent", agent_id=developer.id,
                        created_at=NOW - timedelta(hours=2)))
        await s.commit()
    text = await _context(reviewer)
    assert text is not None
    assert str(task.id) in text and "RIGHT NOW" in text
    assert "status: your review run is reviewing it" in text
    assert "started 22:17" in text
    assert "awaits review" not in text


# ── Round 4 (23.10.2026): honest about replies and who decides a review ────


async def test_the_block_names_the_reply_the_chat_may_write():
    """t1 SHOULD-1: asked to put its answer on the task, the chat said "the
    server blocks that" — it had not tried. The block only listed what is
    refused; the reply that is allowed (stored as ``chat_reply``) was named
    nowhere. Now it is, with the one form the chat can run (no TASK_ID in
    the chat's env, so ``--task-id``), and the rule against made-up
    refusals."""
    agent = await _agent()
    task = await _running_task(agent)
    text = await _context(agent)
    assert "A reply in the task thread IS allowed" in text
    assert '`mc comment message "<text>" --task-id <id>`' in text
    assert "when the operator asks" in text
    assert "not move the run" in text
    assert "Never say the server refuses a call you have not made" in text
    assert str(task.id) in text


async def test_a_human_review_card_names_the_operator_and_the_button():
    """t1 NICE-3: on a card with human review the refusal said "the verdict
    is your review run's … press 'Genehmigen'". There was no review run, and
    the button is "Annehmen". The block names who decides, per card."""
    from app.models.task import Task

    agent = await _agent(operator_language="de")
    task = await _handed_off_task(agent, left_at=NOW - timedelta(minutes=2))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        row = await s.get(Task, task.id)
        row.human_review_required = True
        s.add(row)
        await s.commit()
    text = await _context(agent)
    assert "the operator decides it in the task view" in text
    assert '"Annehmen" / "Änderungen anfordern"' in text
    assert "review run" not in text


async def test_a_human_review_card_names_the_english_buttons():
    agent = await _agent()
    await _handed_off_task(agent, left_at=NOW - timedelta(minutes=2))
    text = await _context(agent)
    assert '"Approve" / "Request changes"' in text
    assert "review run" not in text


async def test_an_agent_review_names_the_reviewer_agent():
    """The developer's card went to a reviewer agent (review handoff): its
    run decides — not the operator, not this chat."""
    from app.models.task import Task, TaskEvent

    developer = await _agent()
    reviewer = await _agent(role="reviewer")
    task = Task(id=uuid.uuid4(), board_id=developer.board_id, title="[Probe] Under review",
                status="review", assigned_agent_id=reviewer.id, dispatch_intent="review_handoff",
                started_at=NOW - timedelta(minutes=9), dispatched_at=NOW - timedelta(minutes=1))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(task)
        await s.commit()
        s.add(TaskEvent(task_id=task.id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=developer.id, created_at=NOW - timedelta(minutes=9)))
        s.add(TaskEvent(task_id=task.id, from_status="in_progress", to_status="review",
                        changed_by="agent", agent_id=developer.id, created_at=NOW - timedelta(minutes=2)))
        await s.commit()
    dev_text = await _context(developer)
    assert "a reviewer agent's review run decides it" in dev_text
    assert "the operator decides" not in dev_text
    rev_text = await _context(reviewer)
    assert "your own review run decides it — not this chat" in rev_text
    # Its review run's card: a reply from the chat is refused there too.
    assert "not on a card your review run is reviewing" in rev_text


async def test_a_running_task_names_no_reviewer():
    agent = await _agent()
    await _running_task(agent)
    text = await _context(agent)
    assert "decides it" not in text


async def test_a_review_card_still_with_the_developer_names_no_reviewer():
    """Before a handoff the card can sit in `review` with the developer as
    assignee: no reviewer is known, so the block names none."""
    from app.models.task import Task, TaskEvent

    agent = await _agent()
    task = Task(id=uuid.uuid4(), board_id=agent.board_id, title="[Probe] Just finished",
                status="review", assigned_agent_id=agent.id, started_at=NOW - timedelta(minutes=9))
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(task)
        await s.commit()
        s.add(TaskEvent(task_id=task.id, from_status="inbox", to_status="in_progress",
                        changed_by="agent", agent_id=agent.id, created_at=NOW - timedelta(minutes=9)))
        s.add(TaskEvent(task_id=task.id, from_status="in_progress", to_status="review",
                        changed_by="agent", agent_id=agent.id, created_at=NOW - timedelta(minutes=1)))
        await s.commit()
    text = await _context(agent)
    assert "it waits for its review" in text
    assert "reviewer agent" not in text and "review run" not in text
