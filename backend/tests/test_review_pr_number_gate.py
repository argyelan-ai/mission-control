"""Regression tests: Registry-Repo cards must carry pr_number INTO the
in_progress→review transition — not discover its absence at reviewer
dispatch (Task 27ab2ef9, incidents 6f1afe01 + d9910cf3).

On pre-fix main, PATCH status=review on a repo_id card without pr_number
answered 200, committed the status, and the developer's turn ended; only
the reviewer DISPATCH then hit _setup_review_workspace_for_dispatch's
_block() — status=blocked, "Question for @Operator", human repair.
The gate in agent_task_status.py now rejects the transition with an honest
400 while the developer can still fix it in the same turn.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

_REFLECTION = (
    # Body must carry >=80 chars below the headers — the W1 reflection
    # validator rejects header-only skeletons.
    "## Was wurde gemacht\nFeature implementiert inklusive Unit-Tests "
    "und Doku-Update im README.\n\n"
    "## Was hat funktioniert\nTDD-Zyklus lief sauber durch, alle Tests "
    "auf Anhieb gruen.\n\n"
    "## Was war unklar\nNichts Nennenswertes.\n\n"
    "## Lesson fuer Agent-Memory\nKeine neue Lesson."
)


async def _create_repo_card_data(*, repo_id=True, human_review_required=None):
    """Board + developer + reviewer + registry Repo + in_progress task."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.repo import Repo
    from app.models.task import Task, TaskComment
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()
    repo_pk = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="PR Gate Board", slug=f"prgate-{board_id.hex[:8]}")
        s.add(board)

        dev_token_raw, dev_token_hash = generate_agent_token()
        developer = Agent(
            id=dev_id,
            name="Cody",
            role="developer",
            board_id=board_id,
            agent_token_hash=dev_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write", "tasks:create"],
        )
        s.add(developer)

        reviewer_token_raw, reviewer_token_hash = generate_agent_token()
        reviewer = Agent(
            id=reviewer_id,
            name="Rex",
            role="reviewer",
            board_id=board_id,
            agent_token_hash=reviewer_token_hash,
            is_board_lead=False,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(reviewer)

        if repo_id:
            s.add(Repo(
                id=repo_pk,
                full_name="argyelan-ai/mission-control",
                url="https://github.com/argyelan-ai/mission-control",
            ))

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Implement feature Z",
            status="in_progress",
            assigned_agent_id=dev_id,
            repo_id=repo_pk if repo_id else None,
            human_review_required=human_review_required,
        )
        s.add(task)
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=dev_id,
            comment_type="progress", content="Implementation complete",
        ))
        s.add(TaskComment(
            task_id=task_id, author_type="agent", author_agent_id=dev_id,
            comment_type="reflection", content=_REFLECTION,
        ))
        await s.commit()
        await s.refresh(board)
        await s.refresh(developer)
        await s.refresh(reviewer)
        await s.refresh(task)

    return {
        "board": board,
        "developer": developer,
        "reviewer": reviewer,
        "task": task,
        "dev_token": dev_token_raw,
    }


def _patch_ctx():
    """Shared patch context: events + telegram silenced, handoff observed."""
    return (
        patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock),
        patch("app.routers.agent_task_status.emit_event", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock),
        patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock),
        patch("app.services.telegram_bot.settings.telegram_bot_token", "test-token"),
        patch("app.services.telegram_bot.settings.telegram_chat_id", "test-chat"),
        patch("app.services.telegram_bot.telegram_bot.send_message", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_repo_id_review_without_pr_number_is_rejected_400(client, fake_redis):
    """THE regression: repo_id + no pr_number → honest 400, status NOT committed.

    On pre-fix main this PATCH answered 200 and the card stranded later at
    reviewer dispatch (blocked + "Question for @Operator").
    """
    data = await _create_repo_card_data()
    ctx = _patch_ctx()
    with ctx[0], ctx[1], ctx[2], ctx[3] as mock_handoff, ctx[4], ctx[5], ctx[6]:
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 400, resp.text
    assert "PR-Nummer" in resp.json().get("detail", "")
    assert "mc review --pr" in resp.json().get("detail", "")
    # Status must NOT be committed — the developer's turn is still running.
    mock_handoff.assert_not_called()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "in_progress"


@pytest.mark.asyncio
async def test_review_with_pr_number_in_same_patch_passes(client, fake_redis):
    """pr_number alongside status=review → transition succeeds, field persists."""
    data = await _create_repo_card_data()
    ctx = _patch_ctx()
    with ctx[0], ctx[1], ctx[2], ctx[3] as mock_handoff, ctx[4], ctx[5], ctx[6]:
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review", "pr_number": 631},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 200, resp.text
    mock_handoff.assert_called_once()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "review"
        assert updated.pr_number == 631


@pytest.mark.asyncio
async def test_project_id_card_without_pr_number_still_transitions(client, fake_redis):
    """project_id path: backend creates the PR itself — no gate, unchanged."""
    data = await _create_repo_card_data(repo_id=False)
    ctx = _patch_ctx()
    with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6]:
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 200, resp.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "review"


@pytest.mark.asyncio
async def test_human_review_required_repo_card_is_exempt(client, fake_redis):
    """human_review_required → no agent-reviewer dispatch → nothing strands."""
    data = await _create_repo_card_data(human_review_required=True)
    ctx = _patch_ctx()
    with ctx[0], ctx[1], ctx[2], ctx[3] as mock_handoff, ctx[4], ctx[5], ctx[6]:
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 200, resp.text
    mock_handoff.assert_not_called()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "review"
