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
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", REMOVED)
async def test_removed_route_returns_404(auth_client: AsyncClient, method: str, path: str):
    resp = await auth_client.request(method, path)
    assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"


def test_removed_prefixes_are_not_in_the_router_table():
    from app.main import app

    prefixes = ("/api/v1/playbooks", "/api/v1/automations", "/api/v1/workflows")
    left = sorted(
        getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith(prefixes)
    )
    assert not left, f"removed routes still registered: {left}"


@pytest.mark.asyncio
async def test_skill_lab_survives_the_playbook_removal(auth_client: AsyncClient):
    # Frozen, not removed (landkarte F-skill-lab): it must keep answering.
    resp = await auth_client.get("/api/v1/skill-lab/candidates")
    assert resp.status_code == 200, resp.text
