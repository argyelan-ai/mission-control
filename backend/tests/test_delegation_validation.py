"""Tests fuer Board Lead Delegations-Validierung.

Stellt sicher dass Board Leads keine unvollstaendigen Tasks delegieren koennen.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _setup_delegation_scenario():
    """Board + Board Lead + Developer erstellen mit Agent-Auth-Tokens."""
    from app.models.board import Board
    from app.models.agent import Agent
    from app.auth import generate_agent_token

    board_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    dev_id = uuid.uuid4()

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(id=board_id, name="Test Board", slug="test")
        s.add(board)

        lead_token_raw, lead_token_hash = generate_agent_token()
        lead = Agent(
            id=lead_id,
            name="Henry",
            role="lead",
            board_id=board_id,
            agent_token_hash=lead_token_hash,
            is_board_lead=True,
            scopes=["tasks:read", "tasks:write", "tasks:create", "tasks:manage"],
        )
        s.add(lead)

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

        await s.commit()

    return board_id, lead_id, dev_id, lead_token_raw, dev_token_raw


@pytest.mark.asyncio
async def test_board_lead_delegation_requires_description(client):
    """Board Lead muss bei Delegation an anderen Agent eine Beschreibung angeben."""
    board_id, lead_id, dev_id, lead_token, _ = await _setup_delegation_scenario()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks",
        headers={"Authorization": f"Bearer {lead_token}"},
        json={
            "title": "UI Review machen",
            "assigned_agent_id": str(dev_id),
        },
    )
    assert resp.status_code == 422
    assert "Delegation braucht" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_board_lead_delegation_rejects_short_description(client):
    """Zu kurze Beschreibung wird abgelehnt."""
    board_id, lead_id, dev_id, lead_token, _ = await _setup_delegation_scenario()

    resp = await client.post(
        f"/api/v1/agent/boards/{board_id}/tasks",
        headers={"Authorization": f"Bearer {lead_token}"},
        json={
            "title": "UI Review machen",
            "description": "Mach das bitte.",
            "assigned_agent_id": str(dev_id),
        },
    )
    assert resp.status_code == 422
    assert "mind. 50 Zeichen" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_board_lead_delegation_accepts_detailed_description(client):
    """Ausfuehrliche Beschreibung wird akzeptiert."""
    board_id, lead_id, dev_id, lead_token, _ = await _setup_delegation_scenario()

    with patch('app.services.dispatch.logger') as mock_rpc, \
         patch("app.services.activity.broadcast", new_callable=AsyncMock), \
         patch("app.services.dispatch.engine", test_engine):

        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "UI Review aller MC Seiten",
                "description": (
                    "## Ziel\nAlle MC Seiten auf Desktop und Mobile pruefen.\n\n"
                    "## Kontext\n- URL: http://localhost\n- Stack: Next.js 15\n\n"
                    "## Zugangsdaten\nE-Mail: admin@mc.local / Passwort: test123\n\n"
                    "## Definition of Done\n- Screenshots vorher/nachher\n- PR erstellt"
                ),
                "assigned_agent_id": str(dev_id),
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_non_lead_can_create_task_without_description(client):
    """Normaler Agent darf Tasks ohne Beschreibung erstellen (kein Board Lead Check)."""
    board_id, lead_id, dev_id, _, dev_token = await _setup_delegation_scenario()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks",
            headers={"Authorization": f"Bearer {dev_token}"},
            json={
                "title": "Quick fix fuer Button",
            },
        )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_board_lead_self_assign_no_description_needed(client):
    """Board Lead kann sich selbst Tasks ohne Beschreibung zuweisen."""
    board_id, lead_id, dev_id, lead_token, _ = await _setup_delegation_scenario()

    with patch("app.services.activity.broadcast", new_callable=AsyncMock):
        resp = await client.post(
            f"/api/v1/agent/boards/{board_id}/tasks",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={
                "title": "Eigene Notiz",
                # Kein assigned_agent_id → wird Lead selbst zugewiesen
            },
        )
    assert resp.status_code == 201
