"""E5 clean-up: removed API surfaces stay gone.

Each entry is a route of a feature the operator decided to remove (see the
CHANGELOG "Removed" lines). Unknown paths answer 404 before auth runs, so an
authenticated client sees the same 404 an attacker would.
"""
import pytest
from httpx import AsyncClient

REMOVED = [
    # Workflows / automations / playbooks (migration 0207).
    ("GET", "/api/v1/playbooks"),
    ("GET", "/api/v1/playbooks/catalog"),
    ("POST", "/api/v1/playbooks/guided/sessions/start"),
    ("GET", "/api/v1/automations"),
    ("POST", "/api/v1/automations/00000000-0000-0000-0000-000000000001/run"),
    ("GET", "/api/v1/workflows"),
    ("GET", "/api/v1/workflows/stream"),
    # GitHub/local webhooks, meetings, Discord channel API (migration 0208).
    ("POST", "/api/v1/webhooks/github/00000000-0000-0000-0000-000000000001"),
    ("POST", "/api/v1/webhooks/local/push"),
    ("GET", "/api/v1/meetings"),
    ("GET", "/api/v1/meetings/stream"),
    ("GET", "/api/v1/meetings/agent-messages"),
    ("GET", "/api/v1/discord/channels"),
    ("GET", "/api/v1/discord/config"),
    ("POST", "/api/v1/discord/agents/00000000-0000-0000-0000-000000000001/channel"),
    ("POST", "/api/v1/agents/00000000-0000-0000-0000-000000000001/discord-channel"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", REMOVED)
async def test_removed_route_returns_404(auth_client: AsyncClient, method: str, path: str):
    resp = await auth_client.request(method, path)
    assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"


def test_removed_prefixes_are_not_in_the_router_table():
    from app.main import app

    prefixes = (
        "/api/v1/playbooks",
        "/api/v1/automations",
        "/api/v1/workflows",
        "/api/v1/webhooks",
        "/api/v1/meetings",
        "/api/v1/discord",
    )
    left = sorted(
        getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith(prefixes)
    )
    left += sorted(
        getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").endswith("/discord-channel")
    )
    assert not left, f"removed routes still registered: {left}"


@pytest.mark.asyncio
async def test_skill_lab_survives_the_playbook_removal(auth_client: AsyncClient):
    # Frozen, not removed (landkarte F-skill-lab): it must keep answering.
    resp = await auth_client.get("/api/v1/skill-lab/candidates")
    assert resp.status_code == 200, resp.text
