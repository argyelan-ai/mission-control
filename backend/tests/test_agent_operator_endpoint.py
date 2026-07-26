"""GET /api/v1/agent/operator — liefert den Anzeigenamen des Operators.

Jarvis spricht den Operator sonst generisch an ("Operator"), weil der Name
nirgends im System-Prompt steht. Der Endpoint ist bewusst winzig: er gibt
NUR Anzeigename + Zeitzone heraus, keine Mail, keine Rolle, keine ID.
"""
import os
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _make_agent_headers(scopes: list[str] | None = None) -> dict[str, str]:
    """Agent mit tasks:read anlegen und den Bearer-Header dafuer bauen.

    Es gibt keine `agent_headers`-Fixture in conftest.py — agent-scoped Tests
    legen ihren Agenten selbst an (Muster aus test_x_posts_endpoint.py).
    """
    from app.auth import generate_agent_token
    from app.models.agent import Agent
    from app.models.board import Board

    raw_token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        board = Board(name="Jarvis", slug=f"jarvis-{uuid.uuid4().hex[:6]}")
        s.add(board)
        await s.commit()
        await s.refresh(board)

        agent = Agent(
            name="Jarvis",
            role="assistant",
            scopes=scopes or ["tasks:read"],
            board_id=board.id,
            agent_token_hash=token_hash,
        )
        s.add(agent)
        await s.commit()

    return {"Authorization": f"Bearer {raw_token}"}


@pytest.mark.asyncio
async def test_operator_endpoint_returns_preferred_name(client: AsyncClient, session: AsyncSession):
    from app.models.user import User

    headers = await _make_agent_headers()

    user = User(
        id=uuid.uuid4(),
        email="operator-test@local",
        name="Markus Argyelan",
        preferred_name="Mark",
        timezone="Europe/Zurich",
        role="admin",
    )
    session.add(user)
    await session.commit()

    os.environ["JARVIS_OPERATOR_USER_ID"] = str(user.id)
    try:
        resp = await client.get("/api/v1/agent/operator", headers=headers)
    finally:
        os.environ.pop("JARVIS_OPERATOR_USER_ID", None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == "Mark"
    assert body["timezone"] == "Europe/Zurich"
    assert "email" not in body


@pytest.mark.asyncio
async def test_operator_endpoint_falls_back_to_name(client: AsyncClient, session: AsyncSession):
    """Ohne preferred_name gewinnt name — nie ein leerer String."""
    from app.models.user import User

    headers = await _make_agent_headers()

    user = User(
        id=uuid.uuid4(),
        email="operator-noprefer@local",
        name="Nur Name",
        preferred_name=None,
        role="admin",
    )
    session.add(user)
    await session.commit()

    os.environ["JARVIS_OPERATOR_USER_ID"] = str(user.id)
    try:
        resp = await client.get("/api/v1/agent/operator", headers=headers)
    finally:
        os.environ.pop("JARVIS_OPERATOR_USER_ID", None)

    assert resp.json()["name"] == "Nur Name"


@pytest.mark.asyncio
async def test_operator_endpoint_not_configured(client: AsyncClient):
    """Ohne JARVIS_OPERATOR_USER_ID: 200 + ok=False, kein 500.

    Fail-soft ist Absicht — Jarvis muss auch ohne Konfiguration starten und
    faellt dann auf die neutrale Anrede zurueck.
    """
    headers = await _make_agent_headers()

    os.environ.pop("JARVIS_OPERATOR_USER_ID", None)
    resp = await client.get("/api/v1/agent/operator", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "reason": "not_configured"}
