import pytest

from app.services.workflow_renderer import render_value


@pytest.mark.asyncio
async def test_render_value_can_use_workflow_board_id(session):
    rendered = await render_value(
        session,
        "/api/v1/boards/{{workflow.board_id}}/snapshot",
        workflow_snapshot={
            "id": "workflow-1",
            "name": "Digest",
            "description": None,
            "board_id": "board-123",
            "project_id": None,
            "trigger_type": "scheduled",
            "trigger_config": {"schedule_type": "weekly", "schedule_day": "mon", "schedule_time": "08:30"},
            "max_runtime_minutes": 60,
            "policy_profile": "safe",
        },
        run={"id": "run-1"},
        context={"steps": {}},
    )

    assert rendered == "/api/v1/boards/board-123/snapshot"
