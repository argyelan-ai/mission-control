"""The question card in the chat, for every agent (Welle 3, E11).

An agent asks the operator — `mc question` (a clarification approval that
blocks the task) or a blocking `mc ask` (a thread question that parks the
task in `waiting`). The chat lists these open questions for ONE agent, and
one tap answers them through the existing resolve paths, so the task runs on
and the answer reaches the agent through its normal pull.

Nothing here knows a harness: the list and the answer are the same for a
Claude terminal agent, an omp agent or any other runtime.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board
from app.models.task import Task, TaskComment

from .conftest import test_engine


async def _setup(*, agent_runtime: str = "cli-bridge", comm_v2: bool = False):
    """Board + an agent working on one in_progress task. Returns (board, agent, task, token)."""
    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=uuid.uuid4(), name="Q", slug=f"q-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        agent = Agent(
            id=uuid.uuid4(), name="Worker", role="developer", board_id=board.id,
            scopes=["tasks:help", "chat:write"], provision_status="provisioned",
            agent_token_hash=token_hash, agent_runtime=agent_runtime, comm_v2=comm_v2,
        )
        s.add(agent)
        await s.commit()
        task = Task(
            id=uuid.uuid4(), board_id=board.id, title="Add a test for parse()",
            status="in_progress", assigned_agent_id=agent.id,
        )
        s.add(task)
        await s.commit()
        agent.current_task_id = task.id
        s.add(agent)
        await s.commit()
    return board, agent, task, raw_token


def _as_agent(client: AsyncClient, token: str) -> None:
    client.headers["Authorization"] = f"Bearer {token}"


async def _as_operator(client: AsyncClient, *, role: str = "admin") -> None:
    from app.models.user import User

    user_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=user_id, email=f"{user_id.hex[:8]}@mc.local", name="Op", role=role, is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(user_id), role)}"


async def _mc_question(client: AsyncClient, board_id, token: str, question: str, options=None) -> str:
    _as_agent(client, token)
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/clarification",
            json={"question": question, "options": options},
        )
    assert resp.status_code == 201, resp.text
    return resp.json()["approval_id"]


@pytest.mark.asyncio
async def test_mc_question_card_answer_task_runs_on(client: AsyncClient, fake_redis):
    """The whole loop: mc question → card in the chat list → one tap → task
    in_progress again → the agent's next pull carries the answer."""
    board, agent, task, token = await _setup()
    approval_id = await _mc_question(
        client, board.id, token,
        "Ready to commit: add test for parse() (1 file). Commit?",
        ["Commit", "Show diff", "Change something"],
    )

    await _as_operator(client)
    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/questions")
    assert resp.status_code == 200, resp.text
    questions = resp.json()["questions"]
    assert len(questions) == 1
    q = questions[0]
    assert q["id"] == approval_id
    assert q["source"] == "approval"
    assert q["answerable"] is True
    assert q["blocking"] is True
    assert q["question"].startswith("Ready to commit")
    assert q["options"] == ["Commit", "Show diff", "Change something"]
    assert q["task_id"] == str(task.id)
    assert q["task_title"] == "Add a test for parse()"

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/questions/{approval_id}/answer",
        json={"answer": "Commit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "answered", "task_status": "in_progress"}

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.get(Task, task.id)).status == "in_progress"
        approval = await s.get(Approval, uuid.UUID(approval_id))
        assert approval.status == "approved"
        assert approval.resolver_note == "Commit"
        # The agent's own pull delivers the answer (same path as the inbox).
        from app.routers.agents import _collect_and_ack_new_comments

        fresh_agent = await s.get(Agent, agent.id)
        delivered = await _collect_and_ack_new_comments(fresh_agent, s)
    assert any("Commit" in c["content"] for c in delivered), delivered

    # The card is gone from the list.
    resp = await client.get(f"/api/v1/agents/{agent.id}/chat/questions")
    assert resp.json()["questions"] == []


@pytest.mark.asyncio
async def test_list_is_per_agent_and_only_pending(client: AsyncClient, fake_redis):
    board, agent, task, token = await _setup()
    _, other, _, other_token = await _setup()
    await _mc_question(client, board.id, token, "Mine?")
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Approval(
            board_id=board.id, agent_id=agent.id, task_id=task.id,
            action_type="clarification_question", description="old",
            payload={"question": "Already answered"}, status="approved",
        ))
        await s.commit()

    await _as_operator(client)
    mine = (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"]
    theirs = (await client.get(f"/api/v1/agents/{other.id}/chat/questions")).json()["questions"]
    assert [q["question"] for q in mine] == ["Mine?"]
    assert theirs == []


@pytest.mark.asyncio
async def test_other_approvals_are_listed_but_answered_elsewhere(client: AsyncClient, fake_redis):
    """An install request or a blocker is the agent asking, too — the chat
    shows it honestly (never hidden) but sends the operator to the card that
    states its consequences instead of approving it with one tap."""
    board, agent, task, _ = await _setup()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approval = Approval(
            board_id=board.id, agent_id=agent.id, task_id=task.id,
            action_type="install_mcp", description="Install MCP server 'search'",
            payload={"name": "search"}, status="pending",
        )
        s.add(approval)
        await s.commit()
        await s.refresh(approval)

    await _as_operator(client)
    questions = (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"]
    assert len(questions) == 1
    assert questions[0]["answerable"] is False
    assert questions[0]["kind"] == "install_mcp"
    assert questions[0]["question"] == "Install MCP server 'search'"

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/questions/{approval.id}/answer",
        json={"answer": "yes"},
    )
    assert resp.status_code == 409
    assert resp.json() == {"reason": "answer_elsewhere"}
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.get(Approval, approval.id)).status == "pending"


@pytest.mark.asyncio
async def test_blocking_ask_is_a_card_and_the_answer_resumes_the_task(client: AsyncClient, fake_redis):
    """`mc ask --blocking --to <operator>` parks the task in `waiting`; the
    same card answers it through the thread reply path."""
    from app.comm_constants import QUESTION_TARGETS

    board, agent, task, token = await _setup(comm_v2=True)
    _as_agent(client, token)
    resp = await client.post(
        "/api/v1/agent/tasks/current/ask",
        json={"question": "Which name?", "blocking": True, "to": QUESTION_TARGETS[0], "options": ["a", "b"]},
    )
    assert resp.status_code == 201, resp.text
    message_id = resp.json()["message_id"]

    await _as_operator(client)
    questions = (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"]
    assert len(questions) == 1
    assert questions[0]["id"] == message_id
    assert questions[0]["source"] == "thread"
    assert questions[0]["answerable"] is True
    assert questions[0]["blocking"] is True
    assert questions[0]["options"] == ["a", "b"]

    resp = await client.post(
        f"/api/v1/agents/{agent.id}/chat/questions/{message_id}/answer",
        json={"answer": "b"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_status"] == "in_progress"
    assert (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"] == []


@pytest.mark.asyncio
async def test_questions_to_the_lead_are_not_operator_cards(client: AsyncClient, fake_redis):
    board, agent, task, token = await _setup(comm_v2=True)
    _as_agent(client, token)
    resp = await client.post("/api/v1/agent/tasks/current/ask", json={"question": "Lead?", "to": "boss"})
    assert resp.status_code == 201, resp.text
    await _as_operator(client)
    assert (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"] == []


@pytest.mark.asyncio
async def test_answer_twice_is_409_not_open(client: AsyncClient, fake_redis):
    board, agent, task, token = await _setup()
    approval_id = await _mc_question(client, board.id, token, "Go?", ["Yes", "No"])
    await _as_operator(client)
    url = f"/api/v1/agents/{agent.id}/chat/questions/{approval_id}/answer"
    assert (await client.post(url, json={"answer": "Yes"})).status_code == 200
    resp = await client.post(url, json={"answer": "No"})
    assert resp.status_code == 409
    assert resp.json() == {"reason": "question_not_open"}


@pytest.mark.asyncio
async def test_answer_needs_admin_and_text(client: AsyncClient, fake_redis):
    board, agent, task, token = await _setup()
    approval_id = await _mc_question(client, board.id, token, "Go?")
    url = f"/api/v1/agents/{agent.id}/chat/questions/{approval_id}/answer"

    await _as_operator(client, role="viewer")
    assert (await client.post(url, json={"answer": "Yes"})).status_code == 403
    # Reading is fine for any signed-in user (the card renders read-only).
    assert (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).status_code == 200

    await _as_operator(client)
    assert (await client.post(url, json={"answer": "   "})).status_code == 422
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert (await s.get(Task, task.id)).status == "blocked"


@pytest.mark.asyncio
async def test_unknown_agent_is_404(client: AsyncClient, fake_redis):
    await _as_operator(client)
    assert (await client.get(f"/api/v1/agents/{uuid.uuid4()}/chat/questions")).status_code == 404


@pytest.mark.asyncio
async def test_harness_does_not_matter(client: AsyncClient, fake_redis):
    """Same list for every runtime — the card is not an ACP-only feature."""
    for runtime in ("cli-bridge", "host", "claude-code"):
        board, agent, task, token = await _setup(agent_runtime=runtime)
        await _mc_question(client, board.id, token, f"From {runtime}?")
        await _as_operator(client)
        questions = (await client.get(f"/api/v1/agents/{agent.id}/chat/questions")).json()["questions"]
        assert [q["question"] for q in questions] == [f"From {runtime}?"], runtime
