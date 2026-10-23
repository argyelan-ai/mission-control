"""Wake a sleeping box from its HOST flag (round-5 re-check, L1).

PORSCHE slept, its host row said ``power_managed=true`` — but none of its
active runtime rows carried the flag, and waking existed only per runtime
(``POST /runtimes/{id}/wake``). The page had nothing to wake it with and
offered "Start model" on a box that does not answer.

These tests pin the host-level wake (``POST /hosts/{id}/wake``): it reuses the
one Wake-on-LAN path (``runtime_manager``'s trigger file for the host-side
watcher), it is only for power-managed boxes, and it drops the remembered
"asleep" so the next metrics poll asks the box again. They also pin the short
memory of the box's own check: a sleeping box costs the page one slow check,
not one every poll.

No real box is woken: the trigger file lands in the test home
(conftest ``wake_request_dir``) or a tmp dir, the control server is mocked.
Only RFC 5737 placeholder IPs + documentation MACs — public repo.
"""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services import host_liveness, runtime_manager
from app.services.host_resolver import ResolvedHost

_MAC = "00:00:5E:00:53:01"


def _wol_host_body(slug: str = "wake-host", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "Wake Host",
        "kind": "flask_wol",
        "control_url": "http://192.0.2.20:5555",
        "wol_mac_address": _MAC,
        "power_managed": True,
    }
    body.update(overrides)
    return body


async def _viewer_token() -> str:
    from app.auth import create_access_token
    from app.models.user import User
    from sqlmodel.ext.asyncio.session import AsyncSession
    from tests.conftest import test_engine

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer",
                   role="viewer", is_active=True))
        await s.commit()
    return create_access_token(str(uid), "viewer")


def _watcher_alive(wake_dir, age_s: float = 5):
    """The host-side watcher's heartbeat — written on every run (round-7 L5).

    Without it the backend knows nobody reads the folder: no wake is queued.
    """
    import os
    import time

    wake_dir.mkdir(parents=True, exist_ok=True)
    beat = wake_dir / runtime_manager.WAKE_HELPER_HEARTBEAT_NAME
    beat.write_text("{}", encoding="utf-8")
    at = time.time() - age_s
    os.utime(beat, (at, at))
    return beat


# ── service: one Wake-on-LAN path, now also from a host ─────────────────────


@pytest.mark.asyncio
async def test_wake_host_writes_the_same_trigger_file(tmp_path):
    host = ResolvedHost(
        slug="wake-host", display_name="Wake Host", kind="flask_wol",
        control_url="http://192.0.2.20:5555", wol_mac_address=_MAC, power_managed=True,
        source="registry",
    )
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(host)
    assert result["ok"] is True, result
    payload = json.loads((tmp_path / "wake-host.request.json").read_text())
    assert payload["mac"] == _MAC
    assert payload["slug"] == "wake-host"
    # No ssh_host on a flask_wol box: the control server's address is the box.
    assert payload["ip"] == "192.0.2.20"
    assert "requested_at" in payload


@pytest.mark.asyncio
async def test_wake_host_refuses_a_box_that_is_not_power_managed(tmp_path):
    host = ResolvedHost(slug="always-on", kind="ssh", ssh_host="192.0.2.30", wol_mac_address=_MAC)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(host)
    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_wake_host_needs_a_mac(tmp_path):
    host = ResolvedHost(slug="no-mac", kind="flask_wol", power_managed=True)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path), \
         patch.object(runtime_manager.settings, "wol_mac", ""):
        result = await runtime_manager.wake_host(host)
    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []


# ── route: POST /hosts/{id}/wake ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_wakes_a_power_managed_host_without_a_flagged_runtime(auth_client, tmp_path):
    """The PORSCHE case: the host carries the flag, no runtime row does."""
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body())).json()
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post(f"/api/v1/hosts/{created['id']}/wake")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert (tmp_path / "wake-host.request.json").exists()


@pytest.mark.asyncio
async def test_route_by_slug_too(auth_client, tmp_path):
    await auth_client.post("/api/v1/hosts", json=_wol_host_body("by-slug"))
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post("/api/v1/hosts/by-slug/wake")
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_route_refuses_a_box_that_is_not_power_managed(auth_client, tmp_path):
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={"slug": "spark", "display_name": "Spark", "kind": "ssh", "ssh_host": "192.0.2.10"},
        )
    ).json()
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post(f"/api/v1/hosts/{created['id']}/wake")
    assert resp.status_code == 400
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_route_unknown_host_404(auth_client):
    resp = await auth_client.post(f"/api/v1/hosts/{uuid.uuid4()}/wake")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_viewer_may_not_wake(auth_client, tmp_path):
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body("viewer-box"))).json()
    token = await _viewer_token()
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post(
            f"/api/v1/hosts/{created['id']}/wake",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert list(tmp_path.iterdir()) == []


# ── the box's own check, remembered briefly ─────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_reuses_a_fresh_asleep_answer(auth_client):
    """A sleeping box answers the 5 s poll from memory, not with a 3 s check."""
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body("sleepy"))).json()
    probe = AsyncMock(return_value=False)
    with patch("app.services.runtime_manager._control_server_reachable", new=probe):
        first = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")).json()
        second = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")).json()
    assert first["status"] == "asleep"
    assert second == first
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_metrics_asks_an_awake_box_every_time(auth_client):
    """Only "asleep" is remembered: an awake box answers fast and may fall asleep."""
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body("awake"))).json()
    probe = AsyncMock(return_value=True)
    with patch("app.services.runtime_manager._control_server_reachable", new=probe):
        await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
        await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert probe.await_count == 2


@pytest.mark.asyncio
async def test_wake_forgets_the_remembered_asleep(auth_client, tmp_path):
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body("wakes"))).json()
    host_id = created["id"]
    with patch("app.services.runtime_manager._control_server_reachable", new=AsyncMock(return_value=False)):
        await auth_client.get(f"/api/v1/hosts/{host_id}/metrics")
    assert await host_liveness.recall(host_id) is False

    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        assert (await auth_client.post(f"/api/v1/hosts/{host_id}/wake")).status_code == 200
    assert await host_liveness.recall(host_id) is None

    probe = AsyncMock(return_value=True)
    with patch("app.services.runtime_manager._control_server_reachable", new=probe):
        after = (await auth_client.get(f"/api/v1/hosts/{host_id}/metrics")).json()
    assert probe.await_count == 1
    assert after["status"] == "awake"


# ── the wake helper on the Mac is not running (PR #976 review, MUST 1) ──────


def _leave_a_stale_request(wake_dir, name: str = "other-box", age_s: int = 600):
    """A request the host-side watcher never read — what a missing helper leaves."""
    import os
    import time

    wake_dir.mkdir(parents=True, exist_ok=True)
    stale = wake_dir / f"{name}.request.json"
    stale.write_text(json.dumps({"slug": name, "mac": _MAC}), encoding="utf-8")
    old = time.time() - age_s
    os.utime(stale, (old, old))
    return stale


@pytest.mark.asyncio
async def test_wake_host_says_so_when_the_helper_left_a_request_waiting(tmp_path):
    """No "Waking…" for a box nobody wakes: an unread old request means no helper.

    Second signal next to the heartbeat: the heartbeat is fresh here, yet a
    request nobody retired is still waiting — the helper does not work.
    """
    stale = _leave_a_stale_request(tmp_path)
    _watcher_alive(tmp_path)
    host = ResolvedHost(
        slug="wake-host", display_name="Wake Host", kind="flask_wol",
        control_url="http://192.0.2.20:5555", wol_mac_address=_MAC, power_managed=True,
        source="registry",
    )
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(host)
    assert result["ok"] is False
    assert result["helper_down"] is True
    assert result["reason"] == "request_waiting"
    assert "läuft nicht" in result["message"]
    # Nothing new is queued, and the old request is left for the helper.
    assert not (tmp_path / "wake-host.request.json").exists()
    assert stale.exists()


@pytest.mark.asyncio
async def test_wake_runtime_says_so_too(tmp_path):
    """The per-runtime wake shares the path — same honest answer."""
    _leave_a_stale_request(tmp_path)
    _watcher_alive(tmp_path)
    runtime = {"slug": "wake-rt", "display_name": "Wake RT", "power_managed": True,
               "wol_mac_address": _MAC, "host": "192.0.2.20"}
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_runtime(runtime)
    assert result["ok"] is False and result["helper_down"] is True
    assert not (tmp_path / "wake-rt.request.json").exists()


@pytest.mark.asyncio
async def test_a_fresh_waiting_request_is_not_a_missing_helper(tmp_path):
    """The watcher runs at least every 30 s — a request a minute old is on its way."""
    _leave_a_stale_request(tmp_path, age_s=60)
    _watcher_alive(tmp_path)
    host = ResolvedHost(
        slug="wake-host", display_name="Wake Host", kind="flask_wol",
        control_url="http://192.0.2.20:5555", wol_mac_address=_MAC, power_managed=True,
        source="registry",
    )
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(host)
    assert result["ok"] is True, result
    assert (tmp_path / "wake-host.request.json").exists()


@pytest.mark.asyncio
async def test_a_second_wake_keeps_the_first_request_and_its_age(tmp_path):
    """Rewriting a waiting request would reset its age and hide a dead helper."""
    host = ResolvedHost(
        slug="wake-host", display_name="Wake Host", kind="flask_wol",
        control_url="http://192.0.2.20:5555", wol_mac_address=_MAC, power_managed=True,
        source="registry",
    )
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        assert (await runtime_manager.wake_host(host))["ok"] is True
        first = (tmp_path / "wake-host.request.json").read_text()
        import os
        import time
        old = time.time() - 30
        os.utime(tmp_path / "wake-host.request.json", (old, old))
        assert (await runtime_manager.wake_host(host))["ok"] is True
    req = tmp_path / "wake-host.request.json"
    assert req.read_text() == first
    assert req.stat().st_mtime == pytest.approx(old, abs=1)


@pytest.mark.asyncio
async def test_route_answers_503_when_the_helper_is_down(auth_client, tmp_path):
    """503, not 400: the request is fine, the host-side helper is missing."""
    created = (await auth_client.post("/api/v1/hosts", json=_wol_host_body("helperless"))).json()
    _leave_a_stale_request(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post(f"/api/v1/hosts/{created['id']}/wake")
    assert resp.status_code == 503, resp.text
    assert "läuft nicht" in resp.json()["detail"]
    # Nothing was queued for a helper that is not there.
    assert not (tmp_path / "helperless.request.json").exists()


# ── a real heartbeat instead of a leftover file (round-7 re-check, L5) ──────
#
# Live on the Mac the helper was not installed; the honest 503 came only from
# a request left over since August. An EMPTY folder said "Waking…" for two
# minutes although nobody read it. The watcher now writes ``watcher.alive`` on
# every run; no heartbeat (or an old one) means no helper, whatever the folder
# holds.


def _wol_host() -> ResolvedHost:
    return ResolvedHost(
        slug="wake-host", display_name="Wake Host", kind="flask_wol",
        control_url="http://192.0.2.20:5555", wol_mac_address=_MAC, power_managed=True,
        source="registry",
    )


@pytest.mark.asyncio
async def test_an_empty_folder_without_heartbeat_is_a_missing_helper(tmp_path):
    """The r1 S1 case: nothing waiting, no helper — still no "Waking…"."""
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(_wol_host())
    assert result["ok"] is False
    assert result["helper_down"] is True
    assert result["reason"] == "no_heartbeat"
    assert "Weck-Helfer" in result["message"] and "Wake Host" in result["message"]
    # Nothing queued for a helper that is not there.
    assert not (tmp_path / "wake-host.request.json").exists()


@pytest.mark.asyncio
async def test_a_missing_folder_is_a_missing_helper_too(tmp_path):
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path / "absent"):
        result = await runtime_manager.wake_host(_wol_host())
    assert result["ok"] is False and result["helper_down"] is True
    assert not (tmp_path / "absent").exists()


@pytest.mark.asyncio
async def test_an_old_heartbeat_is_a_missing_helper(tmp_path):
    """Three missed 30 s runs: the helper stopped."""
    _watcher_alive(tmp_path, age_s=runtime_manager.WAKE_HELPER_HEARTBEAT_S + 30)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(_wol_host())
    assert result["ok"] is False and result["helper_down"] is True
    assert result["reason"] == "heartbeat_stale"
    assert "2 min" in result["message"]
    assert not (tmp_path / "wake-host.request.json").exists()


@pytest.mark.asyncio
async def test_a_fresh_heartbeat_queues_the_wake(tmp_path):
    _watcher_alive(tmp_path, age_s=runtime_manager.WAKE_HELPER_HEARTBEAT_S - 30)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        result = await runtime_manager.wake_host(_wol_host())
    assert result["ok"] is True, result
    assert "helper_down" not in result
    assert (tmp_path / "wake-host.request.json").exists()


@pytest.mark.asyncio
async def test_the_heartbeat_is_not_a_request(tmp_path):
    """watcher.alive must never be read as a waiting request (glob *.request.json)."""
    _watcher_alive(tmp_path, age_s=60)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        assert (await runtime_manager.wake_host(_wol_host()))["ok"] is True


# ── route: POST /runtimes/{id}/wake, aligned with the host route ────────────


async def _wol_runtime(async_session, slug: str = "wake-rt", with_host: bool = True):
    from app.models.host import Host
    from app.models.runtime import Runtime

    host = None
    if with_host:
        host = Host(slug=f"{slug}-box", display_name="Wake RT Box", kind="flask_wol",
                    control_url="http://192.0.2.20:5555", wol_mac_address=_MAC,
                    power_managed=True)
        async_session.add(host)
        await async_session.flush()
    rt = Runtime(
        slug=slug, display_name="Wake RT", runtime_type="flask_wol",
        endpoint="http://192.0.2.20:8000/v1", control_url="http://192.0.2.20:5555",
        wol_mac_address=_MAC, power_managed=True, enabled=True, ui_order=1,
        host_id=host.id if host else None,
    )
    async_session.add(rt)
    await async_session.commit()
    return rt, host


@pytest.mark.asyncio
async def test_runtime_route_answers_503_without_heartbeat(async_session, auth_client, tmp_path):
    await _wol_runtime(async_session)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post("/api/v1/runtimes/wake-rt/wake")
    assert resp.status_code == 503, resp.text
    assert "Weck-Helfer" in resp.json()["detail"]
    assert not (tmp_path / "wake-rt.request.json").exists()


@pytest.mark.asyncio
async def test_runtime_route_wakes_with_heartbeat(async_session, auth_client, tmp_path):
    await _wol_runtime(async_session)
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post("/api/v1/runtimes/wake-rt/wake")
    assert resp.status_code == 200, resp.text
    assert (tmp_path / "wake-rt.request.json").exists()


@pytest.mark.asyncio
async def test_runtime_route_viewer_may_not_wake(async_session, auth_client, tmp_path):
    """Same rule as the host route: a viewer only looks."""
    await _wol_runtime(async_session)
    _watcher_alive(tmp_path)
    token = await _viewer_token()
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        resp = await auth_client.post(
            "/api/v1/runtimes/wake-rt/wake", headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 403
    assert not (tmp_path / "wake-rt.request.json").exists()


@pytest.mark.asyncio
async def test_runtime_route_forgets_the_remembered_asleep(async_session, auth_client, tmp_path):
    """Waking from the runtime detail drops the box's "asleep" like the host route."""
    _, host = await _wol_runtime(async_session)
    await host_liveness.remember(host.id, False)
    assert await host_liveness.recall(host.id) is False
    _watcher_alive(tmp_path)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        assert (await auth_client.post("/api/v1/runtimes/wake-rt/wake")).status_code == 200
    assert await host_liveness.recall(host.id) is None


@pytest.mark.asyncio
async def test_runtime_route_keeps_a_remembered_asleep_when_nothing_was_woken(
    async_session, auth_client, tmp_path,
):
    _, host = await _wol_runtime(async_session)
    await host_liveness.remember(host.id, False)
    with patch.object(runtime_manager.settings, "wake_request_dir", tmp_path):
        assert (await auth_client.post("/api/v1/runtimes/wake-rt/wake")).status_code == 503
    assert await host_liveness.recall(host.id) is False
