"""One honest state per box in the Models tab (round-4 user test, t3 S1 / r2 S1).

The watcher's verdict on a MODEL port ("dark for 3+ probes") is not the
state of the BOX: an awake power-managed box that runs no model read
"asleep or off" in the Models tab while the Fleet tab next to it said
"free · ready". "asleep" now needs the box's own host check to fail; an
awake box with a dark model port is "no_model". "Refetch" (force) follows
the same rule (t3 NICE-4: it answered ``unreachable``, the next GET
``asleep``). A box whose watcher is green but whose /models fails is
remembered instead of re-probed on every page (r1 N4).
"""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.models.host import Host
from app.models.runtime import Runtime
from app.services import host_liveness, model_catalog
from app.utils import utcnow


def mock_httpx(monkeypatch, handler):
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(model_catalog, "_RETRY_BACKOFF", 0)
    return seen


def use_fake_redis(monkeypatch, fake_redis):
    async def _get_redis():
        return fake_redis

    monkeypatch.setattr(model_catalog, "get_redis", _get_redis)
    monkeypatch.setattr(host_liveness, "get_redis", _get_redis)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(model_catalog.settings, "home_host", "/nonexistent-test-home")

    async def fake_creds(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(model_catalog, "resolve_provider_credentials", fake_creds)


def control_server(monkeypatch, awake: bool) -> AsyncMock:
    probe = AsyncMock(return_value=awake)
    monkeypatch.setattr(host_liveness, "_probe_control_server", probe)
    return probe


async def add_host(session, **kwargs) -> Host:
    defaults = {"slug": "box", "display_name": "Box", "kind": "flask_wol"}
    defaults.update(kwargs)
    host = Host(**defaults)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def add_runtime(session, host: Host | None = None, **kwargs) -> Runtime:
    defaults = {
        "slug": "box-model",
        "display_name": "Box model",
        "runtime_type": "vllm_docker",
        "endpoint": "http://192.0.2.77:5559/v1",
        "host_id": host.id if host else None,
    }
    defaults.update(kwargs)
    rt = Runtime(**defaults)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


async def mark_live(fake_redis, slug: str, **doc) -> None:
    await fake_redis.set(model_catalog.RedisKeys.runtime_live(slug), json.dumps(doc))


async def sleeper_box(session) -> Host:
    return await add_host(
        session, slug="sleeper", display_name="Sleeper", kind="flask_wol",
        control_url="http://192.0.2.77:5555", power_managed=True,
    )


def entry_for(providers: list[dict], slug: str) -> dict:
    return next(p for p in providers if p["key"] == f"openai:{slug}")


# ── The box answers, its model port does not ─────────────────────────────────


@pytest.mark.asyncio
async def test_awake_box_without_model_is_not_called_asleep(session, monkeypatch, fake_redis):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    probe = control_server(monkeypatch, awake=True)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=3837)

    entry = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert entry["status"] == model_catalog.STATUS_NO_MODEL
    assert entry["models"] == []
    assert seen == [], "the dark model port is still not probed"
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_box_whose_own_check_fails_is_asleep(session, monkeypatch, fake_redis):
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    control_server(monkeypatch, awake=False)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=12)

    entry = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert entry["status"] == model_catalog.STATUS_ASLEEP


@pytest.mark.asyncio
async def test_metrics_poll_answer_is_reused_without_a_second_check(session, monkeypatch, fake_redis):
    """The host card already asked the box (5 s metrics poll) — the catalog
    reads that answer instead of asking again."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    probe = control_server(monkeypatch, awake=False)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=12)
    await host_liveness.remember(host.id, True)

    entry = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert entry["status"] == model_catalog.STATUS_NO_MODEL
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_heartbeat_means_awake(session, monkeypatch, fake_redis):
    """A box whose node agent beat a moment ago is awake, whatever its kind."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    host = await add_host(
        session, slug="spark", display_name="Spark", kind="ssh",
        agent_last_seen_at=utcnow() - timedelta(seconds=5),
    )
    await add_runtime(session, host, slug="spark-model", endpoint="http://192.0.2.10:8000/v1")
    await mark_live(fake_redis, "spark-model", reachable=False, consecutive_failures=40)

    entry = entry_for(await model_catalog.build_catalog(session), "spark-model")

    assert entry["status"] == model_catalog.STATUS_NO_MODEL


@pytest.mark.asyncio
async def test_dark_port_on_an_always_on_box_is_not_asleep(session, monkeypatch, fake_redis):
    """No power management, no cheap host check: the box is not said to sleep.
    The port does not answer — that is what is shown, without waiting on it."""
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    host = await add_host(session, slug="ssh-box", display_name="SSH box", kind="ssh")
    await add_runtime(session, host, slug="ssh-model", endpoint="http://192.0.2.11:8000/v1")
    await mark_live(fake_redis, "ssh-model", reachable=False, consecutive_failures=9)

    entry = entry_for(await model_catalog.build_catalog(session), "ssh-model")

    assert entry["status"] == model_catalog.STATUS_UNREACHABLE
    assert seen == []


# ── "Refetch" uses the same rule ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refetch_names_an_awake_box_like_the_page_does(session, monkeypatch, fake_redis):
    mock_httpx(
        monkeypatch, lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("dark", request=r))
    )
    use_fake_redis(monkeypatch, fake_redis)
    control_server(monkeypatch, awake=True)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=3837)

    forced = entry_for(await model_catalog.build_catalog(session, force=True), "box-model")
    plain = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert forced["status"] == plain["status"] == model_catalog.STATUS_NO_MODEL


@pytest.mark.asyncio
async def test_refetch_that_finds_a_model_still_shows_it(session, monkeypatch, fake_redis):
    """The rule only renames a FAILED probe — a box that answers on refetch
    shows its models even while the watcher has not caught up yet."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    control_server(monkeypatch, awake=True)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=4)

    forced = entry_for(await model_catalog.build_catalog(session, force=True), "box-model")

    assert forced["status"] == model_catalog.STATUS_OK


# ── Not asked is not "checked a moment ago" ──────────────────────────────────


@pytest.mark.asyncio
async def test_unprobed_entry_has_no_check_time(session, monkeypatch, fake_redis):
    """r2 S1: "checked < 1 min ago" stood next to "not queried"."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    control_server(monkeypatch, awake=True)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=30)

    entry = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert entry["cached_at"] is None


# ── r1 N4: box answers, /models fails — remembered, not re-probed ────────────


@pytest.mark.asyncio
async def test_failure_while_the_box_answers_is_remembered(session, monkeypatch, fake_redis):
    seen = mock_httpx(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    use_fake_redis(monkeypatch, fake_redis)
    await add_runtime(session, slug="sick-model", endpoint="http://192.0.2.12:8000/v1")
    await mark_live(fake_redis, "sick-model", reachable=True, consecutive_failures=0)

    first = entry_for(await model_catalog.build_catalog(session), "sick-model")
    probes = len(seen)
    second = entry_for(await model_catalog.build_catalog(session), "sick-model")

    assert first["status"] == model_catalog.STATUS_UNREACHABLE
    assert second["status"] == model_catalog.STATUS_UNREACHABLE
    assert second["cached"] is True
    assert len(seen) == probes, "a box the watcher already saw answering is not re-probed per page"


@pytest.mark.asyncio
async def test_failure_from_a_dark_box_still_gives_way_once_it_wakes(session, monkeypatch, fake_redis):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ConnectTimeout("dark", request=request)
        return httpx.Response(200, json={"data": [{"id": "m"}]})

    mock_httpx(monkeypatch, handler)
    use_fake_redis(monkeypatch, fake_redis)
    await add_runtime(session, slug="wake-model", endpoint="http://192.0.2.13:8000/v1")
    await mark_live(fake_redis, "wake-model", reachable=False, consecutive_failures=1)
    first = entry_for(await model_catalog.build_catalog(session), "wake-model")
    assert first["status"] == model_catalog.STATUS_UNREACHABLE

    await mark_live(fake_redis, "wake-model", reachable=True, consecutive_failures=0)
    second = entry_for(await model_catalog.build_catalog(session), "wake-model")

    assert second["status"] == model_catalog.STATUS_OK


# ── The metrics endpoint feeds the memory ────────────────────────────────────


@pytest.mark.asyncio
async def test_host_metrics_remembers_the_answer(auth_client, monkeypatch, fake_redis):
    use_fake_redis(monkeypatch, fake_redis)
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={
                "slug": "wol-mem",
                "display_name": "Wake box",
                "kind": "flask_wol",
                "control_url": "http://192.0.2.20:5555",
                "power_managed": True,
            },
        )
    ).json()

    with patch(
        "app.services.runtime_manager._control_server_reachable",
        new=AsyncMock(return_value=True),
    ):
        await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")

    assert await host_liveness.recall(created["id"]) is True


# ── PR #971 review (NICE): one definition of "awake", no needless host loads ──


@pytest.mark.asyncio
async def test_page_probe_is_the_watchers_check_with_the_page_limit():
    """host_liveness asks the same /health check the runtime watcher uses —
    only with the shorter page-load limit, not a copy of it."""
    from app.services import runtime_manager

    check = AsyncMock(return_value=True)
    with patch.object(runtime_manager, "_control_server_reachable", new=check):
        assert await host_liveness._probe_control_server("http://192.0.2.77:5555") is True
    check.assert_awaited_once_with("http://192.0.2.77:5555", timeout=host_liveness.PROBE_TIMEOUT_S)


@pytest.mark.asyncio
async def test_cached_success_does_not_load_the_boxes(session, monkeypatch, fake_redis):
    """A remembered success wins for a dark port — its boxes are not needed."""
    mock_httpx(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"id": "m"}]}))
    use_fake_redis(monkeypatch, fake_redis)
    control_server(monkeypatch, awake=True)
    host = await sleeper_box(session)
    await add_runtime(session, host)
    await mark_live(fake_redis, "box-model", reachable=True, consecutive_failures=0)
    first = entry_for(await model_catalog.build_catalog(session), "box-model")
    assert first["status"] == model_catalog.STATUS_OK

    await mark_live(fake_redis, "box-model", reachable=False, consecutive_failures=12)
    hosts_of = AsyncMock(side_effect=model_catalog._hosts_of)
    monkeypatch.setattr(model_catalog, "_hosts_of", hosts_of)

    entry = entry_for(await model_catalog.build_catalog(session), "box-model")

    assert entry["status"] == model_catalog.STATUS_OK
    assert entry["cached"] is True
    hosts_of.assert_not_awaited()
