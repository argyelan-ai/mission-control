"""An answered question resumes an omp ACP task in a RUN, never in the chat
(round-1 user test t1, MUST-1 — follow-up of the chat guard).

For an omp agent with the ACP chat daemon (``acp-docker``) the task turn ends
when the agent asks a blocking question (``waiting``); no run session waits
for a nudge. Before, an answer with the agent still on the card took the
"session is still live" branch: no re-dispatch, only a thread message — and
the wake-up for that lands in the CHAT session, which then worked the task
there. With the chat guard (``chat_session_guard``) that would leave the card
stuck. So for ``acp-docker`` the answer re-dispatches like a parked card.
Other agents keep the old branch.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import create_access_token, generate_agent_token
from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task
from app.models.user import User
from app.services.messaging import ensure_task_thread, post_message

from .conftest import test_engine

pytestmark = pytest.mark.asyncio


async def _answer_with_agent_on_card(client, *, harness: str, runtime: str = "cli-bridge"):
    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _raw, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Resume", slug=f"rs-{uuid.uuid4().hex[:6]}"))
        await s.commit()
        s.add(Agent(id=agent_id, name="Probe", role="developer", board_id=board_id,
                    scopes=["tasks:read", "tasks:write"], provision_status="provisioned",
                    agent_token_hash=token_hash, agent_runtime=runtime, harness=harness,
                    current_task_id=task_id, run_state="running"))
        await s.commit()
        s.add(Task(id=task_id, board_id=board_id, title="Probe waiting", status="waiting",
                   assigned_agent_id=agent_id, dispatch_attempt_id=str(uuid.uuid4())))
        await s.commit()
        task = await s.get(Task, task_id)
        thread = await ensure_task_thread(s, task)
        question = await post_message(
            s, thread_id=thread.id, sender_type="agent", sender_id=agent_id,
            message_type="question", body="Which colour?",
            question_meta={"awaiting": True, "blocking": True, "to": "boss", "priority": "high"},
        )
        user_id = uuid.uuid4()
        s.add(User(id=user_id, email=f"u-{user_id.hex[:6]}@mc.local", name="Op",
                   role="admin", is_active=True))
        await s.commit()
    mock_dispatch = AsyncMock()
    with patch("app.services.dispatch.auto_dispatch_task", mock_dispatch):
        client.headers["Authorization"] = f"Bearer {create_access_token(str(user_id), 'admin')}"
        resp = await client.post(f"/api/v1/tasks/{task_id}/thread/messages",
                                 json={"body": "Yellow.", "reply_to": str(question.id)})
    assert resp.status_code == 201, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        resumed = await s.get(Task, task_id)
    return mock_dispatch, resumed


async def test_omp_acp_answer_resumes_through_the_run_path(client):
    mock_dispatch, task = await _answer_with_agent_on_card(client, harness="omp")
    assert task.status == "in_progress"
    assert mock_dispatch.called, "the answer must start a run, not wake the chat"
    assert "Yellow." in mock_dispatch.call_args.kwargs["extra_recovery_context"]


async def test_other_agents_keep_the_live_session_branch(client):
    mock_dispatch, task = await _answer_with_agent_on_card(client, harness="claude")
    assert task.status == "in_progress"
    assert not mock_dispatch.called


async def _unblock_reset(harness: str) -> tuple[str, str | None]:
    from app.services.task_lifecycle import apply_unblock_notify_reset

    board_id, agent_id, task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_attempt = str(uuid.uuid4())
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Unblock", slug=f"ub-{uuid.uuid4().hex[:6]}"))
        await s.commit()
        s.add(Agent(id=agent_id, name="Probe", role="developer", board_id=board_id,
                    agent_runtime="cli-bridge", harness=harness, current_task_id=task_id))
        await s.commit()
        s.add(Task(id=task_id, board_id=board_id, title="Probe", status="in_progress",
                   assigned_agent_id=agent_id, dispatch_attempt_id=old_attempt))
        await s.commit()
        task = await s.get(Task, task_id)
        await apply_unblock_notify_reset(s, task, "waiting", task_id, caller="test")
        task = await s.get(Task, task_id)
        return old_attempt, task.dispatch_attempt_id


async def test_omp_acp_waiting_unblock_hands_the_card_to_a_new_run():
    """Operator PATCH waiting → in_progress: for acp-docker no paused run
    session exists, so the handshake resets and the poll starts a run."""
    old, new = await _unblock_reset("omp")
    assert new is not None and new != old


async def test_other_agents_keep_their_paused_session_on_waiting_unblock():
    old, new = await _unblock_reset("claude")
    assert new == old
