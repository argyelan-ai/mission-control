"""Host Registry API (ADR-048, B3) — CRUD, Delete-Guard, Metrics, Spark-Alias.

Nur RFC-5737-Platzhalter-IPs (192.0.2.x) + Dummy-MAC — public Repo,
keine echten Adressen in Fixtures.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.runtime import Runtime
from tests.conftest import test_engine

# nvidia-smi + free -m Antwort im _SPARK_METRICS_CMD-Format
_SSH_METRICS_STDOUT = (
    "35, 8806, 131072, 61\n"
    "---\n"
    "              total        used        free\n"
    "Mem:          119181       15230       90000\n"
    "Swap:              0           0           0"
)


async def _viewer_token() -> str:
    """JWT für einen viewer-User (Pattern aus test_runtime_readiness_gate)."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer",
                   role="viewer", is_active=True))
        await s.commit()
    return create_access_token(str(uid), "viewer")


def _ssh_host_body(slug: str = "gpu-box", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "GPU Box",
        "kind": "ssh",
        "ssh_host": "192.0.2.10",
        "ssh_user": "mcuser",
        "ssh_key_path": "/home/mcuser/.ssh/id_rsa",
        "notes": "Testbox",
        "ui_order": 1,
    }
    body.update(overrides)
    return body


async def _stub_state(*_args, **_kwargs):
    """get_runtime_state-Ersatz — kein SSH in Tests."""
    return {"state": "ready", "http_reachable": True, "container_status": None}


# ── CRUD ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_list_hosts_sorted(auth_client):
    """POST legt Host an; GET liefert bare Liste, nach ui_order sortiert."""
    r1 = await auth_client.post("/api/v1/hosts", json=_ssh_host_body("b-box", ui_order=2))
    r2 = await auth_client.post(
        "/api/v1/hosts",
        json={"slug": "a-box", "display_name": "A", "kind": "local", "ui_order": 1},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    created = r1.json()
    assert created["slug"] == "b-box"
    assert created["ssh_host"] == "192.0.2.10"
    assert created["ssh_key_path"] == "/home/mcuser/.ssh/id_rsa"  # Pfad ok, kein Key-Inhalt

    resp = await auth_client.get("/api/v1/hosts")
    assert resp.status_code == 200
    hosts = resp.json()
    assert [h["slug"] for h in hosts] == ["a-box", "b-box"]


@pytest.mark.asyncio
async def test_create_duplicate_slug_409(auth_client):
    assert (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).status_code == 200
    resp = await auth_client.post("/api/v1/hosts", json=_ssh_host_body())
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_invalid_kind_and_control_url_422(auth_client):
    bad_kind = await auth_client.post(
        "/api/v1/hosts", json=_ssh_host_body(kind="teleport")
    )
    assert bad_kind.status_code == 422
    bad_url = await auth_client.post(
        "/api/v1/hosts",
        json=_ssh_host_body(slug="porsche-bad", kind="flask_wol",
                            control_url="ftp://192.0.2.20:5555"),
    )
    assert bad_url.status_code == 422


@pytest.mark.asyncio
async def test_patch_host_updates_and_clears_nullable(auth_client):
    """PATCH ändert Felder; explizites null räumt nullable Felder (exclude_unset)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    resp = await auth_client.patch(
        f"/api/v1/hosts/{created['id']}",
        json={"display_name": "Renamed", "enabled": False, "notes": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["display_name"] == "Renamed"
    assert data["enabled"] is False
    assert data["notes"] is None
    # ohne notes im Body bleibt notes unangetastet
    resp2 = await auth_client.patch(f"/api/v1/hosts/{created['id']}", json={"ui_order": 7})
    assert resp2.json()["notes"] is None
    assert resp2.json()["ui_order"] == 7


@pytest.mark.asyncio
async def test_patch_slug_conflict_409_and_lookup_by_slug(auth_client):
    await auth_client.post("/api/v1/hosts", json=_ssh_host_body("box-one"))
    await auth_client.post(
        "/api/v1/hosts", json={"slug": "box-two", "display_name": "Two", "kind": "local"}
    )
    # Slug-Lookup im Pfad + Umbenennung auf belegten Slug → 409
    resp = await auth_client.patch("/api/v1/hosts/box-two", json={"slug": "box-one"})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_patch_unknown_host_404(auth_client):
    resp = await auth_client.patch(f"/api/v1/hosts/{uuid.uuid4()}", json={"ui_order": 1})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_host_204(auth_client):
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    resp = await auth_client.delete(f"/api/v1/hosts/{created['id']}")
    assert resp.status_code == 204
    assert (await auth_client.get("/api/v1/hosts")).json() == []


@pytest.mark.asyncio
async def test_delete_host_with_bound_runtime_409(auth_client, async_session):
    """Delete-Guard: gebundene Runtimes blocken den Delete (erst umbinden)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    rt = Runtime(
        slug="bound-rt",
        display_name="Bound",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        host_id=uuid.UUID(created["id"]),
    )
    async_session.add(rt)
    await async_session.commit()

    resp = await auth_client.delete(f"/api/v1/hosts/{created['id']}")
    assert resp.status_code == 409
    assert "bound-rt" in resp.json()["detail"]

    # Umbinden → Delete geht durch
    rt.host_id = None
    async_session.add(rt)
    await async_session.commit()
    resp = await auth_client.delete(f"/api/v1/hosts/{created['id']}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_host_writes_forbidden_for_viewer(client, auth_client, fake_redis):
    """Writes sind admin-only — viewer bekommt 403 (Reads bleiben offen)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    token = await _viewer_token()
    headers = {"Authorization": f"Bearer {token}"}

    assert (await client.post("/api/v1/hosts", headers=headers,
                              json=_ssh_host_body("viewer-box"))).status_code == 403
    assert (await client.patch(f"/api/v1/hosts/{created['id']}", headers=headers,
                               json={"ui_order": 9})).status_code == 403
    assert (await client.delete(f"/api/v1/hosts/{created['id']}",
                                headers=headers)).status_code == 403
    assert (await client.get("/api/v1/hosts", headers=headers)).status_code == 200


# ── Metrics ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_host_metrics_ssh(auth_client):
    """kind=ssh → nvidia-smi/free-Parsing via get_host_metrics (SSH gemockt)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
    ) as ssh_mock:
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["kind"] == "ssh"
    assert data["reachable"] is True
    assert data["gpu_util_pct"] == 35
    assert data["vram_used_mb"] == 8806
    assert data["vram_total_mb"] == 131072
    assert data["gpu_temp_c"] == 61
    assert data["ram_total_mb"] == 119181
    assert data["ram_used_mb"] == 15230
    # SSH lief gegen DIESEN Host, nicht gegen den Settings-Fallback
    host_kwarg = ssh_mock.call_args.kwargs["host"]
    assert host_kwarg.ssh_host == "192.0.2.10"
    assert host_kwarg.slug == "gpu-box"


@pytest.mark.asyncio
async def test_host_metrics_ssh_unreachable(auth_client):
    """SSH-Fehler → reachable=false statt 500 (Muster get_host_metrics)."""
    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=OSError("connect failed")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reachable"] is False
    assert data["gpu_util_pct"] is None


@pytest.mark.asyncio
async def test_host_metrics_flask_wol_awake_and_asleep(auth_client):
    """kind=flask_wol → awake/health-Status statt GPU-Metrics."""
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={
                "slug": "porsche-test",
                "display_name": "PORSCHE",
                "kind": "flask_wol",
                "control_url": "http://192.0.2.20:5555",
                "wol_mac_address": "00:00:5E:00:53:01",
                "power_managed": True,
            },
        )
    ).json()

    with patch(
        "app.services.runtime_manager._porsche_reachable",
        new=AsyncMock(return_value=True),
    ):
        awake = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")).json()
    assert awake == {
        "kind": "flask_wol",
        "slug": "porsche-test",
        "reachable": True,
        "awake": True,
        "status": "awake",
    }

    with patch(
        "app.services.runtime_manager._porsche_reachable",
        new=AsyncMock(return_value=False),
    ):
        asleep = (await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")).json()
    assert asleep["awake"] is False
    assert asleep["status"] == "asleep"
    assert "gpu_util_pct" not in asleep


@pytest.mark.asyncio
async def test_host_metrics_local_empty(auth_client):
    """kind=local → leeres Objekt mit kind-Feld, kein SSH-Versuch."""
    created = (
        await auth_client.post(
            "/api/v1/hosts",
            json={"slug": "mc-host", "display_name": "MC", "kind": "local"},
        )
    ).json()
    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(side_effect=AssertionError("local darf kein SSH machen")),
    ):
        resp = await auth_client.get(f"/api/v1/hosts/{created['id']}/metrics")
    assert resp.status_code == 200
    assert resp.json() == {"kind": "local", "slug": "mc-host", "reachable": True}


@pytest.mark.asyncio
async def test_host_metrics_unknown_host_404(auth_client):
    resp = await auth_client.get(f"/api/v1/hosts/{uuid.uuid4()}/metrics")
    assert resp.status_code == 404


# ── Back-Compat-Alias GET /runtimes/spark/metrics ────────────────────────────


@pytest.mark.asyncio
async def test_spark_alias_404_without_dgx_spark_host(auth_client):
    """Kein Host 'dgx-spark' → 404 mit klarer Message (kein stummer Fallback)."""
    resp = await auth_client.get("/api/v1/runtimes/spark/metrics")
    assert resp.status_code == 404
    assert "dgx-spark" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_spark_alias_delegates_to_dgx_spark_host(auth_client, async_session):
    """Host 'dgx-spark' vorhanden → Alias liefert dessen Metrics."""
    async_session.add(Host(
        slug="dgx-spark",
        display_name="DGX Spark",
        kind="ssh",
        ssh_host="192.0.2.10",
        ssh_user="mcuser",
    ))
    await async_session.commit()

    with patch(
        "app.services.runtime_manager._ssh_run",
        new=AsyncMock(return_value=(_SSH_METRICS_STDOUT, "", 0)),
    ) as ssh_mock:
        resp = await auth_client.get("/api/v1/runtimes/spark/metrics")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reachable"] is True
    assert data["vram_total_mb"] == 131072
    assert ssh_mock.call_args.kwargs["host"].ssh_host == "192.0.2.10"


# ── GET /runtimes: host-Referenz im Payload ──────────────────────────────────


@pytest.mark.asyncio
async def test_runtimes_payload_contains_host_ref(auth_client, async_session):
    """Gebundene Runtime → host {id, slug, display_name}; ungebunden → null."""
    host = Host(slug="gpu-box", display_name="GPU Box", kind="ssh", ssh_host="192.0.2.10")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)

    async_session.add(Runtime(
        slug="bound-rt",
        display_name="Bound",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        host_id=host.id,
    ))
    async_session.add(Runtime(
        slug="unbound-rt",
        display_name="Unbound",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.11:8000/v1",
    ))
    await async_session.commit()

    with patch("app.services.runtime_manager.get_runtime_state", side_effect=_stub_state):
        resp = await auth_client.get("/api/v1/runtimes")
    assert resp.status_code == 200, resp.text
    by_slug = {r["slug"]: r for r in resp.json()["runtimes"]}
    assert by_slug["bound-rt"]["host"] == {
        "id": str(host.id),
        "slug": "gpu-box",
        "display_name": "GPU Box",
    }
    assert by_slug["unbound-rt"]["host"] is None


@pytest.mark.asyncio
async def test_single_runtime_payload_contains_host_ref(auth_client, async_session):
    """GET /runtimes/{slug} trägt dieselbe Host-Referenz wie die Liste."""
    host = Host(slug="gpu-box", display_name="GPU Box", kind="ssh", ssh_host="192.0.2.10")
    async_session.add(host)
    await async_session.commit()
    await async_session.refresh(host)
    async_session.add(Runtime(
        slug="bound-rt",
        display_name="Bound",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        host_id=host.id,
    ))
    await async_session.commit()

    with patch("app.services.runtime_manager.get_runtime_state", side_effect=_stub_state):
        resp = await auth_client.get("/api/v1/runtimes/bound-rt")
    assert resp.status_code == 200
    assert resp.json()["host"]["slug"] == "gpu-box"


# ── Input-Längen (Spiegel der String(N)-Spalten) ─────────────────────────────


@pytest.mark.asyncio
async def test_host_create_overlong_fields_422(auth_client):
    """Überlange Werte → 422 statt Postgres StringDataRightTruncation (500).

    SQLite (Tests) erzwingt keine Spaltenlängen — der Guard MUSS darum im
    Pydantic-Modell sitzen."""
    too_long_host = "h" * 129  # Spalte: String(128)
    resp = await auth_client.post(
        "/api/v1/hosts", json=_ssh_host_body(ssh_host=too_long_host)
    )
    assert resp.status_code == 422

    resp = await auth_client.post(
        "/api/v1/hosts",
        json=_ssh_host_body(slug="mac-box", wol_mac_address="0" * 33),  # String(32)
    )
    assert resp.status_code == 422

    created = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    resp = await auth_client.patch(
        f"/api/v1/hosts/{created['id']}", json={"ssh_user": "u" * 65}  # String(64)
    )
    assert resp.status_code == 422


# ── Runtime ↔ Host Binden/Umbinden via API (ADR-048) ─────────────────────────


def _runtime_body(slug: str = "api-rt", **overrides) -> dict:
    body = {
        "slug": slug,
        "display_name": "API Runtime",
        "runtime_type": "openai_compatible",
        "endpoint": "http://192.0.2.30:8000/v1",
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_runtime_create_with_host_id_binds(auth_client):
    """POST /runtimes/db mit host_id → Bindung + host-Ref in der Response."""
    host = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    resp = await auth_client.post(
        "/api/v1/runtimes/db", json=_runtime_body(host_id=host["id"])
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # CRUD-Response trägt die gleiche host-Shape wie GET /runtimes (HostRef|null),
    # nicht den deprecated Legacy-String.
    assert data["host"] == {
        "id": host["id"], "slug": "gpu-box", "display_name": "GPU Box",
    }


@pytest.mark.asyncio
async def test_runtime_create_with_unknown_host_id_422(auth_client):
    resp = await auth_client.post(
        "/api/v1/runtimes/db", json=_runtime_body(host_id=str(uuid.uuid4()))
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_runtime_patch_binds_and_unbinds_host(auth_client):
    """PATCH host_id bindet um; explizites host_id=null bindet los —
    danach geht der Host-Delete durch (der 409-Guard verlangte genau das)."""
    host = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    created = await auth_client.post("/api/v1/runtimes/db", json=_runtime_body())
    assert created.status_code == 200
    assert created.json()["host"] is None

    # Binden
    resp = await auth_client.patch(
        "/api/v1/runtimes/db/api-rt", json={"host_id": host["id"]}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["host"]["slug"] == "gpu-box"

    # Gebunden → Delete blockt
    assert (await auth_client.delete(f"/api/v1/hosts/{host['id']}")).status_code == 409

    # Unbind via explizitem null (exclude_none darf das nicht verschlucken)
    resp = await auth_client.patch(
        "/api/v1/runtimes/db/api-rt", json={"host_id": None}
    )
    assert resp.status_code == 200
    assert resp.json()["host"] is None

    # Jetzt geht der Delete durch — die 409-Anweisung ist per API erfüllbar
    assert (await auth_client.delete(f"/api/v1/hosts/{host['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_runtime_patch_unknown_host_id_422(auth_client):
    await auth_client.post("/api/v1/runtimes/db", json=_runtime_body())
    resp = await auth_client.patch(
        "/api/v1/runtimes/db/api-rt", json={"host_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_runtime_patch_without_host_id_keeps_binding(auth_client):
    """PATCH ohne host_id im Body lässt eine bestehende Bindung unangetastet."""
    host = (await auth_client.post("/api/v1/hosts", json=_ssh_host_body())).json()
    await auth_client.post("/api/v1/runtimes/db", json=_runtime_body(host_id=host["id"]))
    resp = await auth_client.patch(
        "/api/v1/runtimes/db/api-rt", json={"display_name": "Renamed"}
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["host"]["id"] == host["id"]
