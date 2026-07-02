from datetime import timedelta

from app.config import settings
from app.main import app
from app.utils import utcnow


async def test_health_openapi_describes_response_payload():
    app.openapi_schema = None
    openapi = app.openapi()

    description = openapi["paths"]["/health"]["get"].get("description")

    assert description is not None
    assert "status" in description
    assert "version" in description
    assert "review_monitoring" in description


async def test_health_uses_app_version_as_single_source_of_truth(
    client, monkeypatch, make_board, make_task
):
    board = await make_board()
    now = utcnow()

    await make_task(
        board_id=board.id,
        title="Review task",
        status="review",
        updated_at=now - timedelta(minutes=5),
    )

    monkeypatch.setattr(settings, "app_version", "9.9.9-test")

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["version"] == app.version
    assert response.json()["version"] != settings.app_version


async def test_health_includes_review_monitoring(client, make_board, make_task):
    board = await make_board()
    now = utcnow()

    await make_task(
        board_id=board.id,
        title="Fresh review",
        status="review",
        updated_at=now - timedelta(minutes=15),
    )
    await make_task(
        board_id=board.id,
        title="Old review",
        status="review",
        updated_at=now - timedelta(minutes=90),
    )
    await make_task(
        board_id=board.id,
        title="Not in review",
        status="done",
        updated_at=now - timedelta(minutes=180),
    )

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": settings.app_version,
        "review_monitoring": {
            "review_tasks_count": 2,
            "oldest_review_task_age_minutes": 90,
        },
    }
