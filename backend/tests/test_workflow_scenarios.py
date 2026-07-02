"""Tests fuer Workflow-Szenarien: Task Status-Transitions und Review-Handoff.

Deckt ab:
- Reviewer-ACK (Regression fuer Loop-Bug: review→in_progress wurde als Rejection behandelt)
- Happy-Path Review-Handoff
- Review-Rejection und Zurueck-an-Developer
- Self-Review Prevention und Edge Cases
- Ungueltige Transitions
- Review-Safeguard (GLM-5 Approved-aber-in_progress Bug)
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


# ── Helpers ──────────────────────────────────────────────────────────────


async def _create_workflow_data(
    *,
    task_status="in_progress",
    task_assigned_to="developer",
    with_reviewer=True,
):
    """Board + Developer + Reviewer + Task erstellen.

    Returns: dict mit board, developer, reviewer, task, dev_token, reviewer_token.
    """
    from app.models.board import Board
    from app.models.agent import Agent
    from app.models.task import Task
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    dev_id = uuid.uuid4()
    reviewer_id = uuid.uuid4()
    task_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Workflow Board", slug="workflow")
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

        reviewer = None
        reviewer_token_raw = None
        if with_reviewer:
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

        assigned_id = dev_id
        if task_assigned_to == "reviewer" and with_reviewer:
            assigned_id = reviewer_id

        task = Task(
            id=task_id,
            board_id=board_id,
            title="Implement feature X",
            status=task_status,
            assigned_agent_id=assigned_id,
        )
        s.add(task)
        # Evidence-Guard + Reflection-Guard (ADR-023): beide Kommentare
        # muessen vor einer Closing-Transition (review/done) existieren.
        if task_status == "in_progress":
            from app.models.task import TaskComment
            s.add(TaskComment(
                task_id=task_id, author_type="agent", author_agent_id=assigned_id,
                comment_type="progress", content="Implementation complete",
            ))
            s.add(TaskComment(
                task_id=task_id, author_type="agent", author_agent_id=assigned_id,
                comment_type="reflection",
                content=(
                    "## Was wurde gemacht\nFeature X implementiert\n\n"
                    "## Was hat funktioniert\nTDD, dann Refactor\n\n"
                    "## Was war unklar\nNichts — klare Vorgaben\n\n"
                    "## Lesson fuer Agent-Memory\n"
                    "Review-Handoff braucht Evidence + Reflection-Kommentar."
                ),
            ))
        await s.commit()
        await s.refresh(board)
        await s.refresh(developer)
        await s.refresh(task)
        if reviewer:
            await s.refresh(reviewer)

    return {
        "board": board,
        "developer": developer,
        "reviewer": reviewer,
        "task": task,
        "dev_token": dev_token_raw,
        "reviewer_token": reviewer_token_raw,
    }


# ── Gruppe A: Reviewer-ACK (Regression) ─────────────────────────────────


@pytest.mark.asyncio
async def test_reviewer_ack_does_not_trigger_rejection(client, fake_redis):
    """Regression: Reviewer ACKt review→in_progress — darf NICHT als Rejection behandelt werden."""
    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock) as mock_rejection:
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {data['reviewer_token']}"},
                )

    assert resp.status_code == 200, resp.text
    mock_rejection.assert_not_called()

    # Task bleibt beim Reviewer
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "in_progress"
        assert updated.assigned_agent_id == data["reviewer"].id


@pytest.mark.asyncio
async def test_reviewer_ack_sets_ack_timestamp(client, fake_redis):
    """Reviewer-ACK setzt ack_at und started_at."""
    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock):
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {data['reviewer_token']}"},
                )

    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.ack_at is not None, "ack_at should be set on ACK"
        assert updated.started_at is not None, "started_at should be set on ACK"


# ── Gruppe B: Happy-Path Review ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_review_handoff(client, fake_redis):
    """Developer setzt in_progress→review — handle_review_handoff wird aufgerufen."""
    data = await _create_workflow_data(
        task_status="in_progress", task_assigned_to="developer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_handoff", new_callable=AsyncMock) as mock_handoff:
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "review"},
                    headers={"Authorization": f"Bearer {data['dev_token']}"},
                )

    assert resp.status_code == 200, resp.text
    mock_handoff.assert_called_once()


@pytest.mark.asyncio
async def test_reviewer_approves_task_to_done(client, fake_redis):
    """Reviewer setzt review→done (Approval) — completed_at wird gesetzt."""
    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "done"},
                headers={"Authorization": f"Bearer {data['reviewer_token']}"},
            )

    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "done"
        assert updated.completed_at is not None


# ── Gruppe C: Review-Rejection ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_assigned_agent_rejects_triggers_rejection(client, fake_redis):
    """Board Lead setzt review→in_progress auf fremdem Task — Rejection wird ausgeloest."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="developer",
    )

    # Board Lead erstellen
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=uuid.uuid4(),
            name="Henry",
            board_id=data["board"].id,
            agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(lead)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock) as mock_rejection:
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {lead_token_raw}"},
                )

    assert resp.status_code == 200, resp.text
    mock_rejection.assert_called_once()


@pytest.mark.asyncio
async def test_rejection_reassigns_to_original_developer(client, fake_redis):
    """Unit-Test: handle_review_rejection findet Developer und weist Task zurueck."""
    from app.models.activity import ActivityEvent
    from app.services.task_lifecycle import handle_review_rejection

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    # Activity Event erstellen das den Developer als letzten Bearbeiter identifiziert
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        event = ActivityEvent(
            id=uuid.uuid4(),
            event_type="task.status_changed",
            title="Status change",
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            detail={"old_status": "in_progress", "new_status": "review"},
        )
        s.add(event)
        await s.commit()

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        if True:  # Phase 29: gateway rpc patch removed
            with patch("app.services.dispatch._build_dispatch_message", new_callable=AsyncMock, return_value="msg"):
                async with AsyncSession(test_engine, expire_on_commit=False) as s:
                    task = await s.get(data["task"].__class__, data["task"].id)
                    reviewer = await s.get(data["reviewer"].__class__, data["reviewer"].id)
                    result = await handle_review_rejection(
                        s, task, data["board"].id, rejecting_agent=reviewer,
                    )

    assert result is not None
    assert result.id == data["developer"].id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.assigned_agent_id == data["developer"].id
        # Phase 29: re-dispatch is scheduled via asyncio.create_task(auto_dispatch_task(...))
        # so dispatched_at is cleared here; the bg dispatcher will set it after delivery.
        assert updated.dispatched_at is None
        assert updated.ack_at is None  # Reset for new ACK cycle
        assert updated.status == "inbox"


@pytest.mark.asyncio
async def test_rejection_busy_dev_queues_task(client, fake_redis):
    """Bei Rejection: Developer busy → Task wird in Queue gelegt (inbox)."""
    from app.models.task import Task as TaskModel
    from app.models.activity import ActivityEvent
    from app.services.task_lifecycle import handle_review_rejection

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    # Developer hat einen aktiven Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        active_task = TaskModel(
            id=uuid.uuid4(),
            board_id=data["board"].id,
            title="Active task",
            status="in_progress",
            assigned_agent_id=data["developer"].id,
        )
        s.add(active_task)

        event = ActivityEvent(
            id=uuid.uuid4(),
            event_type="task.status_changed",
            title="Status change",
            board_id=data["board"].id,
            task_id=data["task"].id,
            agent_id=data["developer"].id,
            detail={"old_status": "in_progress", "new_status": "review"},
        )
        s.add(event)
        await s.commit()

    with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_queue.enqueue_task", new_callable=AsyncMock) as mock_enqueue:
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                task = await s.get(data["task"].__class__, data["task"].id)
                reviewer = await s.get(data["reviewer"].__class__, data["reviewer"].id)
                result = await handle_review_rejection(
                    s, task, data["board"].id, rejecting_agent=reviewer,
                )

    assert result is not None
    assert result.id == data["developer"].id

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "inbox", "Busy dev → task goes to inbox queue"

    mock_enqueue.assert_called_once()


# ── Gruppe D: Edge Cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_self_review_prevention(client, fake_redis):
    """Developer == einziger Reviewer → handle_review_handoff gibt None zurueck."""
    from app.services.task_lifecycle import handle_review_handoff

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="developer", with_reviewer=False,
    )

    with patch("app.routers.agent_scoped._find_reviewer", new_callable=AsyncMock, return_value=data["developer"]):
        with patch("app.services.task_lifecycle.emit_event", new_callable=AsyncMock):
            async with AsyncSession(test_engine, expire_on_commit=False) as s:
                task = await s.get(data["task"].__class__, data["task"].id)
                developer = await s.get(data["developer"].__class__, data["developer"].id)
                result = await handle_review_handoff(
                    s, task, data["board"].id, developer=developer,
                )

    assert result is None, "Self-review should be prevented"


@pytest.mark.asyncio
async def test_done_to_in_progress_triggers_rejection(client, fake_redis):
    """done→in_progress durch Board Lead → Rejection wird ausgeloest (kein ACK-Guard fuer done)."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    data = await _create_workflow_data(
        task_status="done", task_assigned_to="reviewer",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=uuid.uuid4(),
            name="Henry",
            board_id=data["board"].id,
            agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(lead)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock) as mock_rejection:
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {lead_token_raw}"},
                )

    # done→in_progress is blocked via agent API (UI-only transition)
    assert resp.status_code == 400
    assert "manuell" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_user_test_to_in_progress_triggers_rejection(client, fake_redis):
    """user_test→in_progress → Rejection wird ausgeloest."""
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    data = await _create_workflow_data(
        task_status="user_test", task_assigned_to="reviewer",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=uuid.uuid4(),
            name="Henry",
            board_id=data["board"].id,
            agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write"],
        )
        s.add(lead)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.handle_review_rejection", new_callable=AsyncMock) as mock_rejection:
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {lead_token_raw}"},
                )

    assert resp.status_code == 200, resp.text
    mock_rejection.assert_called_once()


# ── Gruppe E: Ungueltige Transitions ─────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_transition_blocked_to_done(client, fake_redis):
    """blocked→done ist nicht erlaubt → 400."""
    data = await _create_workflow_data(
        task_status="blocked", task_assigned_to="developer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_invalid_transition_inbox_to_review(client, fake_redis):
    """inbox→review ist nicht erlaubt → 400."""
    data = await _create_workflow_data(
        task_status="inbox", task_assigned_to="developer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "review"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_invalid_transition_failed_to_done(client, fake_redis):
    """failed→done ist nicht erlaubt → 400."""
    data = await _create_workflow_data(
        task_status="failed", task_assigned_to="developer",
    )

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        resp = await client.patch(
            f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {data['dev_token']}"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_review_safeguard_corrects_approved_to_done(client, fake_redis):
    """Reviewer setzt in_progress aber letzter Kommentar sagt 'Approved' → korrigiert zu done."""
    from app.models.task import TaskComment

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    # Reviewer-Kommentar mit "Approved" erstellen
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comment = TaskComment(
            task_id=data["task"].id,
            author_agent_id=data["reviewer"].id,
            author_type="agent",
            comment_type="progress",
            content="Code sieht gut aus. Approved!",
        )
        s.add(comment)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            resp = await client.patch(
                f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                json={"status": "in_progress"},
                headers={"Authorization": f"Bearer {data['reviewer_token']}"},
            )

    assert resp.status_code == 200

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        updated = await s.get(Task, data["task"].id)
        assert updated.status == "done", "Safeguard should correct to done when comment says Approved"
        assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_review_safeguard_uses_corrected_status_for_done_side_effects(client, fake_redis):
    """Safeguard-korrigiertes done muss auch Auto-Memory + Feedback-Approval triggern."""
    from app.models.task import TaskComment

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        comment = TaskComment(
            task_id=data["task"].id,
            author_agent_id=data["reviewer"].id,
            author_type="agent",
            comment_type="progress",
            content="Approved, bitte mergen.",
        )
        s.add(comment)
        await s.commit()

    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch("app.services.task_lifecycle.trigger_auto_memory") as mock_auto_memory:
                with patch("app.services.task_lifecycle.trigger_feedback_lesson", new_callable=AsyncMock) as mock_feedback:
                    resp = await client.patch(
                        f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                        json={"status": "in_progress"},
                        headers={"Authorization": f"Bearer {data['reviewer_token']}"},
                    )

    assert resp.status_code == 200
    mock_auto_memory.assert_called_once()
    assert mock_auto_memory.call_args.args[1:] == ("done", "review")
    mock_feedback.assert_awaited_once()
    assert mock_feedback.await_args.args[2:] == ("done", "review")


@pytest.mark.asyncio
async def test_review_safeguard_uses_corrected_status_for_pipeline_sync(client, fake_redis):
    """Safeguard-korrigiertes done muss den Pipeline-Sync triggern."""
    from app.models.content import ContentPipeline
    from app.models.task import Task, TaskComment

    data = await _create_workflow_data(
        task_status="review", task_assigned_to="reviewer",
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pipeline = ContentPipeline(
            id=uuid.uuid4(),
            board_id=data["board"].id,
            title="Pipeline X",
        )
        s.add(pipeline)
        await s.commit()
        await s.refresh(pipeline)

        task = await s.get(Task, data["task"].id)
        task.pipeline_id = pipeline.id
        s.add(task)

        comment = TaskComment(
            task_id=data["task"].id,
            author_agent_id=data["reviewer"].id,
            author_type="agent",
            comment_type="progress",
            content="Approved, bitte mergen.",
        )
        s.add(comment)
        await s.commit()

    # Seit der Vertical-Extraktion läuft pipeline_sync über die Hook-Registry
    # (app.verticals.hooks) — gepatcht wird die Registry, nicht das Modul,
    # weil register() die Funktions-Referenz beim App-Aufbau captured.
    from app.verticals import hooks as vertical_hooks

    mock_sync = AsyncMock()
    with patch("app.routers.agent_scoped.emit_event", new_callable=AsyncMock):
        with patch("app.services.task_lifecycle.update_agent_active_task", new_callable=AsyncMock):
            with patch.object(vertical_hooks, "task_done_hooks", [mock_sync]):
                resp = await client.patch(
                    f"/api/v1/agent/boards/{data['board'].id}/tasks/{data['task'].id}",
                    json={"status": "in_progress"},
                    headers={"Authorization": f"Bearer {data['reviewer_token']}"},
                )

    assert resp.status_code == 200
    mock_sync.assert_awaited_once()
