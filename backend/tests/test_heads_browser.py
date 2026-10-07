"""Heads get a browser (ADR-088 harness wiring).

A head's run opens its own browser session at start; the session's addresses
reach the head through head.env (0600, the head's own capability — never in
spec.json). Off by default until the operator turns it on after a live check.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.browser_session import BrowserSession
from app.services import browser_sessions as svc
from app.services.heads import launcher
from tests.conftest import test_engine
from tests.heads_backend_helpers import heads_root  # noqa: F401
from tests.test_heads_api import _probes, _world  # noqa: F401


@pytest.fixture
def gateway(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201 if request.method == "PUT" else 200, json={})

    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(handler))
    return seen


async def _session_for(run_id: str) -> BrowserSession | None:
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        return (await s.exec(select(BrowserSession).where(BrowserSession.head_run_id == run_id))).first()


@pytest.mark.parametrize("harness", ["claude", "omp"])
async def test_a_head_gets_its_own_browser_session(auth_client, heads_root, make_board, make_task,  # noqa: F811
                                                   gateway, monkeypatch, harness):
    monkeypatch.setattr(settings, "heads_browser_enabled", True)
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": harness,
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["run_id"]
    row = await _session_for(run_id)
    assert row is not None and row.owner_kind == "head"
    token = svc.session_token(row.id)
    env = (heads_root / run_id / "head.env").read_text()
    assert f"MC_BROWSER_CDP_URL='http://127.0.0.1:9300/s/{token}/'" in env
    assert f"MC_BROWSER_MCP_URL='http://127.0.0.1:8931/s/{token}/mcp'" in env
    # The address is a credential: never in spec.json (readable everywhere).
    assert token not in (heads_root / run_id / "spec.json").read_text()
    assert any(r.method == "PUT" for r in gateway)


async def test_no_browser_session_while_switched_off(auth_client, heads_root, make_board, make_task, gateway):  # noqa: F811
    assert settings.heads_browser_enabled is False
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "omp",
                                                         "runtime_slug": "box-slot"})
    run_id = resp.json()["run_id"]
    assert await _session_for(run_id) is None
    assert "MC_BROWSER" not in (heads_root / run_id / "head.env").read_text()


async def test_a_head_starts_even_if_the_browser_cannot_be_registered(auth_client, heads_root, make_board,  # noqa: F811
                                                                     make_task, monkeypatch):
    monkeypatch.setattr(settings, "heads_browser_enabled", True)

    def down(request):
        raise httpx.ConnectError("gateway down", request=request)

    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(down))
    _, task = await _world(make_board, make_task)
    resp = await auth_client.post("/api/v1/heads", json={"task_id": str(task.id), "harness": "claude",
                                                         "runtime_slug": "box-slot"})
    assert resp.status_code == 201
    assert "MC_BROWSER_MCP_URL=" in (heads_root / resp.json()["run_id"] / "head.env").read_text()


def test_head_env_keys_match_the_host_script():
    from tests.test_mc_head_parity import _load_mc_head

    assert launcher.HEAD_ENV_KEYS == _load_mc_head().HEAD_ENV_KEYS
    assert {"MC_BROWSER_CDP_URL", "MC_BROWSER_MCP_URL"} <= launcher.HEAD_ENV_KEYS


async def test_ending_a_session_also_stops_its_router_child(session, monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"closedTargets": 0, "disposedContexts": 0, "closedConnections": 0,
                                          "errors": [], "stopped": True})

    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(handler))
    row, _ = await svc.open_session(session, head_run_id=str(uuid.uuid4()))
    await svc.end_session(session, row, reason="run_ended")
    token = svc.session_token(row.id)
    router = [r for r in calls if r.url.path == f"/_router/sessions/{token}"]
    assert len(router) == 1 and router[0].method == "DELETE"
    assert router[0].url.host == re.sub(r"^https?://|:\d+$", "", svc.ROUTER_BASE_URL)


async def test_router_trouble_never_blocks_the_end(session, monkeypatch):
    def handler(request):
        if request.url.path.startswith("/_router/"):
            raise httpx.ConnectError("router down", request=request)
        return httpx.Response(200, json={"closedTargets": 0, "disposedContexts": 0, "closedConnections": 0,
                                          "errors": []})

    monkeypatch.setattr(svc, "_transport", httpx.MockTransport(handler))
    row, _ = await svc.open_session(session, head_run_id=str(uuid.uuid4()))
    await svc.end_session(session, row, reason="run_ended")
    assert row.status == "ended"
