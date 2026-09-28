import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _create_board_and_agent():
    from app.models.agent import Agent
    from app.models.board import Board

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        board = Board(
            id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
            name="Lead Board",
            slug="lead-board",
        )
        agent = Agent(
            id=uuid.UUID("20000000-0000-0000-0000-000000000001"),
            name="Lead",
            board_id=board.id,
        )
        session.add(board)
        session.add(agent)
        await session.commit()
        return board, agent


def _playbook_payload(**overrides):
    payload = {
        "kind": "spec_to_delivery_plan",
        "name": "Delivery Planner",
        "summary": "Turn product specs into build-ready plans.",
        "goal": "Create a clean delivery plan with risks and next actions.",
        "board_id": "10000000-0000-0000-0000-000000000001",
        "default_agent_id": "20000000-0000-0000-0000-000000000001",
        "current_config": {
            "source_text": "Build a playbook layer for the lead on top of the workflow runtime.",
            "target_outcome": "A product-ready implementation blueprint.",
            "scope_mode": "phased",
            "constraints": "Keep OpenClaw as runtime.",
            "include_risks": True,
        },
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_playbook_creates_backing_workflow(auth_client: AsyncClient):
    await _create_board_and_agent()

    resp = await auth_client.post("/api/v1/playbooks", json=_playbook_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "spec_to_delivery_plan"
    assert body["status"] == "draft"
    assert body["workflow_id"] is not None
    assert "Expected output" in body["preview_markdown"]


@pytest.mark.asyncio
async def test_approve_playbook_activates_backing_workflow(auth_client: AsyncClient):
    await _create_board_and_agent()

    create_resp = await auth_client.post("/api/v1/playbooks", json=_playbook_payload())
    playbook_id = create_resp.json()["id"]

    approve_resp = await auth_client.post(f"/api/v1/playbooks/{playbook_id}/approve")
    assert approve_resp.status_code == 200, approve_resp.text
    body = approve_resp.json()
    assert body["status"] == "active"
    assert body["approved_at"] is not None


@pytest.mark.asyncio
async def test_create_automation_and_run_it(auth_client: AsyncClient):
    await _create_board_and_agent()

    playbook_resp = await auth_client.post("/api/v1/playbooks", json=_playbook_payload())
    playbook_id = playbook_resp.json()["id"]

    automation_resp = await auth_client.post(
        f"/api/v1/playbooks/{playbook_id}/automations",
        json={
            "name": "Delivery Planner Daily",
            "status": "active",
            "trigger_type": "manual",
        },
    )
    assert automation_resp.status_code == 201, automation_resp.text
    automation_id = automation_resp.json()["id"]

    run_resp = await auth_client.post(f"/api/v1/automations/{automation_id}/run")
    assert run_resp.status_code == 200, run_resp.text
    assert run_resp.json()["status"] == "running"


@pytest.mark.asyncio
async def test_list_and_update_skill_candidate(auth_client: AsyncClient):
    from app.models.playbook import SkillCandidate

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        candidate = SkillCandidate(
            title="Research synthesis skill",
            summary="Suggested from repeated discovery runs.",
            candidate_type="new_skill",
            proposed_by="system",
            source_run_ids=["run-1", "run-2"],
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        candidate_id = candidate.id

    list_resp = await auth_client.get("/api/v1/skill-lab/candidates")
    assert list_resp.status_code == 200, list_resp.text
    assert len(list_resp.json()) == 1

    update_resp = await auth_client.patch(
        f"/api/v1/skill-lab/candidates/{candidate_id}",
        json={"status": "approved", "target_skill_key": "research-synthesis"},
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()
    assert body["status"] == "approved"
    assert body["target_skill_key"] == "research-synthesis"
    assert body["reviewed_at"] is not None


@pytest.mark.asyncio
async def test_guided_session_start_creates_seed_playbook(auth_client: AsyncClient):
    await _create_board_and_agent()

    resp = await auth_client.post(
        "/api/v1/playbooks/guided/sessions/start",
        json={
            "board_id": "10000000-0000-0000-0000-000000000001",
            "kind": "spec_to_delivery_plan",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["playbook"]["kind"] == "spec_to_delivery_plan"
    assert body["pending_field_key"] == "goal"
    assert len(body["messages"]) == 1
    assert "minimum information needed" in body["messages"][0]["content"]


@pytest.mark.asyncio
async def test_guided_session_message_advances_required_fields(auth_client: AsyncClient):
    await _create_board_and_agent()

    start_resp = await auth_client.post(
        "/api/v1/playbooks/guided/sessions/start",
        json={
            "board_id": "10000000-0000-0000-0000-000000000001",
            "kind": "spec_to_delivery_plan",
        },
    )
    assert start_resp.status_code == 200, start_resp.text
    session_id = start_resp.json()["session"]["id"]

    goal_resp = await auth_client.post(
        f"/api/v1/playbooks/guided/sessions/{session_id}/message",
        json={"content": "Turn rough product notes into a build-ready delivery plan."},
    )
    assert goal_resp.status_code == 200, goal_resp.text
    goal_body = goal_resp.json()
    assert goal_body["playbook"]["goal"] == "Turn rough product notes into a build-ready delivery plan."
    assert goal_body["pending_field_key"] == "source_text"

    source_resp = await auth_client.post(
        f"/api/v1/playbooks/guided/sessions/{session_id}/message",
        json={"content": "We need the lead to turn conversations into reusable playbooks with approval-first activation."},
    )
    assert source_resp.status_code == 200, source_resp.text
    source_body = source_resp.json()
    assert source_body["playbook"]["current_config"]["source_text"].startswith("We need the lead")
    assert source_body["pending_field_key"] == "target_outcome"


@pytest.mark.asyncio
async def test_guided_current_returns_latest_session(auth_client: AsyncClient):
    await _create_board_and_agent()
    board_id = "10000000-0000-0000-0000-000000000001"

    empty = await auth_client.get(f"/api/v1/playbooks/guided/current?board_id={board_id}")
    assert empty.status_code == 200, empty.text
    assert empty.json() is None

    start = await auth_client.post(
        "/api/v1/playbooks/guided/sessions/start",
        json={"board_id": board_id, "kind": "spec_to_delivery_plan"},
    )
    assert start.status_code == 200, start.text

    current = await auth_client.get(f"/api/v1/playbooks/guided/current?board_id={board_id}")
    assert current.status_code == 200, current.text
    assert current.json()["session"]["id"] == start.json()["session"]["id"]


def test_playbook_routes_use_only_product_segments():
    """Every static segment under /api/v1/playbooks is a product word.

    The guided-setup routes once carried a person's name as a path segment and
    shipped in the public OpenAPI spec. An explicit allowlist catches the next
    one without this file having to spell out any name.
    """
    from app.main import app

    allowed = {
        "api", "v1", "playbooks", "catalog", "skill-packs", "approve", "versions",
        "automations", "runs", "recent", "guided", "current", "sessions", "start",
        "message",
    }
    segments = {
        seg
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/playbooks")
        for seg in route.path.strip("/").split("/")
        if seg and not seg.startswith("{")
    }
    assert segments <= allowed, f"unexpected route segments: {sorted(segments - allowed)}"
