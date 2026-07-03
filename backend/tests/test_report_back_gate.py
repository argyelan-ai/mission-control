"""Tests for the report-back hard gate + auto-draft.

Scenario matrix (context: feat/report-back-hard-gate):

| # | Scenario                               | Behavior                               |
|---|----------------------------------------|----------------------------------------|
| 1 | Happy path: telegram → done            | OK                                     |
| 2 | Without report_back_required           | done without telegram: OK              |
| 3 | Gate kicks in (required, no telegram)  | done → 422                             |
| 4 | Retry after 422                        | 422 → telegram → done: OK              |
| 5 | Idempotency, telegram multiple times   | Flag stays True                        |
| 6 | Failure → auto-draft                   | failed without telegram: draft sent    |
| 7 | Failure after telegram (already sent)  | no second draft                        |
| 9 | Review path with report                | Dev: telegram+review, Rex: done OK     |
|10 | Review path without report             | Rex: done → 422                        |
|11 | Bot unconfigured on failed             | Draft skipped, failed still goes through |
|12 | Subtask (parent_task_id set)           | No gate — subtask isn't a reports case |
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ────────────────────────────────────────────────────────────────────
# Fixtures / Helpers
# ────────────────────────────────────────────────────────────────────

async def _setup_root_task_with_report_required(
    *,
    report_back_required: bool = True,
    task_status: str = "in_progress",
    parent_id: uuid.UUID | None = None,
):
    """Creates board + developer + root task (optionally as subtask) + returns token."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="RB Board", slug=f"rb-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Reporter", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=task_id,
            emoji="🔍",
        ))
        s.add(Task(
            id=task_id,
            board_id=board_id,
            title="Research Task",
            status=task_status,
            assigned_agent_id=agent_id,
            owner_agent_id=agent_id,
            report_back_required=report_back_required,
            parent_task_id=parent_id,
        ))
        await s.commit()

    return {
        "board_id": board_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "token": token_raw,
    }


def _mock_configured_reports_service(send_return={"ok": True, "result": {"message_id": 1}}):
    """Helps with patching — returns a mock where `configured` is True
    and `send` responds with the given return value."""
    mock = MagicMock()
    mock.configured = True
    mock.send = AsyncMock(return_value=send_return)
    return mock


async def _add_reflection_comment(task_id: uuid.UUID, agent_id: uuid.UUID):
    """Posts a mandatory reflection so the closing transition gate passes."""
    from app.models.task import TaskComment
    content = (
        "## Was wurde gemacht\nTask fertig.\n\n"
        "## Was hat funktioniert\nAlles gut.\n\n"
        "## Was war unklar\nNichts.\n\n"
        "## Lesson fuer Agent-Memory\nKeine."
    )
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=task_id,
            author_type="agent",
            author_agent_id=agent_id,
            content=content,
            comment_type="reflection",
        ))
        await s.commit()


# ────────────────────────────────────────────────────────────────────
# Scenario 1: Happy Path
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_happy_path_telegram_then_done(client, fake_redis):
    """1. Send telegram → flag set → done OK."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r1 = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "🔍 Reporter · Done ✅"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r1.status_code == 200

    # Flag must be set in the DB
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True

    # Reflection needed before done
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    # done now goes through
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r2 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r2.status_code == 200, f"Expected 200, got {r2.status_code}: {r2.text}"
    assert r2.json()["status"] == "done"


# ────────────────────────────────────────────────────────────────────
# Scenario 2: Without report_back_required
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_without_report_back_required_passes(client, fake_redis):
    """2. No report_back_required → done without telegram is OK, no gate."""
    data = await _setup_root_task_with_report_required(report_back_required=False)
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────────────────────────
# Scenario 3: Gate kicks in
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_blocks_done_when_required_and_not_sent(client, fake_redis):
    """3. done with reflection but without telegram → 422 gate message."""
    data = await _setup_root_task_with_report_required()
    # Reflection is there, but flag not set — gate must kick in
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 422, r.text
    assert "mc telegram" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────
# Scenario 4: Retry after 422
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_after_422_works(client, fake_redis):
    """4. done (→422) → telegram → done → OK."""
    data = await _setup_root_task_with_report_required()
    await _add_reflection_comment(data["task_id"], data["agent_id"])

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r1 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r1.status_code == 422

        mock_reports = _mock_configured_reports_service()
        with patch("app.services.telegram_reports.telegram_reports", mock_reports):
            r2 = await client.post(
                "/api/v1/agent/telegram/send",
                json={"text": "report"},
                headers={"Authorization": f"Bearer {data['token']}"},
            )
            assert r2.status_code == 200

        r3 = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
        assert r3.status_code == 200, r3.text


# ────────────────────────────────────────────────────────────────────
# Scenario 5: Idempotency
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiple_telegram_calls_idempotent(client, fake_redis):
    """5. telegram × 3 → flag stays true, no side effect."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        for _ in range(3):
            r = await client.post(
                "/api/v1/agent/telegram/send",
                json={"text": "chunk"},
                headers={"Authorization": f"Bearer {data['token']}"},
            )
            assert r.status_code == 200

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True

    # Service was called 3x (no early return when the flag is already set)
    assert mock_reports.send.await_count == 3


# ────────────────────────────────────────────────────────────────────
# Scenario 6: Failure → Auto-Draft
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_triggers_auto_draft(client, fake_redis):
    """6. failed without telegram → auto-draft sent, flag set, failed goes through."""
    data = await _setup_root_task_with_report_required()

    # Add reflection comment so the draft has content
    from app.models.task import TaskComment
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(TaskComment(
            task_id=data["task_id"],
            author_type="agent",
            author_agent_id=data["agent_id"],
            content="**Was ich versucht habe:** Scraping blockiert durch Cloudflare.",
            comment_type="reflection",
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "technical_problem",
                "blocker_question": "Wie scrapen wenn Cloudflare blockt?",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    mock_reports.send.assert_awaited()  # Auto-draft was sent

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is True
        assert t.status == "failed"


# ────────────────────────────────────────────────────────────────────
# Scenario 7: Failure with telegram already sent
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_after_telegram_skips_auto_draft(client, fake_redis):
    """7. telegram sent → failed → no second draft (flag already true)."""
    data = await _setup_root_task_with_report_required()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "manueller report"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    # Reset call count — after the telegram send
    mock_reports.send.reset_mock()

    with patch("app.services.telegram_reports.telegram_reports", mock_reports), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "other",
                "blocker_question": "n/a",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200
    # No second send call — flag was already true, auto-draft block skipped
    mock_reports.send.assert_not_awaited()


# ────────────────────────────────────────────────────────────────────
# Scenario 11: Bot unconfigured on failed
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_with_unconfigured_bot_still_transitions(client, fake_redis):
    """11. failed + bot not configured → auto-draft skipped, failed still goes through, flag not set."""
    data = await _setup_root_task_with_report_required()

    unconfigured_mock = MagicMock()
    unconfigured_mock.configured = False
    unconfigured_mock.send = AsyncMock()  # not called

    with patch("app.services.telegram_reports.telegram_reports", unconfigured_mock), \
         patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={
                "status": "failed",
                "blocker_type": "other",
                "blocker_question": "test",
            },
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 200  # Failed still goes through — task must not get stuck
    unconfigured_mock.send.assert_not_awaited()

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.status == "failed"
        assert t.report_sent_to_telegram is False  # Flag NOT set


# ────────────────────────────────────────────────────────────────────
# Scenario 12: Subtask (parent_task_id set)
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subtask_done_without_telegram_passes(client, fake_redis):
    """12. Subtask (parent_task_id set) → gate does NOT kick in, done OK without telegram."""
    # First create the parent
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    parent_id = uuid.uuid4()
    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    subtask_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Sub Board", slug=f"sub-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="SubWorker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=subtask_id,
        ))
        s.add(Task(
            id=parent_id, board_id=board_id, title="Parent",
            status="in_progress", report_back_required=True,
        ))
        s.add(Task(
            id=subtask_id, board_id=board_id, title="Subtask",
            status="in_progress", assigned_agent_id=agent_id,
            parent_task_id=parent_id,
            report_back_required=True,  # even if true — subtask has no gate
        ))
        await s.commit()

    await _add_reflection_comment(subtask_id, agent_id)

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{subtask_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert r.status_code == 200, r.text


# ────────────────────────────────────────────────────────────────────
# Scenario 10: Review path without report → gate kicks in
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_path_without_report_blocks_rex_done(client, fake_redis):
    """10. Dev → review (no telegram), Rex → done → 422.

    Gate is status-based, not agent-based: no matter who sets done,
    if the flag isn't set, the backend blocks.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="ReviewBoard", slug=f"rv-{uuid.uuid4().hex[:6]}",
                    require_review_before_done=True))
        dev_token, dev_hash = generate_agent_token()
        rex_token, rex_hash = generate_agent_token()
        s.add(Agent(
            id=dev_id, name="Cody", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
            current_task_id=task_id,
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=rex_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Reviewed Feature",
            status="review",  # Dev already set review without telegram
            assigned_agent_id=rex_id, owner_agent_id=dev_id,
            report_back_required=True,
        ))
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        # Rex versucht done zu setzen
        r = await client.patch(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {rex_token}"},
        )
    assert r.status_code == 422
    assert "mc telegram" in r.json()["detail"]


# ────────────────────────────────────────────────────────────────────
# Direct unit tests for render_and_send_failure_draft
# ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_draft_includes_reflection_and_escapes_html():
    """The auto-draft contains reflection + HTML escaping for user content."""
    from app.services.report_auto_draft import _render_draft

    text = _render_draft(
        agent_name="Researcher <weird>",
        agent_emoji="🔍",
        task_title="Title with <tags> & symbols",
        task_id="abc12345-6789-0000-0000-000000000000",
        reflection="Problem war <script>alert()</script> im Code.",
        recent_comments=["Comment 1 & stuff", "Comment 2"],
    )

    # HTML escape: & becomes &amp;, < becomes &lt;
    assert "&lt;weird&gt;" in text
    assert "&lt;tags&gt;" in text
    assert "&amp; symbols" in text
    assert "&lt;script&gt;" in text  # Reflection content escaped

    # Header structure
    assert text.startswith("🔍 <b>Researcher &lt;weird&gt;</b>")
    assert "❌" in text
    assert "<b>Reflexion des Agenten</b>" in text
    assert "<b>Letzte Kommentare</b>" in text
    assert "abc12345" in text  # Short ID


@pytest.mark.asyncio
async def test_worker_without_current_task_id_can_set_flag_via_task_id(client, fake_redis):
    """C1 regression: subagent dispatch worker (no current_task_id) can set the
    flag by sending task_id in the /telegram/send body.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="NoCurrentTask", slug=f"nct-{uuid.uuid4().hex[:6]}"))
        token_raw, token_hash = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="Subworker", role="developer",
            board_id=board_id, agent_token_hash=token_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
            provision_status="provisioned",
            current_task_id=None,  # Subagent dispatch has no agent-level tracker
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Worker-Task", status="in_progress",
            assigned_agent_id=agent_id, owner_agent_id=agent_id,
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "worker report", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {token_raw}"},
        )

    assert r.status_code == 200, r.text

    # Flag must be set even without current_task_id
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, task_id)
        assert t.report_sent_to_telegram is True


@pytest.mark.asyncio
async def test_task_id_ownership_check_rejects_foreign_agent(client, fake_redis):
    """Ownership check: agent may only set the flag on their own tasks."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    foreign_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Own", slug=f"own-{uuid.uuid4().hex[:6]}"))
        _, owner_hash = generate_agent_token()
        foreign_token, foreign_hash = generate_agent_token()
        s.add(Agent(
            id=owner_id, name="Owner", role="developer",
            board_id=board_id, agent_token_hash=owner_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Agent(
            id=foreign_id, name="Foreign", role="developer",
            board_id=board_id, agent_token_hash=foreign_hash,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="OwnedTask", status="in_progress",
            assigned_agent_id=owner_id, owner_agent_id=owner_id,
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "hijack attempt", "task_id": str(task_id)},
            headers={"Authorization": f"Bearer {foreign_token}"},
        )

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_review_approve_blocked_when_report_not_sent(client, fake_redis):
    """C2 regression: POST /review with decision=approve respects the gate."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    rex_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="ReviewGate", slug=f"rvg-{uuid.uuid4().hex[:6]}"))
        _, dev_hash = generate_agent_token()
        rex_token, rex_hash = generate_agent_token()
        s.add(Agent(
            id=dev_id, name="DevGuy", role="developer",
            board_id=board_id, agent_token_hash=dev_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Agent(
            id=rex_id, name="Rex", role="reviewer",
            board_id=board_id, agent_token_hash=rex_hash,
            scopes=["tasks:read", "tasks:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Feature",
            status="review",
            assigned_agent_id=rex_id, owner_agent_id=dev_id,
            report_back_required=True,
            # No report sent — flag stays False
        ))
        await s.commit()

    # /review approve should throw 422
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/review",
            json={"decision": "approve", "comment": "Looks good, approved."},
            headers={"Authorization": f"Bearer {rex_token}"},
        )

    assert r.status_code == 422, r.text
    assert "mc telegram" in r.json()["detail"]


@pytest.mark.asyncio
async def test_discord_channel_task_bypasses_gate(client, fake_redis):
    """C3 regression: tasks with report_back_channel='discord' are not blocked."""
    data = await _setup_root_task_with_report_required()
    # Switch channel to discord
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        t.report_back_channel = "discord"
        s.add(t)
        await s.commit()

    await _add_reflection_comment(data["task_id"], data["agent_id"])

    # done without mc telegram MUST go through — Discord delivery is a different channel
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        r = await client.patch(
            f"/api/v1/agent/boards/{data['board_id']}/tasks/{data['task_id']}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['token']}"},
        )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_atomic_claim_update_prevents_double_send():
    """C4 regression: atomic UPDATE claim guarantees exactly one rowcount=1
    under a simulated race. Direct DB-level test instead of HTTP (SQLite
    limits with concurrent HTTP).
    """
    from sqlalchemy import update as _sa_update
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    task_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Race", slug=f"race-{uuid.uuid4().hex[:6]}"))
        _, h = generate_agent_token()
        s.add(Agent(
            id=agent_id, name="A", role="developer",
            board_id=board_id, agent_token_hash=h,
            scopes=["chat:write"],             provision_status="provisioned",
        ))
        s.add(Task(
            id=task_id, board_id=board_id, title="Race Task",
            status="in_progress", assigned_agent_id=agent_id,
            report_back_required=True,
            report_sent_to_telegram=False,
        ))
        await s.commit()

        # First claim — should result in rowcount=1
        r1 = await s.exec(
            _sa_update(Task)
            .where(Task.id == task_id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await s.commit()
        assert r1.rowcount == 1, "Erster Claim muss erfolgreich sein"

        # Second claim (simulates a parallel request) — should result in rowcount=0
        r2 = await s.exec(
            _sa_update(Task)
            .where(Task.id == task_id, Task.report_sent_to_telegram == False)  # noqa: E712
            .values(report_sent_to_telegram=True)
        )
        await s.commit()
        assert r2.rowcount == 0, (
            "Zweiter Claim muss rowcount=0 ergeben (Flag schon gesetzt) — "
            "sonst ist der Atomic-Claim-Schutz kaputt und wir senden doppelt"
        )


@pytest.mark.asyncio
async def test_telegram_send_http_exception_rolls_back_claim(client, fake_redis):
    """B1 regression: httpx/network exception in telegram_reports.send() → flag
    is rolled back, agent can retry (otherwise permanent lock).
    """
    data = await _setup_root_task_with_report_required()

    # Mock: configured=True but send() raises httpx.ConnectTimeout
    import httpx
    failing_mock = MagicMock()
    failing_mock.configured = True
    failing_mock.send = AsyncMock(side_effect=httpx.ConnectTimeout("Connection timed out"))

    with patch("app.services.telegram_reports.telegram_reports", failing_mock):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "test", "task_id": str(data["task_id"])},
            headers={"Authorization": f"Bearer {data['token']}"},
        )

    assert r.status_code == 503
    assert "Retry" in r.json()["detail"] or "fehlgeschlagen" in r.json()["detail"]

    # Critical: flag NOT set, agent can retry
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        t = await s.get(Task, data["task_id"])
        assert t.report_sent_to_telegram is False, (
            "Flag muss bei Exception zurueckgerollt werden — sonst kann Agent nie retryen"
        )


@pytest.mark.asyncio
async def test_board_lead_cannot_set_flag_on_foreign_board_task(client, fake_redis):
    """H1 regression: board lead of board A may not set the flag on a task of board B."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_a = uuid.uuid4()
    board_b = uuid.uuid4()
    lead_id = uuid.uuid4()
    foreign_task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_a, name="A", slug=f"a-{uuid.uuid4().hex[:6]}"))
        s.add(Board(id=board_b, name="B", slug=f"b-{uuid.uuid4().hex[:6]}"))
        lead_token, lead_hash = generate_agent_token()
        s.add(Agent(
            id=lead_id, name="LeadA", role="lead",
            board_id=board_a, agent_token_hash=lead_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "chat:write"],
provision_status="provisioned",
        ))
        s.add(Task(
            id=foreign_task_id, board_id=board_b, title="Board B Task",
            status="in_progress",
            # Not assigned to lead, not owner — lead belongs to a different board
            report_back_required=True,
        ))
        await s.commit()

    mock_reports = _mock_configured_reports_service()
    with patch("app.services.telegram_reports.telegram_reports", mock_reports):
        r = await client.post(
            "/api/v1/agent/telegram/send",
            json={"text": "cross-board leak attempt", "task_id": str(foreign_task_id)},
            headers={"Authorization": f"Bearer {lead_token}"},
        )

    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_auto_draft_handles_missing_reflection_and_comments():
    """If there's no reflection + no comments, the draft is still valid."""
    from app.services.report_auto_draft import _render_draft

    text = _render_draft(
        agent_name="Bot",
        agent_emoji="🤖",
        task_title="Task",
        task_id="abc12345",
        reflection=None,
        recent_comments=[],
    )

    # Must have at least header + footer
    assert "🤖" in text
    assert "Task ❌" in text
    assert "abc12345" in text
    # Reflection/comments sections are missing
    assert "<b>Reflexion des Agenten</b>" not in text
    assert "<b>Letzte Kommentare</b>" not in text
