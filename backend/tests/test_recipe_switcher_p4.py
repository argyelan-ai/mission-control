"""Rezept-Umschalter P4 (ADR-077) — „Vorflug": passt das Rezept überhaupt auf die Boxen?

Warum es diese Tests gibt
-------------------------
Bis P3 sagte der Umschalter nichts über die Grösse einer Box. Ein Zweibox-
Rezept braucht ~100 GiB je Box; auf einer zu kleinen oder vollen Box scheiterte
der Start erst Minuten später mit einer vLLM-Meldung — nachdem MC das laufende
Modell bereits verdrängt hatte. P4 prüft die Zahlen aus der Node-Agent-
Telemetrie VORHER, in der Liste und im Start.

Drei Regeln:
  1. Kapazität (hart): ``mem_total`` jeder Ziel-Box >= ``min_vram_gb``.
     Keine Telemetrie = kein Urteil (Hinweis, kein Riegel).
  2. Belegung (weich): zu wenig frei UND nichts zu verdrängen = Warnung,
     aber startbar (die Schätzung ist eine Schätzung).
  3. Platte (weich): weniger frei als ``est_weights_gb`` = Warnung. MC kann
     heute nicht feststellen, ob die Gewichte schon auf der Box liegen —
     ein Riegel wäre eine Lüge.

Testdaten heissen box-a / box-b / recipe-x. Kein Netz, keine Gerätedaten.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.services import recipe_switcher

TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"
ENV_FILE = "~/rezept/.env"
ENV_MAP = {"HEAD_IP": "{head_fabric_ip}", "WORKER_IP": "{worker_fabric_ip}"}

#: Eine GB10-Box, wie der node-agent sie meldet: 120 GiB Arbeitsspeicher
#: (= GPU-Speicher, unified memory), Platte gross und halb leer.
BIG_BOX = {
    "mem_total_mb": 122880,
    "mem_available_mb": 118784,
    "disk_total_gb": 3600.0,
    "disk_used_gb": 400.0,
}


def _telemetry(**kw) -> dict:
    values = dict(BIG_BOX)
    values.update(kw)
    return values


# ── Aufbau (gleiche Machart wie test_recipe_switcher_p3.py) ──────────────────


async def _host(
    session: AsyncSession,
    slug: str,
    *,
    kind: str = "ssh",
    ssh_host: str | None = "192.0.2.10",
    telemetry: dict | None = None,
    **kw,
) -> Host:
    host = Host(
        slug=slug,
        display_name=slug.upper(),
        kind=kind,
        ssh_host=ssh_host,
        agent_telemetry=telemetry,
        **kw,
    )
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, slug: str = "recipe-x", **kw) -> LocalRecipe:
    fields = dict(
        display_name=slug,
        engine="vllm_docker",
        model_identifier=f"org/{slug}",
        launch_template=TEMPLATE,
        port=8000,
    )
    fields.update(kw)
    recipe = LocalRecipe(slug=slug, **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _duo_recipe(session: AsyncSession, slug: str = "recipe-duo", **kw) -> LocalRecipe:
    fields = dict(topology={"nodes": 2}, env_file=ENV_FILE, env_map=dict(ENV_MAP), min_vram_gb=100.0)
    fields.update(kw)
    return await _recipe(session, slug, **fields)


async def _runtime(session: AsyncSession, slug: str, host: Host, **kw) -> Runtime:
    fields = dict(
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        launch_command="docker run --label mc.runtime.slug=x img",
        exclusive_memory=True,
    )
    fields.update(kw)
    rt = Runtime(slug=slug, host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _probe(running_slugs: set[str]):
    async def _fake(runtime: Runtime, **kw) -> bool:
        return runtime.slug in running_slugs

    return patch("app.services.recipe_switcher.probe_running", _fake)


def _by_slug(body: list[dict]) -> dict[str, dict]:
    return {e["slug"]: e for e in body}


# ── Reine Helfer ─────────────────────────────────────────────────────────────


def test_box_capacity_without_telemetry_is_none():
    host = Host(slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10")
    assert recipe_switcher.box_capacity(host) is None


def test_box_capacity_reads_the_last_heartbeat():
    host = Host(
        slug="box-a",
        display_name="BOX-A",
        kind="ssh",
        ssh_host="192.0.2.10",
        agent_telemetry=_telemetry(),
    )
    cap = recipe_switcher.box_capacity(host)
    assert cap == {
        "slug": "box-a",
        "mem_total_gb": 120.0,
        "mem_available_gb": 116.0,
        "disk_free_gb": 3200.0,
    }


def test_box_capacity_tolerates_half_filled_telemetry():
    """Eine Box mitten im Hochlauf meldet Felder als null — das ist kein Fehler."""
    host = Host(
        slug="box-a",
        display_name="BOX-A",
        kind="ssh",
        ssh_host="192.0.2.10",
        agent_telemetry={"mem_total_mb": 122880, "cpu_pct": 3.0},
    )
    assert recipe_switcher.box_capacity(host) == {
        "slug": "box-a",
        "mem_total_gb": 120.0,
        "mem_available_gb": None,
        "disk_free_gb": None,
    }


def test_box_capacity_ignores_a_heartbeat_without_any_numbers():
    host = Host(
        slug="box-a",
        display_name="BOX-A",
        kind="ssh",
        ssh_host="192.0.2.10",
        agent_telemetry={"cpu_pct": 3.0},
    )
    assert recipe_switcher.box_capacity(host) is None


# ── Regel 1: Kapazität (hart) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_box_that_is_too_small_is_grey_with_the_numbers(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_total_mb=61440))
    await _recipe(session, min_vram_gb=100.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is False
    assert entry["reason"] == "Box 'box-a' hat 60 GB, Rezept braucht 100 GB."
    assert entry["capacity"]["ok"] is False


@pytest.mark.asyncio
async def test_a_big_enough_box_stays_startable(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry())
    await _recipe(session, min_vram_gb=100.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["capacity"] == {
        "ok": True,
        "warnings": [],
        "boxes": [
            {
                "slug": "box-a",
                "mem_total_gb": 120.0,
                "mem_available_gb": 116.0,
                "disk_free_gb": 3200.0,
            }
        ],
    }


@pytest.mark.asyncio
async def test_a_recipe_without_a_size_is_never_blocked(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_total_mb=4096))
    await _recipe(session)  # min_vram_gb = NULL

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["capacity"]["ok"] is True
    assert entry["capacity"]["warnings"] == []


@pytest.mark.asyncio
async def test_missing_telemetry_warns_but_does_not_block(auth_client, session):
    box_a = await _host(session, "box-a")  # nie einen Heartbeat gesehen
    await _recipe(session, min_vram_gb=100.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["capacity"]["ok"] is True
    assert entry["capacity"]["warnings"] == ["Box 'box-a': Kapazität unbekannt (keine Telemetrie)."]
    assert entry["capacity"]["boxes"] == [
        {"slug": "box-a", "mem_total_gb": None, "mem_available_gb": None, "disk_free_gb": None}
    ]


# ── Regel 2: Belegung (weich) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_little_free_memory_warns_but_stays_startable(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_available_mb=20480))
    await _recipe(session, min_vram_gb=100.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["reason"] is None
    assert entry["capacity"]["warnings"] == [
        "Box 'box-a': nur 20 GB frei, Start kann am Speicher scheitern."
    ]


@pytest.mark.asyncio
async def test_no_warning_when_the_start_frees_the_memory_itself(auth_client, session):
    """Auf der Box läuft ein exklusives Modell — der Start verdrängt es und
    macht damit genau den Speicher frei, über den wir sonst warnen würden."""
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_available_mb=20480))
    await _recipe(session, min_vram_gb=100.0)
    await _recipe(session, "recipe-other", min_vram_gb=100.0, port=8001)
    await _runtime(session, "other-box-a", box_a, model_identifier="org/recipe-other",
                   endpoint="http://192.0.2.10:8001/v1")

    with _probe({"other-box-a"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["capacity"]["warnings"] == []


@pytest.mark.asyncio
async def test_a_shared_box_still_warns_when_nothing_would_be_evicted(auth_client, session):
    """Läuft dort nur etwas NICHT-exklusives, verdrängt der Start nichts —
    dann bleibt die Warnung ehrlich."""
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_available_mb=20480))
    await _recipe(session, min_vram_gb=100.0)
    await _recipe(session, "recipe-other", port=8001)
    await _runtime(session, "other-box-a", box_a, model_identifier="org/recipe-other",
                   endpoint="http://192.0.2.10:8001/v1", exclusive_memory=False)

    with _probe({"other-box-a"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["capacity"]["warnings"] == [
        "Box 'box-a': nur 20 GB frei, Start kann am Speicher scheitern."
    ]


# ── Regel 3: Platte (weich) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_full_disk_warns_but_does_not_block(auth_client, session):
    box_a = await _host(
        session, "box-a", telemetry=_telemetry(disk_total_gb=500.0, disk_used_gb=470.0)
    )
    await _recipe(session, min_vram_gb=100.0, est_weights_gb=110.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["startable"] is True
    assert entry["capacity"]["ok"] is True
    assert entry["capacity"]["warnings"] == [
        "Box 'box-a': nur 30 GB Platte frei, die Gewichte brauchen etwa 110 GB."
    ]


@pytest.mark.asyncio
async def test_the_disk_is_only_checked_on_the_head(auth_client, session):
    """Die Gewichte holt sich der Worker selbst — MC lädt nur auf dem Kopf."""
    box_a = await _host(session, "box-a", telemetry=_telemetry(), fabric_ip="10.0.0.1")
    await _host(
        session,
        "box-b",
        ssh_host="192.0.2.11",
        fabric_ip="10.0.0.2",
        role="worker",
        telemetry=_telemetry(disk_total_gb=500.0, disk_used_gb=490.0),
    )
    await _duo_recipe(session, est_weights_gb=110.0)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]

    assert entry["capacity"]["warnings"] == []


# ── Duo: beide Boxen ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_duo_checks_head_and_worker(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(), fabric_ip="10.0.0.1")
    await _host(
        session,
        "box-b",
        ssh_host="192.0.2.11",
        fabric_ip="10.0.0.2",
        role="worker",
        telemetry=_telemetry(mem_total_mb=61440),
    )
    await _duo_recipe(session)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]

    assert entry["startable"] is False
    assert entry["reason"] == "Box 'box-b' hat 60 GB, Rezept braucht 100 GB."
    assert [b["slug"] for b in entry["capacity"]["boxes"]] == ["box-a", "box-b"]


@pytest.mark.asyncio
async def test_a_duo_warns_for_the_worker_box_too(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(), fabric_ip="10.0.0.1")
    await _host(
        session,
        "box-b",
        ssh_host="192.0.2.11",
        fabric_ip="10.0.0.2",
        role="worker",
        telemetry=_telemetry(mem_available_mb=30720),
    )
    await _duo_recipe(session)

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]

    assert entry["startable"] is True
    assert entry["capacity"]["warnings"] == [
        "Box 'box-b': nur 30 GB frei, Start kann am Speicher scheitern."
    ]


@pytest.mark.asyncio
async def test_the_running_reason_still_wins_over_capacity(auth_client, session):
    """Ein laufendes Rezept sagt „läuft bereits" — nicht „Box zu klein"."""
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_total_mb=61440))
    recipe = await _recipe(session, min_vram_gb=100.0)
    await _runtime(session, "recipe-x-box-a", box_a, model_identifier=recipe.model_identifier)

    with _probe({"recipe-x-box-a"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["running"] is True
    assert entry["reason"] == recipe_switcher.REASON_RUNNING


# ── Der Start: derselbe Satz, 409, VOR jeder Verdrängung ─────────────────────


class _FakeBox:
    """Zählt SSH-Aufrufe — ein Start, der hier ankommt, hat schon zugegriffen."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, command: str, **kw):
        self.calls.append(command)
        return "", "", 0


@pytest.mark.asyncio
async def test_starting_on_a_too_small_box_is_409_and_touches_nothing(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_total_mb=61440))
    await _recipe(session, min_vram_gb=100.0)

    ssh = _FakeBox()
    start = AsyncMock(return_value={"ok": True})
    evict = AsyncMock(return_value={"ok": True})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Box 'box-a' hat 60 GB, Rezept braucht 100 GB."
    assert start.await_count == 0
    assert evict.await_count == 0
    assert ssh.calls == []
    # Und keine halbe Instanz in der Datenbank.
    assert (await session.exec(select(Runtime))).all() == []


@pytest.mark.asyncio
async def test_starting_a_duo_with_a_too_small_worker_is_409(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(), fabric_ip="10.0.0.1")
    await _host(
        session,
        "box-b",
        ssh_host="192.0.2.11",
        fabric_ip="10.0.0.2",
        role="worker",
        telemetry=_telemetry(mem_total_mb=61440),
    )
    await _duo_recipe(session)

    ssh = _FakeBox()
    start = AsyncMock(return_value={"ok": True})
    evict = AsyncMock(return_value={"ok": True})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Box 'box-b' hat 60 GB, Rezept braucht 100 GB."
    assert evict.await_count == 0
    assert ssh.calls == []


@pytest.mark.asyncio
async def test_a_soft_warning_never_stops_a_start(auth_client, session):
    box_a = await _host(session, "box-a", telemetry=_telemetry(mem_available_mb=10240))
    await _recipe(session, min_vram_gb=100.0)

    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", _FakeBox()),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")

    assert resp.status_code == 200, resp.text
    assert start.await_count == 1
