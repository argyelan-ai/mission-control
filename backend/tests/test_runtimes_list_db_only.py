"""GET /runtimes?probe=false answers from the DB alone, and a dead box costs 2 s.

Round-2 user test (t4 S1, S-neu-1, S-neu-7): the runtimes page and the agent
page waited 5.1 s for `GET /api/v1/runtimes`, and the "Models" tab once 10.9 s
for the recipe lists. Measured cause: one sleeping box. Its packets are
dropped, so every HTTP probe to it ran into the full 5 s timeout while
waiting for the TCP connect — and both the list and every box's recipe list
probe the whole fleet.

Two fixes, both tested here:

* ``probe=false`` returns the same rows straight from the DB, marked
  ``container_status = "not_probed"`` — for readers that only need the row
  (the agent page's runtime pill) and as the runtimes page's first paint
  while the live probes are still running. ``probe`` defaults to true, so
  every existing caller (and the recipe switcher) keeps the live state.
* ``_probe_http`` gives the TCP connect its own short limit. A box that
  answers at all connects in milliseconds (measured: LAN 5–30 ms, cloud with
  TLS ~170 ms); only a box that drops packets runs into the limit.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.models.runtime import Runtime
from app.services import runtime_manager


def _rt(slug: str, order: int, **extra) -> Runtime:
    return Runtime(
        slug=slug,
        display_name=slug.title(),
        runtime_type="openai_compatible",
        endpoint=f"http://{slug}.example.com/v1",
        ui_order=order,
        enabled=True,
        **extra,
    )


@pytest.mark.asyncio
async def test_probe_false_never_probes_and_marks_rows_not_probed(async_session, auth_client):
    async_session.add(_rt("alpha", 0, max_context_len=1_048_576, model_identifier="m-1"))
    async_session.add(_rt("beta", 1))
    await async_session.commit()

    async def must_not_probe(*_args, **_kwargs):
        raise AssertionError("probe=false must not touch the network")

    with patch("app.services.runtime_manager.get_runtime_state", side_effect=must_not_probe):
        resp = await auth_client.get("/api/v1/runtimes", params={"probe": "false"})

    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["runtimes"]}
    assert set(rows) == {"alpha", "beta"}
    for row in rows.values():
        assert row["state"] == "unknown"
        assert row["http_reachable"] is False
        assert row["container_status"] == "not_probed"
    # The row itself is complete — the agent page's pill reads exactly these.
    assert rows["alpha"]["max_context_len"] == 1_048_576
    assert rows["alpha"]["model_identifier"] == "m-1"
    assert "provider_label" in rows["alpha"]
    assert "member_hosts" in rows["alpha"]


@pytest.mark.asyncio
async def test_default_still_probes_live(async_session, auth_client):
    """Every existing caller keeps the live state — probe defaults to true."""
    async_session.add(_rt("alpha", 0))
    await async_session.commit()

    async def ready(*_args, **_kwargs):
        return {"state": "ready", "http_reachable": True, "container_status": None}

    with patch("app.services.runtime_manager.get_runtime_state", side_effect=ready) as probe:
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    assert probe.call_count == 1
    assert resp.json()["runtimes"][0]["state"] == "ready"


class _CapturingClient:
    """Stands in for httpx.AsyncClient and records the timeout it was given."""

    seen: list[httpx.Timeout] = []

    def __init__(self, *_args, timeout=None, **_kwargs):
        _CapturingClient.seen.append(httpx.Timeout(timeout))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _url):
        return httpx.Response(200)


@pytest.mark.asyncio
async def test_http_probe_gives_up_on_a_silent_box_after_the_connect_limit():
    _CapturingClient.seen = []
    with patch.object(runtime_manager.httpx, "AsyncClient", _CapturingClient):
        assert await runtime_manager._probe_http("http://box.example.com:8000/v1", "/v1/models")

    (timeout,) = _CapturingClient.seen
    # A dropped connect costs the connect limit, not the whole 5 s.
    assert timeout.connect is not None and timeout.connect <= 2.0
    # A box that IS connected keeps the full 5 s to answer (a busy engine).
    assert timeout.read == 5.0


def test_connect_limit_stays_far_above_a_healthy_connect():
    """Measured 23.10.2026: LAN connect 5–30 ms, cloud connect + TLS ~170 ms.
    The limit must stay well clear of both, or a slow-but-alive box would be
    reported unreachable."""
    assert runtime_manager._PROBE_CONNECT_TIMEOUT_S >= 1.0
