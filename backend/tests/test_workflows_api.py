import uuid
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


def _workflow_payload(**overrides):
    payload = {
        "name": "Weekly Planning Digest",
        "trigger_type": "manual",
        "status": "draft",
        "current_definition": {
            "steps": [
                {
                    "key": "collect",
                    "name": "Collect board data",
                    "step_type": "deterministic",
                    "execution_mode": "single",
                    "executor_type": "internal_api",
                    "executor_config": {"method": "GET", "path": "/api/v1/system/status"},
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def _ai_news_payload(**overrides):
    payload = {
        "name": "AI News Briefing",
        "trigger_type": "scheduled",
        "trigger_config": {"schedule_type": "weekdays", "schedule_time": "08:00"},
        "status": "draft",
        "current_definition": {"steps": []},
        "execution_policy": {
            "workflow_kind": "ai_news_briefing",
            "guided_config": {
                "agent_id": "00000000-0000-0000-0000-000000000111",
                "topic_focus": "Major AI product launches, research releases and policy moves.",
                "custom_instructions": "Keep it concise and Discord-friendly.",
                "timeframe_hours": 24,
                "max_items": 7,
                "source_profile": "balanced",
                "fact_check_level": "strict",
                "include_impacts": True,
                "include_emojis": True,
                "include_openclaw_corner": True,
                "openclaw_items": 2,
            },
        },
    }
    payload.update(overrides)
    return payload


# Phase 30: _create_gateway() helper removed. Gateway model is deleted in
# Plan 30-02 Task 5; workflow_validator.py silently ignores any legacy
# `gateway_id` key in delivery_config payloads (Plan 30-01 D-02). The
# test_create_workflow_version_snapshots_latest_state test below keeps the
# `gateway_id` string in its payload to exercise the silent-ignore path.
async def _create_gateway():
    """No-op shim — Gateway model deleted in Plan 30-02."""
    return None


@pytest.mark.asyncio
async def test_create_workflow_creates_initial_version(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler") as mock_scheduler:
        resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Weekly Planning Digest"
    assert body["current_version"] == 1
    mock_scheduler.register_workflow.assert_not_called()

    from app.models.workflow import WorkflowTemplateVersion

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        result = await session.exec(select(WorkflowTemplateVersion))
        versions = result.all()
        assert len(versions) == 1
        assert versions[0].version == 1


@pytest.mark.asyncio
async def test_create_active_scheduled_workflow_registers_scheduler(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler") as mock_scheduler:
        resp = await auth_client.post(
            "/api/v1/workflows",
            json=_workflow_payload(
                trigger_type="scheduled",
                trigger_config={"schedule_type": "daily", "schedule_time": "07:00"},
                status="active",
            ),
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "active"
    mock_scheduler.register_workflow.assert_called_once()


@pytest.mark.asyncio
async def test_create_active_weekly_workflow_registers_scheduler(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler") as mock_scheduler:
        resp = await auth_client.post(
            "/api/v1/workflows",
            json=_workflow_payload(
                trigger_type="scheduled",
                trigger_config={
                    "schedule_type": "weekly",
                    "schedule_day": "mon",
                    "schedule_time": "08:30",
                },
                status="active",
            ),
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["trigger_config"]["schedule_type"] == "weekly"
    mock_scheduler.register_workflow.assert_called_once()


@pytest.mark.asyncio
async def test_update_workflow_saves_without_creating_new_version(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())
    workflow_id = create_resp.json()["id"]

    with patch("app.routers.workflows.scheduler"):
        update_resp = await auth_client.patch(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "description": "Updated description",
                "change_reason": "Refined wording",
            },
        )

    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["current_version"] == 1
    assert update_resp.json()["description"] == "Updated description"

    from app.models.workflow import WorkflowTemplateVersion

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(WorkflowTemplateVersion).where(WorkflowTemplateVersion.workflow_id == uuid.UUID(workflow_id))
        )
        versions = sorted(result.all(), key=lambda item: item.version)
        assert [version.version for version in versions] == [1]


@pytest.mark.asyncio
async def test_create_workflow_version_snapshots_latest_state(auth_client: AsyncClient):
    await _create_gateway()

    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())
    workflow_id = create_resp.json()["id"]

    update_payload = {
        "description": "Updated description",
        "status": "validated",
        "reflect_on": "always",
        "delivery_config": {
            "delivery_mode": "discord_channel",
            "gateway_id": "11111111-1111-1111-1111-111111111111",
            "channel_id": "123",
            "channel_name": "alerts",
            "deliver_on": "always",
            "delivery_format": "markdown",
        },
    }
    with patch("app.routers.workflows.scheduler"):
        update_resp = await auth_client.patch(f"/api/v1/workflows/{workflow_id}", json=update_payload)
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["current_version"] == 1
    assert update_resp.json()["delivery_config"]["channel_name"] == "alerts"
    assert update_resp.json()["delivery_config"]["delivery_format"] == "markdown"

    version_resp = await auth_client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={"change_reason": "Ready for rollout"},
    )
    assert version_resp.status_code == 200, version_resp.text
    assert version_resp.json()["version"] == 2

    from app.models.workflow import WorkflowTemplateVersion

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        result = await session.exec(
            select(WorkflowTemplateVersion)
            .where(WorkflowTemplateVersion.workflow_id == uuid.UUID(workflow_id))
            .order_by(WorkflowTemplateVersion.version.asc())
        )
        versions = result.all()
        assert [version.version for version in versions] == [1, 2]
        latest = versions[-1]
        assert latest.change_reason == "Ready for rollout"
        snapshot = latest.definition_snapshot
        assert snapshot["description"] == "Updated description"
        assert snapshot["status"] == "validated"
        assert snapshot["reflect_on"] == "always"
        assert snapshot["delivery_config"]["channel_name"] == "alerts"


@pytest.mark.asyncio
async def test_delete_workflow_version_removes_historical_snapshot(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())
    workflow_id = create_resp.json()["id"]

    version_resp = await auth_client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={"change_reason": "Snapshot"},
    )
    assert version_resp.status_code == 200, version_resp.text

    delete_resp = await auth_client.delete(f"/api/v1/workflows/{workflow_id}/versions/1")
    assert delete_resp.status_code == 204, delete_resp.text

    versions_resp = await auth_client.get(f"/api/v1/workflows/{workflow_id}/versions")
    assert versions_resp.status_code == 200
    assert [version["version"] for version in versions_resp.json()] == [2]


@pytest.mark.asyncio
async def test_cannot_delete_current_workflow_version(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())
    workflow_id = create_resp.json()["id"]

    delete_resp = await auth_client.delete(f"/api/v1/workflows/{workflow_id}/versions/1")
    assert delete_resp.status_code == 400
    assert "current workflow version" in delete_resp.text


@pytest.mark.asyncio
async def test_rollback_restores_snapshot_fields_without_creating_new_version(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_workflow_payload())
    workflow_id = create_resp.json()["id"]

    with patch("app.routers.workflows.scheduler"):
        await auth_client.patch(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "description": "Current draft state",
                "status": "validated",
                "reflect_on": "always",
            },
        )

    await auth_client.post(
        f"/api/v1/workflows/{workflow_id}/versions",
        json={"change_reason": "Validated snapshot"},
    )

    with patch("app.routers.workflows.scheduler"):
        await auth_client.patch(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "description": "Changed again",
                "status": "draft",
                "reflect_on": "manual",
            },
        )

    rollback_resp = await auth_client.post(f"/api/v1/workflows/{workflow_id}/rollback/2")
    assert rollback_resp.status_code == 200, rollback_resp.text
    body = rollback_resp.json()
    assert body["current_version"] == 2
    assert body["description"] == "Current draft state"
    assert body["status"] == "validated"
    assert body["reflect_on"] == "always"

    versions_resp = await auth_client.get(f"/api/v1/workflows/{workflow_id}/versions")
    assert versions_resp.status_code == 200
    assert [version["version"] for version in versions_resp.json()] == [2, 1]


@pytest.mark.asyncio
async def test_start_run_creates_run_and_step_rows(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post(
            "/api/v1/workflows",
            json=_workflow_payload(status="active"),
        )
    workflow_id = create_resp.json()["id"]

    with patch("app.services.workflow_service.create_tracked_task") as mock_create_task:
        mock_create_task.side_effect = lambda coro, name=None: coro.close()
        run_resp = await auth_client.post(f"/api/v1/workflows/{workflow_id}/run", json={})

    assert run_resp.status_code == 202, run_resp.text
    run_body = run_resp.json()
    assert run_body["workflow_id"] == workflow_id
    assert run_body["status"] == "running"
    mock_create_task.assert_called_once()

    from app.models.workflow import WorkflowRun, WorkflowStepRun

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        run = await session.get(WorkflowRun, uuid.UUID(run_body["id"]))
        assert run is not None

        result = await session.exec(
            select(WorkflowStepRun).where(WorkflowStepRun.run_id == run.id)
        )
        step_runs = result.all()
        assert len(step_runs) == 1
        assert step_runs[0].step_key == "collect"
        assert step_runs[0].status == "pending"


@pytest.mark.asyncio
async def test_rejects_duplicate_step_keys(auth_client: AsyncClient):
    with patch("app.routers.workflows.scheduler"):
        resp = await auth_client.post(
            "/api/v1/workflows",
            json=_workflow_payload(
                current_definition={
                    "steps": [
                        {
                            "key": "dup",
                            "name": "One",
                            "step_type": "deterministic",
                            "execution_mode": "single",
                            "executor_type": "internal_api",
                            "executor_config": {"method": "GET", "path": "/api/v1/system/status"},
                        },
                        {
                            "key": "dup",
                            "name": "Two",
                            "step_type": "deterministic",
                            "execution_mode": "single",
                            "executor_type": "internal_api",
                            "executor_config": {"method": "GET", "path": "/api/v1/system/status"},
                        },
                    ]
                }
            ),
        )

    assert resp.status_code == 400
    assert "Duplicate step key" in resp.text


@pytest.mark.asyncio
async def test_create_ai_news_guided_workflow_compiles_steps(auth_client: AsyncClient):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                id=uuid.UUID("00000000-0000-0000-0000-000000000111"),
                name="Research Agent",
            )
        )
        await session.commit()

    with patch("app.routers.workflows.scheduler") as mock_scheduler:
        resp = await auth_client.post("/api/v1/workflows", json=_ai_news_payload())

    assert resp.status_code == 201, resp.text
    body = resp.json()
    steps = body["current_definition"]["steps"]
    assert [step["key"] for step in steps] == [
        "openclaw_skills_snapshot",
        "compose_ai_news_briefing",
    ]
    assert steps[-1]["step_type"] == "llm"
    assert body["execution_policy"]["workflow_kind"] == "ai_news_briefing"
    mock_scheduler.register_workflow.assert_not_called()


@pytest.mark.asyncio
async def test_update_ai_news_guided_workflow_rebuilds_prompt(auth_client: AsyncClient):
    from app.models.agent import Agent

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        session.add(
            Agent(
                id=uuid.UUID("00000000-0000-0000-0000-000000000111"),
                name="Research Agent",
            )
        )
        await session.commit()

    with patch("app.routers.workflows.scheduler"):
        create_resp = await auth_client.post("/api/v1/workflows", json=_ai_news_payload())
    workflow_id = create_resp.json()["id"]

    with patch("app.routers.workflows.scheduler"):
        update_resp = await auth_client.patch(
            f"/api/v1/workflows/{workflow_id}",
            json={
                "execution_policy": {
                    "workflow_kind": "ai_news_briefing",
                    "guided_config": {
                        "agent_id": "00000000-0000-0000-0000-000000000111",
                        "topic_focus": "Only cover the most important enterprise AI moves.",
                        "custom_instructions": "No emojis.",
                        "timeframe_hours": 48,
                        "max_items": 5,
                        "source_profile": "official",
                        "fact_check_level": "strict",
                        "include_impacts": True,
                        "include_emojis": False,
                        "include_openclaw_corner": False,
                        "openclaw_items": 1,
                    },
                }
            },
        )

    assert update_resp.status_code == 200, update_resp.text
    steps = update_resp.json()["current_definition"]["steps"]
    assert [step["key"] for step in steps] == ["compose_ai_news_briefing"]
    assert "last 48 hours" in steps[0]["input_template"]
    assert "Do not use emojis." in steps[0]["input_template"]
