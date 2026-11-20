"""Etappe 2 "clean up" (ADR-102 §3): loops are frozen.

Page counter 28.09.–19.11.: /loops 4 views, 0 in the last 14 days; the only
loop's last round was 07.07. Nothing is deleted: existing loops stay readable
and a running one can still be paused or stopped (the safe way out), but
create, start, edit and delete are refused with a clear 410 "frozen".
Way back: `loops.FROZEN = False`.
"""

import pytest
from httpx import AsyncClient

from app.routers import loops as loops_router
from tests.test_loops_l1 import _mk_board, _mk_loop


def _payload(board_id) -> dict:
    return {
        "board_id": str(board_id), "name": "New grind", "goal": "g",
        "backlog_source": "markdown", "backlog_md": "- one item",
    }


@pytest.mark.asyncio
async def test_create_is_refused_with_410_frozen(auth_client: AsyncClient):
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json=_payload(board.id))
    assert r.status_code == 410, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "frozen"
    assert "frozen" in detail["message"].lower()

    # nothing was written
    listed = await auth_client.get("/api/v1/loops", params={"board_id": str(board.id)})
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_existing_loops_stay_readable_pausable_and_stoppable(auth_client: AsyncClient):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused")

    listed = await auth_client.get("/api/v1/loops", params={"board_id": str(board.id)})
    assert [row["id"] for row in listed.json()] == [str(loop.id)]
    detail = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert detail.status_code == 200
    stopped = await auth_client.post(f"/api/v1/loops/{loop.id}/stop")
    assert stopped.status_code == 200, stopped.text

    running = await _mk_loop(board, status="running", name="Still running")
    paused = await auth_client.post(f"/api/v1/loops/{running.id}/pause")
    assert paused.status_code == 200, paused.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method,suffix,body", [
    ("post", "/start", None),
    ("patch", "", {"name": "renamed"}),
    ("delete", "", None),
])
async def test_start_edit_delete_are_refused_and_data_stays(auth_client: AsyncClient, method, suffix, body):
    board = await _mk_board()
    loop = await _mk_loop(board, status="paused", name="Kept")
    kwargs = {"json": body} if body is not None else {}
    r = await getattr(auth_client, method)(f"/api/v1/loops/{loop.id}{suffix}", **kwargs)
    assert r.status_code == 410, r.text
    assert r.json()["detail"]["code"] == "frozen"

    after = await auth_client.get(f"/api/v1/loops/{loop.id}")
    assert after.status_code == 200
    assert after.json()["name"] == "Kept"
    assert after.json()["status"] == "paused"


@pytest.mark.asyncio
async def test_switch_off_frozen_restores_create(auth_client: AsyncClient, monkeypatch):
    monkeypatch.setattr(loops_router, "FROZEN", False)
    board = await _mk_board()
    r = await auth_client.post("/api/v1/loops", json=_payload(board.id))
    assert r.status_code == 201, r.text
