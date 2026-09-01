"""Geräte-Steuerung — Soll-Zustand, Ist-Meldung und Ampel (Vertrag 01.09.2026).

Vorbild für Aufbau und Fixtures: tests/test_nodes_api.py (Pairing → Token →
Heartbeat). Nur RFC-5737-Platzhalter und Fantasienamen, wie dort — das Repo
ist öffentlich.

Die Tests sind so gebaut, dass sie fehlschlagen KÖNNEN: der
Rückwärtskompatibilitäts-Test vergleicht die Heartbeat-Antwort exakt (ein
zusätzliches `desired_state: null` würde ihn rot machen), und die
Ampel-Tests prüfen jede Farbe einzeln gegen einen Zustand, der nur diese
eine Farbe ergeben darf.
"""
import uuid
from datetime import timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.services import device_state as ds
from app.utils import utcnow
from tests.conftest import test_engine


def _telemetry(**overrides) -> dict:
    body = {"ts": utcnow().isoformat(), "cpu_pct": 9.0, "gpu_temp_c": 61}
    body.update(overrides)
    return body


def _device_state(**overrides) -> dict:
    body = {
        "gpu_mode": "eco",
        "gpu_clock_mhz": 1989,
        "gpu_power_w": 32.5,
        "gpu_temp_c": 74,
        "min_free_kbytes": 5242880,
        "oom_guard": "active",
        "latency_tune": True,
        "mtu": {"iface": "enP7s7", "value": 9000},
        "applied_at": utcnow().isoformat(),
    }
    body.update(overrides)
    return body


_FULL_DESIRED = {
    "gpu_mode": "eco",
    "min_free_kbytes": 5242880,
    "oom_guard": True,
    "latency_tune": True,
    "mtu": 9000,
}


async def _pair(client, auth_client, hostname: str = "gx10") -> tuple[str, str]:
    """Pairing-Code → node_token (identisch zu test_nodes_api._paired_token)."""
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    paired = (
        await client.post(
            "/api/v1/nodes/pair",
            json={"code": minted["code"], "hostname": hostname, "agent_version": "0.2.0"},
        )
    ).json()
    return paired["node_token"], paired["host_id"]


async def _heartbeat(client, token: str, **extra):
    return await client.post(
        "/api/v1/nodes/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={"telemetry": _telemetry(), **extra},
    )


# ── Rückwärtskompatibilität: alter Agent ohne device_state ───────────────────


@pytest.mark.asyncio
async def test_heartbeat_without_device_state_answer_unchanged(client, auth_client):
    """Ein alter Agent schickt kein device_state und darf KEIN neues Feld in
    der Antwort sehen — sonst bricht sein strikter Antwort-Parser."""
    token, host_id = await _pair(client, auth_client)

    resp = await _heartbeat(client, token)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "heartbeat_interval_s": 15, "commands": []}

    state = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert state["device_state"] is None


@pytest.mark.asyncio
async def test_heartbeat_without_device_state_keeps_previous(client, auth_client):
    """Ein Heartbeat ohne device_state darf den zuletzt gemeldeten Ist nicht
    löschen — dieselbe Regel wie beim Inventar."""
    token, host_id = await _pair(client, auth_client)
    assert (await _heartbeat(client, token, device_state=_device_state())).status_code == 200

    await _age_last_seen(host_id)
    assert (await _heartbeat(client, token)).status_code == 200

    state = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert state["device_state"]["gpu_mode"] == "eco"


async def _age_last_seen(host_id: str, seconds: int = 30) -> None:
    """Rate-Guard (5 s) umgehen, ohne zu schlafen — dasselbe Mittel wie
    test_nodes_api für den Stale-Fall: direkt an der Zeile drehen."""
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        host = await s.get(Host, uuid.UUID(host_id))
        host.agent_last_seen_at = utcnow() - timedelta(seconds=seconds)
        s.add(host)
        await s.commit()


# ── Ist-Zustand entgegennehmen ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_with_device_state_is_stored(client, auth_client):
    token, host_id = await _pair(client, auth_client)

    resp = await _heartbeat(client, token, device_state=_device_state())
    assert resp.status_code == 200, resp.text

    state = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert state["device_state"]["gpu_mode"] == "eco"
    assert state["device_state"]["mtu"] == {"iface": "enP7s7", "value": 9000}
    assert state["device_state_updated_at"] is not None
    # last_error war null → exclude_none lässt es weg, statt es als hartes
    # null zu speichern (sonst würde die Ampel es als Fehler lesen können).
    assert "last_error" not in state["device_state"]


# ── Soll setzen → landet in der nächsten Heartbeat-Antwort ───────────────────


@pytest.mark.asyncio
async def test_desired_state_appears_in_next_heartbeat(client, auth_client):
    token, host_id = await _pair(client, auth_client)

    put = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"gpu_mode": "eco+", "mtu": 9000})
    assert put.status_code == 200, put.text
    assert put.json()["desired_state"] == {"gpu_mode": "eco+", "mtu": 9000}

    resp = await _heartbeat(client, token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["desired_state"] == {"gpu_mode": "eco+", "mtu": 9000}


@pytest.mark.asyncio
async def test_empty_body_clears_desired_state(client, auth_client):
    token, host_id = await _pair(client, auth_client)
    await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"gpu_mode": "eco"})

    cleared = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["desired_state"] is None

    # Kein Soll → das Feld fällt wieder ganz aus der Antwort.
    assert "desired_state" not in (await _heartbeat(client, token)).json()


@pytest.mark.asyncio
async def test_desired_state_slug_lookup(auth_client, client):
    _token, host_id = await _pair(client, auth_client, hostname="beta-box")
    resp = await auth_client.put("/api/v1/nodes/beta-box/device-state", json={"gpu_mode": "normal"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["host_id"] == host_id


@pytest.mark.asyncio
async def test_desired_state_unknown_host_404(auth_client):
    resp = await auth_client.put("/api/v1/nodes/gibt-es-nicht/device-state", json={"gpu_mode": "eco"})
    assert resp.status_code == 404


# ── Strenge Validierung — fail-closed ────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"gpu_mode": "turbo"},          # nicht in der festen Liste
        {"gpu_mode": "unknown"},        # im IST erlaubt, als BEFEHL nie
        {"gpu_mode": "eco; reboot"},    # Shell-Anhängsel
        {"min_free_kbytes": 999_999_999},  # weit über der Plausibilitätsgrenze
        {"min_free_kbytes": 1},
        {"mtu": 70},
        {"mtu": 100_000},
        {"nvidia_smi_args": "-i 0"},    # unbekanntes Feld → nie durchreichen
    ],
)
@pytest.mark.asyncio
async def test_invalid_desired_state_422(client, auth_client, body):
    _token, host_id = await _pair(client, auth_client)
    resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json=body)
    assert resp.status_code == 422, resp.text

    # Sabotage-Probe: nichts davon darf gespeichert worden sein.
    state = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert state["desired_state"] is None


@pytest.mark.asyncio
async def test_all_valid_gpu_modes_accepted(client, auth_client):
    _token, host_id = await _pair(client, auth_client)
    for mode in ds.GPU_MODES:
        resp = await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"gpu_mode": mode})
        assert resp.status_code == 200, f"{mode}: {resp.text}"
        assert resp.json()["desired_state"]["gpu_mode"] == mode


@pytest.mark.asyncio
async def test_set_desired_state_forbidden_for_viewer(client):
    """Fernsteuerung ist root-nah — nur Admin, wie jeder Host-Schreibzugriff."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer", role="viewer", is_active=True))
        await s.commit()
    token = create_access_token(str(uid), "viewer")

    resp = await client.put(
        f"/api/v1/nodes/{uuid.uuid4()}/device-state",
        headers={"Authorization": f"Bearer {token}"},
        json={"gpu_mode": "eco"},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_set_desired_state_unauthenticated_401(client):
    resp = await client.put(f"/api/v1/nodes/{uuid.uuid4()}/device-state", json={"gpu_mode": "eco"})
    assert resp.status_code in (401, 403)


# ── Ampel — die vier Fälle, reine Funktion ───────────────────────────────────


def test_ampel_gruen_ist_deckt_soll_und_frisch():
    result = ds.compute_status(
        is_agent_host=True,
        desired=_FULL_DESIRED,
        current=_device_state(),
        reported_at=utcnow(),
    )
    assert result["status"] == ds.STATUS_GREEN
    assert result["diff"] == []


def test_ampel_gelb_wenn_ist_vom_soll_abweicht():
    result = ds.compute_status(
        is_agent_host=True,
        desired=_FULL_DESIRED,
        current=_device_state(gpu_mode="boost"),
        reported_at=utcnow(),
    )
    assert result["status"] == ds.STATUS_YELLOW
    assert result["diff"] == ["gpu_mode"]


def test_ampel_gelb_wenn_meldung_veraltet():
    result = ds.compute_status(
        is_agent_host=True,
        desired=_FULL_DESIRED,
        current=_device_state(),
        reported_at=utcnow() - timedelta(seconds=ds.STALE_AFTER_S + 1),
    )
    assert result["status"] == ds.STATUS_YELLOW
    assert result["reason"] == "stale"


def test_ampel_rot_bei_last_error():
    result = ds.compute_status(
        is_agent_host=True,
        desired=_FULL_DESIRED,
        current=_device_state(last_error="nvidia-smi -lgc failed: permission denied"),
        reported_at=utcnow(),
    )
    assert result["status"] == ds.STATUS_RED
    assert "permission denied" in result["last_error"]


def test_ampel_rot_wenn_nie_gehaertet():
    result = ds.compute_status(
        is_agent_host=True, desired=_FULL_DESIRED, current=None, reported_at=None
    )
    assert result["status"] == ds.STATUS_RED
    assert result["reason"] == "no_device_state"


def test_ampel_grau_ohne_node_agent():
    result = ds.compute_status(
        is_agent_host=False, desired=None, current=None, reported_at=None
    )
    assert result["status"] == ds.STATUS_GREY
    assert result["reason"] == "no_agent"


# ── Soll/Ist-Vergleich im Detail ─────────────────────────────────────────────


def test_diff_ignoriert_felder_ohne_meinung():
    """Kein Feld = keine Meinung: ein Ist im Boost-Modus ist grün, solange
    niemand einen GPU-Modus vorgegeben hat."""
    assert ds.desired_state_diff({"mtu": 9000}, _device_state(gpu_mode="boost")) == []


def test_diff_oom_guard_aus_akzeptiert_inactive_und_missing():
    for ist in ("inactive", "missing"):
        assert ds.desired_state_diff({"oom_guard": False}, {"oom_guard": ist}) == []
    assert ds.desired_state_diff({"oom_guard": False}, {"oom_guard": "active"}) == ["oom_guard"]
    assert ds.desired_state_diff({"oom_guard": True}, {"oom_guard": "missing"}) == ["oom_guard"]


def test_diff_mtu_vergleicht_gegen_den_wert_nicht_das_dict():
    assert ds.desired_state_diff({"mtu": 9000}, {"mtu": {"iface": "eth0", "value": 9000}}) == []
    assert ds.desired_state_diff({"mtu": 9000}, {"mtu": {"iface": "eth0", "value": 1500}}) == ["mtu"]


def test_diff_fehlendes_ist_feld_zaehlt_als_abweichung():
    assert ds.desired_state_diff({"gpu_mode": "eco"}, {}) == ["gpu_mode"]


# ── Ampel über den Endpunkt (Ende-zu-Ende) ───────────────────────────────────


@pytest.mark.asyncio
async def test_device_state_endpoint_status_flips_red_to_green(client, auth_client):
    token, host_id = await _pair(client, auth_client)

    # Frisch gepaart, nie gehärtet → rot.
    first = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert first["status"] == ds.STATUS_RED and first["reason"] == "no_device_state"

    await auth_client.put(f"/api/v1/nodes/{host_id}/device-state", json={"gpu_mode": "eco"})

    # Soll gesetzt, Ist meldet noch boost → gelb.
    await _heartbeat(client, token, device_state=_device_state(gpu_mode="boost"))
    yellow = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert yellow["status"] == ds.STATUS_YELLOW and yellow["diff"] == ["gpu_mode"]

    # Agent hat nachgezogen → grün.
    await _age_last_seen(host_id)
    await _heartbeat(client, token, device_state=_device_state(gpu_mode="eco"))
    green = (await auth_client.get(f"/api/v1/nodes/{host_id}/device-state")).json()
    assert green["status"] == ds.STATUS_GREEN, green


@pytest.mark.asyncio
async def test_device_state_endpoint_grey_for_host_without_agent(auth_client, async_session):
    """Ein per SSH angelegter Host ohne Pairing hat nichts zu steuern."""
    host = Host(slug="ssh-only-box", display_name="SSH Only", kind="ssh")
    async_session.add(host)
    await async_session.commit()

    resp = await auth_client.get(f"/api/v1/nodes/{host.id}/device-state")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == ds.STATUS_GREY and data["has_agent"] is False


# ── Listen-Endpunkt für die Geräte-Seite ─────────────────────────────────────


@pytest.mark.asyncio
async def test_devices_list_returns_paired_hosts_with_ampel(client, auth_client, async_session):
    token, host_id = await _pair(client, auth_client, hostname="gx10-liste")
    await _heartbeat(client, token, device_state=_device_state())

    # Ein nie gepaarter SSH-Host darf NICHT in der Liste auftauchen — sonst
    # sieht ein Cloud-Setup ohne GPU-Box eine Reihe grauer Kacheln.
    async_session.add(Host(slug="ssh-nicht-in-liste", display_name="SSH", kind="ssh"))
    await async_session.commit()

    resp = await auth_client.get("/api/v1/nodes/devices")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    slugs = [r["slug"] for r in rows]
    assert "ssh-nicht-in-liste" not in slugs
    row = next(r for r in rows if r["host_id"] == host_id)
    assert row["has_agent"] is True
    assert row["device_state"]["gpu_mode"] == "eco"
    assert row["status"] in (ds.STATUS_GREEN, ds.STATUS_YELLOW, ds.STATUS_RED)


@pytest.mark.asyncio
async def test_devices_route_not_shadowed_by_host_id(auth_client):
    """/devices muss vor /{host_id}/device-state stehen — sonst läge hier
    ein 404 'Host devices nicht gefunden'."""
    resp = await auth_client.get("/api/v1/nodes/devices")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_device_state_carries_display_name(client, auth_client):
    """Die Oberfläche soll die Kachel-Überschrift nicht gegen eine zweite
    Quelle verknüpfen müssen — sonst laufen Liste und Kachel auseinander."""
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={"display_name_hint": "GX10 Werkstatt"})).json()
    paired = (
        await client.post("/api/v1/nodes/pair", json={"code": minted["code"], "hostname": "gx10-anzeige"})
    ).json()

    row = (await auth_client.get(f"/api/v1/nodes/{paired['host_id']}/device-state")).json()
    assert row["display_name"] == "GX10 Werkstatt"
    assert row["slug"] == "gx10-anzeige"
