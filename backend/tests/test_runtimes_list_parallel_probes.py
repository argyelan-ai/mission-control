"""GET /runtimes probes every runtime at the same time, with a per-probe limit.

The list endpoint used to await one live probe (SSH + HTTP) after the other,
so the page waited for the SUM of all probes — two sleeping boxes with a 5 s
HTTP timeout each were enough for a 14 s blank page. The probes are
independent, so the endpoint now waits for the SLOWEST one only, and a probe
that never answers is cut off and reported as "unknown — probe timed out"
instead of holding the whole list hostage.
"""
import asyncio
import time
from unittest.mock import patch

import pytest

from app.models.runtime import Runtime


def _rt(slug: str, order: int) -> Runtime:
    return Runtime(
        slug=slug,
        display_name=slug.title(),
        runtime_type="openai_compatible",
        endpoint=f"http://{slug}.example.com/v1",
        ui_order=order,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_list_waits_for_the_slowest_probe_not_the_sum(async_session, auth_client):
    """Three probes of 1.0 s each: sequential = 3.0 s, parallel ≈ 1.0 s."""
    for i, slug in enumerate(("alpha", "beta", "gamma")):
        async_session.add(_rt(slug, i))
    await async_session.commit()

    async def slow_state(*_args, **_kwargs):
        await asyncio.sleep(1.0)
        return {"state": "ready", "http_reachable": True, "container_status": None}

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=slow_state,
    ):
        started = time.perf_counter()
        resp = await auth_client.get("/api/v1/runtimes")
        elapsed = time.perf_counter() - started

    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["runtimes"]}
    assert set(rows) == {"alpha", "beta", "gamma"}
    assert all(r["state"] == "ready" for r in rows.values())
    # Sum would be 3.0 s; the slowest single probe is 1.0 s. 2.0 s leaves
    # generous room for a slow CI runner without letting a sequential loop pass.
    assert elapsed < 2.0, f"probes ran one after another ({elapsed:.2f}s)"


@pytest.mark.asyncio
async def test_hanging_probe_is_reported_unknown_and_does_not_block(async_session, auth_client):
    """A probe that never answers is cut off at the limit; the rest still report."""
    async_session.add(_rt("alpha", 0))
    async_session.add(_rt("beta", 1))
    await async_session.commit()

    async def state_by_slug(runtime, *_args, **_kwargs):
        if runtime["slug"] == "beta":
            await asyncio.sleep(30)  # a box that never answers
        return {"state": "ready", "http_reachable": True, "container_status": None}

    with patch(
        "app.services.runtime_manager.get_runtime_state",
        side_effect=state_by_slug,
    ), patch("app.routers.runtimes._STATE_PROBE_TIMEOUT_S", 0.3):
        started = time.perf_counter()
        resp = await auth_client.get("/api/v1/runtimes")
        elapsed = time.perf_counter() - started

    assert resp.status_code == 200, resp.text
    assert elapsed < 2.0, f"hanging probe blocked the list ({elapsed:.2f}s)"
    rows = {r["slug"]: r for r in resp.json()["runtimes"]}
    assert rows["alpha"]["state"] == "ready"
    hung = rows["beta"]
    assert hung["state"] == "unknown"
    assert hung["http_reachable"] is False
    assert hung["container_status"] == "probe_timeout"
    assert hung["state_message"] == "probe timed out"


def test_limit_covers_the_longest_honest_probe_and_stays_below_ssh_connect():
    """The limit must never turn an honest multi-step answer into "unknown".

    The longest probe that still ends in a real answer is the power-managed
    box: control-port check (3 s HTTP timeout) THEN the model probe (5 s HTTP
    timeout) = 8 s for "booted, no model" — which is what shows the Start
    button. SSH + HTTP probes (docker/ssh_process/unsloth/lmstudio) are one
    SSH round trip plus the same 5 s. The limit has to clear that, and stay
    below the 10 s SSH connect timeout, so a hung box is still cut off before
    it costs the page a full connect attempt.
    """
    from app.routers import runtimes as runtimes_router

    power_managed_worst_case = 3.0 + 5.0
    ssh_connect_timeout = 10.0
    assert power_managed_worst_case < runtimes_router._STATE_PROBE_TIMEOUT_S < ssh_connect_timeout
