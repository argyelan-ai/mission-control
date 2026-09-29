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
    # CLI-session leftovers, research router (row 4, row 7).
    ("GET", "/api/v1/cli-sessions"),
    ("POST", "/api/v1/cli-sessions/restart"),
    ("GET", "/api/v1/docker-sessions/00000000-0000-0000-0000-000000000001/state"),
    ("GET", "/api/v1/research"),
    ("POST", "/api/v1/research/start"),
    ("GET", "/api/v1/research/00000000-0000-0000-0000-000000000001/chat"),
    # Board groups, phase create/edit/delete, board-scoped approvals (0209).
    ("GET", "/api/v1/board-groups"),
    ("POST", "/api/v1/board-groups"),
    ("PATCH", "/api/v1/board-groups/00000000-0000-0000-0000-000000000001"),
    ("GET", "/api/v1/boards/00000000-0000-0000-0000-000000000001/approvals"),
]

# Paths that now fall through to a parameterised sibling route (e.g.
# /boards/{id}/tasks/{task_id}) answer 405/422 instead of 404 — what matters
# is that no handler serves them any more.
SHADOWED = [
    ("POST", "/api/v1/projects/00000000-0000-0000-0000-000000000001/phases"),
    ("GET", "/api/v1/boards/00000000-0000-0000-0000-000000000001/tasks/stream"),
    ("GET", "/api/v1/boards/00000000-0000-0000-0000-000000000001/memory/stream"),
    ("PATCH", "/api/v1/projects/00000000-0000-0000-0000-000000000001/phases/00000000-0000-0000-0000-000000000002"),
    ("DELETE", "/api/v1/projects/00000000-0000-0000-0000-000000000001/phases/00000000-0000-0000-0000-000000000002"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", REMOVED)
async def test_removed_route_returns_404(auth_client: AsyncClient, method: str, path: str):
    resp = await auth_client.request(method, path)
    assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"


def _served() -> set[tuple[str, str]]:
    """(METHOD, path) of every documented route, read from the OpenAPI schema.

    Walking ``app.routes`` is not stable across FastAPI/Starlette versions
    (newer ones nest included routers), so the schema is the reliable view of
    what the API serves. The size guard keeps the checks below from passing
    on an empty table.
    """
    from app.main import app

    paths = app.openapi()["paths"]
    served = {(m.upper(), p) for p, ops in paths.items() for m in ops}
    assert len(served) > 200, f"route table looks empty: {len(served)} routes"
    return served


def test_removed_prefixes_are_not_in_the_router_table():
    served = _served()
    prefixes = (
        "/api/v1/playbooks",
        "/api/v1/automations",
        "/api/v1/workflows",
        "/api/v1/webhooks",
        "/api/v1/meetings",
        "/api/v1/discord",
        "/api/v1/cli-sessions",
        "/api/v1/research",
        "/api/v1/board-groups",
    )
    left = sorted(f"{m} {p}" for m, p in served if p.startswith(prefixes))
    removed_exact = {
        ("GET", "/api/v1/agents/runtime-status"),
        ("GET", "/api/v1/docker-sessions/{agent_id}/state"),
        ("GET", "/api/v1/boards/{board_id}/tasks/stream"),
        ("GET", "/api/v1/boards/{board_id}/memory/stream"),
        ("GET", "/api/v1/boards/{board_id}/approvals"),
        # Phase create/update/delete are gone; list and complete stay.
        ("POST", "/api/v1/projects/{project_id}/phases"),
        ("PATCH", "/api/v1/projects/{project_id}/phases/{phase_id}"),
        ("DELETE", "/api/v1/projects/{project_id}/phases/{phase_id}"),
    }
    left += sorted(f"{m} {p}" for m, p in served & removed_exact)
    assert not left, f"removed routes still registered: {left}"


@pytest.mark.asyncio
async def test_runtime_status_is_gone(auth_client: AsyncClient):
    # The path now falls through to GET /agents/{agent_id}, which rejects the
    # non-UUID id — the removed handler must not answer any more.
    resp = await auth_client.get("/api/v1/agents/runtime-status")
    assert resp.status_code in (404, 422), resp.status_code


@pytest.mark.asyncio
async def test_skill_lab_survives_the_playbook_removal(auth_client: AsyncClient):
    # Frozen, not removed (landkarte F-skill-lab): it must keep answering.
    resp = await auth_client.get("/api/v1/skill-lab/candidates")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", SHADOWED)
async def test_shadowed_removed_route_is_not_served(auth_client: AsyncClient, method: str, path: str):
    resp = await auth_client.request(method, path)
    assert resp.status_code in (404, 405, 422), f"{method} {path} -> {resp.status_code}"
    assert "text/event-stream" not in resp.headers.get("content-type", "")


def test_the_live_streams_stay_registered():
    """The two removed board streams must not take the live ones with them."""
    served = _served()
    for live in (
        "/api/v1/activity/stream",
        "/api/v1/agents/stream",
        "/api/v1/approvals/stream",
        "/api/v1/schedule/stream",
        "/api/v1/agents/{agent_id}/chat/stream",
        "/api/v1/groups/{group_id}/stream",
    ):
        assert ("GET", live) in served, f"live stream missing: {live}"


def test_phase_list_and_complete_stay():
    served = _served()
    assert ("GET", "/api/v1/projects/{project_id}/phases") in served
    assert ("POST", "/api/v1/projects/{project_id}/phases/{phase_id}/complete") in served
    assert ("GET", "/api/v1/projects/{project_id}") in served
