"""The Mac actions ("Open on the Mac", "Show in Finder") — only for a viewer at the Mac itself,
and an honest answer (round-2 user tests, fix package Q4).

  t3 S1 / r1 S4 / t4 / r2 N6 — the buttons were decided by CSS alone; a
      laptop over Tailscale saw them and a click opened the file unseen on
      the Mac mini. The endpoints now refuse a caller that did not open MC
      via a loopback name (403). The client IP cannot tell: Tailscale Serve,
      Caddy and the Next proxy all hand the backend a local address. The
      browser's Origin header (and, without one, the forwarded/own Host) can.
  t3 S1 addition — a failing Mac helper was swallowed and answered
      ``{"ok": true}``; now it is a 502.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient
from starlette.requests import Request

from app.config import settings
from app.services import host_open
from tests.conftest import test_engine  # noqa: F401 — shared SQLite engine for auth_client

LOCAL = {"Origin": "http://localhost:3000"}
REMOTE = {"Origin": "https://mc.example.ts.net"}


def _request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "client": ("127.0.0.1", 5)})


# ── the rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost."],
)
def test_loopback_names(host):
    assert host_open.is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["mc.example.ts.net", "203.0.113.20", "198.51.100.7", "mac-mini.local", "localhost.example.com",
     "127.0.0.1.example.com", "", None],
)
def test_other_names(host):
    assert host_open.is_loopback_host(host) is False


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        # A browser always sends Origin on a POST: it is the page's own address.
        ({"Origin": "http://localhost:3000", "Host": "backend:8000"}, True),
        ({"Origin": "http://127.0.0.1", "Host": "localhost"}, True),
        ({"Origin": "http://[::1]:3000"}, True),
        # Over Tailscale / LAN: the proxies hand over a local Host — Origin tells.
        ({"Origin": "https://mc.example.ts.net", "Host": "localhost"}, False),
        ({"Origin": "http://203.0.113.20", "Host": "127.0.0.1:8000", "X-Forwarded-Host": "localhost"}, False),
        # A sandboxed page (opaque origin) is never the operator at the Mac.
        ({"Origin": "null", "Host": "localhost"}, False),
        # No Origin (a script, curl): the forwarded host first, then Host.
        ({"Host": "localhost:8000"}, True),
        ({"Host": "localhost:8000", "X-Forwarded-Host": "mc.example.ts.net"}, False),
        ({"Host": "127.0.0.1:8000", "X-Forwarded-Host": "localhost:3000, proxy.example"}, True),
        ({"Host": "mc.example.ts.net"}, False),
        ({}, False),
    ],
)
def test_request_is_from_host(headers, expected):
    assert host_open.request_is_from_host(_request(headers)) is expected


# ── deliverable open ────────────────────────────────────────────────────────


async def _deliverable(path: str):
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task

    board_id, task_id, agent_id, del_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with _AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name="Test", slug=f"test-{board_id.hex[:6]}"))
        s.add(Task(id=task_id, board_id=board_id, title="T"))
        s.add(Agent(id=agent_id, name="test", role="test"))
        s.add(TaskDeliverable(id=del_id, task_id=task_id, agent_id=agent_id, deliverable_type="file",
                              title="Snake", path=path))
        await s.commit()
    return f"/api/v1/boards/{board_id}/tasks/{task_id}/deliverables/{del_id}/open"


def _helper(*, status: int = 200, raises: Exception | None = None):
    response = MagicMock()
    response.status_code = status
    post = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=response)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.post = post
    return client, post


def _in_docker():
    return patch("app.services.host_open.in_docker", return_value=True)


@pytest.mark.asyncio
async def test_deliverable_open_from_another_computer_is_refused(auth_client: AsyncClient, tmp_path):
    f = tmp_path / "snake.html"
    f.write_text("<p>x</p>")
    url = await _deliverable(str(f))
    client, post = _helper()
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(url, json={"reveal": False}, headers=REMOTE)
    assert resp.status_code == 403
    post.assert_not_called()


@pytest.mark.asyncio
async def test_deliverable_open_at_the_mac_reaches_the_helper(auth_client: AsyncClient, tmp_path):
    f = tmp_path / "snake.html"
    f.write_text("<p>x</p>")
    url = await _deliverable(str(f))
    client, post = _helper()
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(url, json={"reveal": True}, headers=LOCAL)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    post.assert_called_once_with(
        "http://host.docker.internal:8765/open",
        json={"path": os.path.realpath(str(f)), "reveal": True},
        timeout=3.0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "helper",
    [
        {"raises": httpx.ConnectError("refused")},  # helper not running
        {"status": 500},  # helper ran, `open` failed
        {"status": 400},  # helper refused the path
    ],
)
async def test_deliverable_open_reports_a_failing_helper(auth_client: AsyncClient, tmp_path, helper):
    f = tmp_path / "snake.html"
    f.write_text("<p>x</p>")
    url = await _deliverable(str(f))
    client, post = _helper(**helper)
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(url, json={"reveal": False}, headers=LOCAL)
    assert resp.status_code == 502
    assert resp.json() != {"ok": True}
    post.assert_called_once()


@pytest.mark.asyncio
async def test_deliverable_open_without_docker_reports_a_failing_open(auth_client: AsyncClient, tmp_path):
    f = tmp_path / "snake.html"
    f.write_text("<p>x</p>")
    url = await _deliverable(str(f))
    with patch("app.services.host_open.in_docker", return_value=False), \
         patch("app.services.host_open.subprocess.Popen", side_effect=FileNotFoundError("open")):
        resp = await auth_client.post(url, json={"reveal": False}, headers=LOCAL)
    assert resp.status_code == 502


# ── Files page open ─────────────────────────────────────────────────────────


def _vault(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    vault = tmp_path / ".mc" / "vault"
    vault.mkdir(parents=True)
    (vault / "note.md").write_text("# hi")

    async def _reachable():
        return True

    monkeypatch.setattr("app.routers.files._native_open_reachable", _reachable)


@pytest.mark.asyncio
async def test_files_open_from_another_computer_is_refused(auth_client: AsyncClient, tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    client, post = _helper()
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(
            "/api/v1/files/open", json={"root": "vault", "subpath": "note.md", "reveal": True}, headers=REMOTE
        )
    assert resp.status_code == 403
    post.assert_not_called()


@pytest.mark.asyncio
async def test_files_open_at_the_mac_works(auth_client: AsyncClient, tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    client, post = _helper()
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(
            "/api/v1/files/open", json={"root": "vault", "subpath": "note.md", "reveal": True}, headers=LOCAL
        )
    assert resp.status_code == 200
    post.assert_called_once()


@pytest.mark.asyncio
async def test_files_open_reports_a_failing_helper(auth_client: AsyncClient, tmp_path, monkeypatch):
    _vault(tmp_path, monkeypatch)
    client, _ = _helper(raises=httpx.ConnectError("refused"))
    with _in_docker(), patch("httpx.AsyncClient", return_value=client):
        resp = await auth_client.post(
            "/api/v1/files/open", json={"root": "vault", "subpath": "note.md", "reveal": True}, headers=LOCAL
        )
    assert resp.status_code == 502
