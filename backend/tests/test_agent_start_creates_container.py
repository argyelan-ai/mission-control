"""Der Start-Knopf eines frisch angelegten Agenten darf nicht ins Leere laufen.

Live-Befund 31.08.2026 (Runtime-Switch-Livetest): ``POST /agents`` legt nur die
DB-Zeile an, ``POST /provision`` schreibt Dateien + compose-Service. Der
Container selbst entsteht erst beim naechsten ``start-all.sh``. Der Start-Knopf
rief blind ``docker start <name>`` und beantwortete den fehlenden Container mit
HTTP 500 ``No such container`` — ein neuer Agent blieb genau hier stecken.

Richtig ist: existiert der Container nicht, wird er ueber die compose-Datei
erzeugt (derselbe Weg, den force-recreate schon geht). Ein vorhandener,
gestoppter Container wird weiterhin schlicht gestartet.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent

from tests.conftest import test_engine


async def _make_agent(name: str) -> uuid.UUID:
    agent_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(id=agent_id, name=name, agent_runtime="cli-bridge"))
        await s.commit()
    return agent_id


@pytest.mark.anyio
async def test_start_creates_container_when_missing(auth_client: AsyncClient):
    """Fehlender Container -> compose erzeugt ihn, kein 500."""
    agent_id = await _make_agent("Start Missing Agent")

    recreate = AsyncMock(
        return_value={"status": "recreated", "container": "mc-agent-start-missing-agent", "mode": "recreate"}
    )
    with patch("app.routers.cli_terminal._get_container_state", AsyncMock(return_value="not-found")), \
         patch("app.routers.cli_terminal._docker_action", AsyncMock()) as docker_action, \
         patch("app.routers.cli_terminal._create_agent_container", recreate):
        resp = await auth_client.post(f"/api/v1/agents/{agent_id}/start")

    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "running"
    recreate.assert_awaited_once()
    docker_action.assert_not_awaited()


@pytest.mark.anyio
async def test_start_uses_docker_start_for_existing_container(auth_client: AsyncClient):
    """Regression: vorhandener, gestoppter Container wird weiterhin gestartet."""
    agent_id = await _make_agent("Start Existing Agent")

    recreate = AsyncMock()
    with patch("app.routers.cli_terminal._get_container_state", AsyncMock(return_value="exited")), \
         patch("app.routers.cli_terminal._docker_action", AsyncMock()) as docker_action, \
         patch("app.routers.cli_terminal._create_agent_container", recreate):
        resp = await auth_client.post(f"/api/v1/agents/{agent_id}/start")

    assert resp.status_code == 200, resp.text
    docker_action.assert_awaited_once()
    assert docker_action.await_args.args[0] == "start"
    recreate.assert_not_awaited()
