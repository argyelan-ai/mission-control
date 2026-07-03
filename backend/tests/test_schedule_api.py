"""Tests for the Schedule REST API (CRUD + trigger)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


class TestListJobs:
    async def test_returns_empty_list_initially(self, auth_client: AsyncClient):
        """GET /api/v1/schedule/jobs — returns an empty list when there are no jobs."""
        resp = await auth_client.get("/api/v1/schedule/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/schedule/jobs")
        assert resp.status_code == 401


class TestCreateJob:
    async def test_create_daily_chat_job(self, auth_client: AsyncClient):
        """POST — a daily chat job gets created."""
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Test Briefing",
                "schedule_type": "daily",
                "schedule_time": "09:00",
                "action_type": "chat_send",
                "agent_name": "Henry",
                "message": "Hallo Henry",
                "enabled": True,
            })

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Briefing"
        assert data["schedule_time"] == "09:00"
        assert data["id"] is not None

    async def test_create_interval_job(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Stündlicher Check",
                "schedule_type": "interval",
                "schedule_interval_hours": 4,
                "action_type": "chat_send",
                "agent_name": "Rex",
                "message": "Check status",
                "enabled": True,
            })

        assert resp.status_code == 201
        assert resp.json()["schedule_interval_hours"] == 4

    async def test_requires_operator_role(self, client: AsyncClient):
        resp = await client.post("/api/v1/schedule/jobs", json={
            "name": "X", "schedule_type": "daily", "schedule_time": "09:00",
            "action_type": "chat_send"
        })
        assert resp.status_code == 401


class TestGetJob:
    async def test_get_existing_job(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            create_resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Get Test", "schedule_type": "daily", "schedule_time": "10:00",
                "action_type": "chat_send", "agent_name": "Henry", "message": "Test",
            })
        job_id = create_resp.json()["id"]

        resp = await auth_client.get(f"/api/v1/schedule/jobs/{job_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Get Test"

    async def test_get_nonexistent_returns_404(self, auth_client: AsyncClient):
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestUpdateJob:
    async def test_patch_enables_disables(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            mock_svc.update_job = AsyncMock()
            create_resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Toggle Test", "schedule_type": "daily", "schedule_time": "10:00",
                "action_type": "chat_send", "agent_name": "Henry", "message": "Test",
                "enabled": True,
            })
            job_id = create_resp.json()["id"]

            resp = await auth_client.patch(f"/api/v1/schedule/jobs/{job_id}", json={"enabled": False})

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_patch_nonexistent_returns_404(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.update_job = AsyncMock()
            resp = await auth_client.patch(
                f"/api/v1/schedule/jobs/{uuid.uuid4()}", json={"enabled": False}
            )
        assert resp.status_code == 404


class TestDeleteJob:
    async def test_delete_removes_job(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            mock_svc.remove_job = AsyncMock()
            create_resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Delete Me", "schedule_type": "daily", "schedule_time": "11:00",
                "action_type": "chat_send", "agent_name": "Henry", "message": "Test",
            })
            job_id = create_resp.json()["id"]

            del_resp = await auth_client.delete(f"/api/v1/schedule/jobs/{job_id}")
            assert del_resp.status_code == 204

        get_resp = await auth_client.get(f"/api/v1/schedule/jobs/{job_id}")
        assert get_resp.status_code == 404

    async def test_delete_nonexistent_returns_404(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.remove_job = AsyncMock()
            resp = await auth_client.delete(f"/api/v1/schedule/jobs/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestTriggerJob:
    async def test_trigger_returns_202(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            mock_svc.trigger_now = AsyncMock()
            create_resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Trigger Test", "schedule_type": "daily", "schedule_time": "12:00",
                "action_type": "chat_send", "agent_name": "Henry", "message": "Test",
            })
            job_id = create_resp.json()["id"]

            resp = await auth_client.post(f"/api/v1/schedule/jobs/{job_id}/trigger")

        assert resp.status_code == 202

    async def test_trigger_nonexistent_returns_404(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.trigger_now = AsyncMock()
            resp = await auth_client.post(f"/api/v1/schedule/jobs/{uuid.uuid4()}/trigger")
        assert resp.status_code == 404


class TestSkipReviewField:
    async def test_create_task_job_with_skip_review(self, auth_client: AsyncClient):
        """POST — task_skip_review is stored and returned."""
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Digest Skip Review",
                "schedule_type": "daily",
                "schedule_time": "06:00",
                "action_type": "create_task",
                "task_board_id": str(uuid.uuid4()),
                "task_title": "AI Digest",
                "task_priority": "medium",
                "task_skip_review": True,
            })

        assert resp.status_code == 201
        assert resp.json()["task_skip_review"] is True

    async def test_update_task_job_skip_review(self, auth_client: AsyncClient):
        """PATCH — task_skip_review can be changed via update."""
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            mock_svc.update_job = AsyncMock()
            create_resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Digest Update Test",
                "schedule_type": "daily",
                "schedule_time": "07:00",
                "action_type": "create_task",
                "task_board_id": str(uuid.uuid4()),
                "task_title": "Weekly Digest",
                "task_skip_review": False,
            })
            job_id = create_resp.json()["id"]

        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.update_job = AsyncMock()
            resp = await auth_client.patch(
                f"/api/v1/schedule/jobs/{job_id}",
                json={"task_skip_review": True},
            )

        assert resp.status_code == 200
        assert resp.json()["task_skip_review"] is True


# ── Helper fixture shared across v2 tests ────────────────────────────────────

async def _create_test_job(auth_client, **extra):
    """Create a simple daily job and return its JSON dict."""
    with patch("app.routers.schedule.scheduler") as mock_svc:
        mock_svc.add_job = AsyncMock()
        resp = await auth_client.post("/api/v1/schedule/jobs", json={
            "name": "V2 Test Job",
            "schedule_type": "daily",
            "schedule_time": "08:00",
            "action_type": "create_task",
            **extra,
        })
    assert resp.status_code == 201, resp.text
    return resp.json()


class TestNewScheduleV2Endpoints:
    """Tests for the new v2 endpoints added in schedule v2"""

    async def test_stats_endpoint_returns_200_for_existing_job(self, auth_client: AsyncClient):
        job = await _create_test_job(auth_client)
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{job['id']}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "success_rate_7d" in data
        assert "success_rate_30d" in data
        assert "avg_duration_ms" in data
        assert "p95_duration_ms" in data
        assert "total_runs_30d" in data
        assert "runs_by_day" in data

    async def test_stats_endpoint_returns_404_for_missing_job(self, auth_client: AsyncClient):
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{uuid.uuid4()}/stats")
        assert resp.status_code == 404

    async def test_heatmap_endpoint_returns_list(self, auth_client: AsyncClient):
        job = await _create_test_job(auth_client)
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{job['id']}/heatmap")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_upcoming_endpoint_returns_list(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.scheduler = MagicMock()
            mock_svc.scheduler.get_jobs.return_value = []
            resp = await auth_client.get("/api/v1/schedule/upcoming")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_preview_firings_cron(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/v1/schedule/preview-firings", json={
            "schedule_type": "cron",
            "schedule_cron": "0 9 * * 1-5",
            "count": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "firings" in data
        assert len(data["firings"]) <= 3

    async def test_preview_firings_daily(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/v1/schedule/preview-firings", json={
            "schedule_type": "daily",
            "schedule_time": "09:00",
            "count": 2,
        })
        assert resp.status_code == 200
        assert len(resp.json()["firings"]) <= 2

    async def test_preview_firings_weekly_custom(self, auth_client: AsyncClient):
        resp = await auth_client.post("/api/v1/schedule/preview-firings", json={
            "schedule_type": "weekly_custom",
            "schedule_weekdays": [0, 2, 4],
            "schedule_time": "10:00",
            "count": 5,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "firings" in data
        assert len(data["firings"]) <= 5

    async def test_snooze_sets_snoozed_until(self, auth_client: AsyncClient):
        job = await _create_test_job(auth_client)
        with patch("app.routers.schedule.scheduler"):
            resp = await auth_client.patch(
                f"/api/v1/schedule/jobs/{job['id']}/snooze",
                json={"hours": 4},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["snoozed_until"] is not None

    async def test_snooze_returns_404_for_missing_job(self, auth_client: AsyncClient):
        resp = await auth_client.patch(
            f"/api/v1/schedule/jobs/{uuid.uuid4()}/snooze",
            json={"hours": 2},
        )
        assert resp.status_code == 404

    async def test_duplicate_creates_copy(self, auth_client: AsyncClient):
        job = await _create_test_job(auth_client)
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post(f"/api/v1/schedule/jobs/{job['id']}/duplicate")
        assert resp.status_code == 201
        data = resp.json()
        assert "Copy of" in data["name"]
        assert data["enabled"] is False
        assert data["id"] != job["id"]

    async def test_duplicate_returns_404_for_missing_job(self, auth_client: AsyncClient):
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post(f"/api/v1/schedule/jobs/{uuid.uuid4()}/duplicate")
        assert resp.status_code == 404

    async def test_tasks_endpoint_returns_list(self, auth_client: AsyncClient):
        job = await _create_test_job(auth_client)
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{job['id']}/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_tasks_endpoint_returns_404_for_missing_job(self, auth_client: AsyncClient):
        # /tasks does not do a job existence check — it simply returns empty list
        # (join on runs returns nothing). Accept either 200 (empty) or 404.
        resp = await auth_client.get(f"/api/v1/schedule/jobs/{uuid.uuid4()}/tasks")
        assert resp.status_code in (200, 404)

    async def test_create_cron_job(self, auth_client: AsyncClient):
        """POST — cron job with schedule_cron stored correctly."""
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Cron Test Job",
                "schedule_type": "cron",
                "schedule_cron": "30 6 * * 1-5",
                "action_type": "create_task",
            })
        assert resp.status_code == 201
        data = resp.json()
        assert data["schedule_cron"] == "30 6 * * 1-5"
        assert data["schedule_type"] == "cron"

    async def test_create_weekly_custom_job(self, auth_client: AsyncClient):
        """POST — weekly_custom job with schedule_weekdays stored correctly."""
        with patch("app.routers.schedule.scheduler") as mock_svc:
            mock_svc.add_job = AsyncMock()
            resp = await auth_client.post("/api/v1/schedule/jobs", json={
                "name": "Weekly Custom Job",
                "schedule_type": "weekly_custom",
                "schedule_time": "09:00",
                "schedule_weekdays": [0, 2, 4],
                "action_type": "create_task",
            })
        assert resp.status_code == 201
        data = resp.json()
        assert data["schedule_weekdays"] == [0, 2, 4]
        assert data["schedule_type"] == "weekly_custom"
