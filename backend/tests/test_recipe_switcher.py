"""Rezept-Umschalter P0+P1 (Vertrag 02.09.2026) — Liste, Start, Pflichtfeld.

Was hier abgesichert wird:
  * ``GET /api/v1/hosts/{id}/recipes``: solo/duo/none, die Grau-Sätze, die
    Reihenfolge (laufend → startbar → grau), Belegung über ``runtime_hosts``,
    Port-Kollision auf derselben Box, Box ohne SSH.
  * ``POST …/recipes/{slug}/start``: legt die Instanz an und startet über den
    bestehenden ``start_runtime`` (gemockt), 409 bei zwei Boxen, 422 ohne
    Startbefehl, 409 ohne SSH, admin-only.
  * Startbefehl-Pflicht in ``POST/PATCH /runtimes/db`` (422 mit Satz).
  * Rückwärtskompatibilität: Katalog ohne ``topology`` = solo, Cloud-Runtimes
    ohne Box werden nicht angefasst.
  * Umwandlung alter sparkrun-Katalogzeilen (Startbefehl bleibt, Engine wird
    ein gewöhnlicher Docker-Start).
  * Migration 0191 (Quelltext-Ebene, wie 0178): beide Spalten, nullable,
    Downgrade nimmt sie zurück, keine Datenzeilen.

Testdaten heissen box-a / box-b / recipe-x. Kein Netz: die Health-Probe
(``recipe_switcher.probe_running``) und ``start_runtime`` werden ersetzt.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import ENGINES, LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services import local_registry, recipe_switcher
from tests.conftest import test_engine

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

# Ein Startbefehl mit dem Pflicht-Label — mehr braucht ein Rezept nicht.
TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str, *, kind: str = "ssh", ssh_host: str | None = "192.0.2.10", **kw) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind=kind, ssh_host=ssh_host, **kw)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, slug: str = "recipe-x", **kw) -> LocalRecipe:
    fields = dict(
        display_name=slug.replace("-", " ").title(),
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


async def _runtime(session: AsyncSession, slug: str, host: Host | None, **kw) -> Runtime:
    fields = dict(
        display_name=slug,
        runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1",
        launch_command="docker run --label mc.runtime.slug=x img",
        exclusive_memory=True,
    )
    fields.update(kw)
    rt = Runtime(slug=slug, host_id=host.id if host else None, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _probe(running_slugs: set[str]):
    """Health-Probe-Ersatz: nur die genannten Runtime-Slugs gelten als gesund."""

    async def _fake(runtime: Runtime, **kw) -> bool:
        return runtime.slug in running_slugs

    return patch("app.services.recipe_switcher.probe_running", _fake)


async def _viewer_headers() -> dict[str, str]:
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer", role="viewer", is_active=True))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(uid), 'viewer')}"}


def _by_slug(body: list[dict]) -> dict[str, dict]:
    return {e["slug"]: e for e in body}


# ── Reine Helfer ─────────────────────────────────────────────────────────────


def test_recipe_nodes_treats_missing_or_broken_topology_as_solo():
    assert recipe_switcher.recipe_nodes(None) == 1
    assert recipe_switcher.recipe_nodes({}) == 1
    assert recipe_switcher.recipe_nodes({"nodes": "zwei"}) == 1
    assert recipe_switcher.recipe_nodes({"nodes": 0}) == 1
    assert recipe_switcher.recipe_nodes({"nodes": 2}) == 2


def test_endpoint_port_reads_the_port_or_none():
    assert recipe_switcher.endpoint_port("http://192.0.2.10:8080/v1") == 8080
    assert recipe_switcher.endpoint_port("http://192.0.2.10/v1") is None
    assert recipe_switcher.endpoint_port(None) is None


# ── GET /hosts/{id}/recipes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solo_recipe_without_instance_is_startable(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")  # topology bleibt NULL → solo, wie heute

    with _probe(set()):
        resp = await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")

    assert resp.status_code == 200
    entry = _by_slug(resp.json())["recipe-x"]
    assert entry["topology"] == {"nodes": 1}
    assert entry["fit"] == "solo"
    assert entry["startable"] is True
    assert entry["reason"] is None
    assert entry["running"] is False
    assert entry["instance_runtime_id"] is None
    assert entry["busy_hosts"] == []
    assert entry["port"] == 8000
    assert entry["engine"] == "vllm_docker"


@pytest.mark.asyncio
async def test_host_lookup_accepts_slug_and_unknown_is_404(auth_client, session):
    await _host(session, "box-a")
    with _probe(set()):
        assert (await auth_client.get("/api/v1/hosts/box-a/recipes")).status_code == 200
        assert (await auth_client.get("/api/v1/hosts/box-zz/recipes")).status_code == 404


@pytest.mark.asyncio
async def test_duo_needs_a_free_second_box(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    # P3: ein Duo-Rezept ist nur startbar, wenn der Katalog sagt, wohin die
    # Adressen gehören — sonst ist der Eintrag grau (env_ready=False).
    await _recipe(session, "recipe-duo", topology={"nodes": 2},
                  env_file="~/rezept/.env", env_map={"WORKER_IP": "{worker_ip}"})

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]
    assert entry["fit"] == "duo"
    assert entry["startable"] is True
    assert entry["candidate_workers"] == [{"host_id": str(box_b.id), "slug": "box-b", "role": None}]

    # box-b wird von einer laufenden exklusiven Runtime belegt → keine freie zweite Box.
    await _runtime(session, "other-on-b", box_b, endpoint="http://192.0.2.11:8000/v1")
    with _probe({"other-on-b"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]
    assert entry["fit"] == "none"
    assert entry["startable"] is False
    assert entry["reason"] == "braucht 2 Boxen — keine freie zweite Box"
    assert entry["candidate_workers"] == []


@pytest.mark.asyncio
async def test_missing_command_is_reported_as_a_sentence(auth_client, session):
    box_a = await _host(session, "box-a")
    # ssh_process hat keinen Engine-Standard → ohne Template kein Startbefehl.
    await _recipe(session, "recipe-nocmd", engine="ssh_process", launch_template=None)
    # Eine vorhandene Instanz ohne Befehl bleibt lesbar, ist aber nicht startbar.
    with_instance = await _recipe(session, "recipe-inst")
    await _runtime(session, "inst-a", box_a, launch_command=None,
                   topology={"nodes": 1, "recipe_slug": with_instance.slug})

    with _probe(set()):
        body = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())

    assert body["recipe-nocmd"]["startable"] is False
    assert body["recipe-nocmd"]["reason"] == "Startbefehl fehlt"
    assert body["recipe-inst"]["startable"] is False
    assert body["recipe-inst"]["reason"] == "Startbefehl fehlt"
    assert body["recipe-inst"]["instance_runtime_id"] is not None


@pytest.mark.asyncio
async def test_running_instance_is_first_then_startable_then_grey(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-a-grey", engine="ssh_process", launch_template=None)
    # min_vram_gb: ein exklusives Rezept verdrängt die laufende exklusive
    # Instanz auf demselben Port — der normale Wechsel, keine Kollision.
    await _recipe(session, "recipe-b-start", min_vram_gb=90.0)
    running = await _recipe(session, "recipe-z-run")
    inst = await _runtime(session, "run-a", box_a, topology={"nodes": 1, "recipe_slug": running.slug})

    with _probe({"run-a"}):
        body = (await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json()

    assert [e["slug"] for e in body] == ["recipe-z-run", "recipe-b-start", "recipe-a-grey"]
    top = body[0]
    assert top["running"] is True
    assert top["instance_runtime_id"] == str(inst.id)
    assert top["busy_hosts"] == ["box-a"]


@pytest.mark.asyncio
async def test_instance_is_recognised_by_recipe_ref_in_launch_command(auth_client, session):
    """Vorhandene Runtimes mit ``uvx sparkrun run <ref>`` bleiben unverändert
    und werden trotzdem als Instanz erkannt (Vertrag, Regel 6)."""
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x", recipe_ref="@reg/recipe-x", launch_template=None)
    legacy = await _runtime(
        session, "legacy-a", box_a,
        launch_command="uvx sparkrun run @reg/recipe-x --solo --label mc.runtime.slug=legacy-a",
    )
    with _probe({"legacy-a"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["instance_runtime_id"] == str(legacy.id)
    assert entry["running"] is True
    assert entry["startable"] is False  # läuft → nicht doppelt startbar
    assert entry["reason"] == "läuft bereits auf dieser Box"


@pytest.mark.asyncio
async def test_busy_hosts_include_members_from_runtime_hosts(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    box_c = await _host(session, "box-c", ssh_host="192.0.2.12")
    duo = await _recipe(session, "recipe-duo", topology={"nodes": 2})
    head = await _runtime(session, "duo-head", box_a, topology={"nodes": 2, "recipe_slug": duo.slug})
    session.add(RuntimeHost(runtime_id=head.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()

    with _probe({"duo-head"}):
        body = (await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json()
    entry = _by_slug(body)["recipe-duo"]

    assert entry["busy_hosts"] == ["box-a", "box-b"]
    # box-b ist Mitglied eines laufenden exklusiven Verbunds → kein Kandidat mehr.
    assert [w["slug"] for w in entry["candidate_workers"]] == ["box-c"]


@pytest.mark.asyncio
async def test_port_collision_on_the_same_box(auth_client, session):
    box_a = await _host(session, "box-a")
    # Ein kleiner Dienst (nicht exklusiv) belegt 8080 — den räumt ein Start nicht weg.
    await _runtime(session, "embed-a", box_a, display_name="Embeddings",
                   endpoint="http://192.0.2.10:8080/v1", exclusive_memory=False)
    await _recipe(session, "recipe-port", port=8080)
    # Exklusiv gegen exklusiv auf demselben Port ist der normale Wechsel, keine Kollision.
    await _runtime(session, "big-a", box_a, display_name="Big", endpoint="http://192.0.2.10:8000/v1")
    await _recipe(session, "recipe-x", port=8000, min_vram_gb=90.0)

    with _probe({"embed-a", "big-a"}):
        body = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())

    assert body["recipe-port"]["startable"] is False
    assert body["recipe-port"]["reason"] == "Port 8080 auf dieser Box belegt durch Embeddings"
    assert body["recipe-x"]["startable"] is True
    assert body["recipe-x"]["reason"] is None


@pytest.mark.asyncio
async def test_host_without_ssh_reports_it_honestly(auth_client, session):
    agent_box = await _host(session, "box-a", kind="agent", ssh_host=None)
    await _recipe(session, "recipe-x")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{agent_box.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is False
    assert entry["reason"] == "Box hat keinen SSH-Zugang — MC kann hier nichts starten"


@pytest.mark.asyncio
async def test_cloud_runtimes_without_host_are_left_alone(auth_client, session):
    """Eine Runtime ohne Box (Cloud) ist nie eine Instanz und wird nicht geprobt."""
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-x")
    await _runtime(session, "cloud", None, runtime_type="cloud", model_identifier=recipe.model_identifier,
                   endpoint="https://api.example/v1")

    probe = AsyncMock(return_value=True)
    with patch("app.services.recipe_switcher.probe_running", probe):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]

    assert entry["instance_runtime_id"] is None
    assert entry["running"] is False
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_hidden_recipes_are_not_listed(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-hidden", enabled=False)
    with _probe(set()):
        assert _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json()) == {}


# ── Handle-Pflicht für Host-Engines (Vorfall 03.09.2026) ─────────────────────
#
# Eine Host-Engine startet MC über SSH und findet sie danach nur an einem Namen
# wieder. Fehlt der, ist der Start ein Einbahnweg — und die Verdrängung, die
# ihm vorausginge, hätte das laufende Modell umsonst getötet. Darum grau in der
# Liste und 422 beim Start, bevor irgendetwas angefasst wird.

HOST_ENGINE_TEMPLATE = "cd ~/engines/{slug} && PORT={port} ./start.sh"


@pytest.mark.asyncio
async def test_host_engine_recipe_without_a_handle_is_grey(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-host", engine="ssh_process",
                  launch_template=HOST_ENGINE_TEMPLATE, process_name=None)
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["startable"] is False
    assert entry["reason"] == recipe_switcher.REASON_NO_HANDLE


@pytest.mark.asyncio
async def test_host_engine_recipe_with_a_handle_is_startable(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-host", engine="ssh_process",
                  launch_template=HOST_ENGINE_TEMPLATE, process_name="engine-server")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["startable"] is True
    assert entry["reason"] is None


@pytest.mark.asyncio
async def test_docker_recipes_stay_startable_without_a_handle(auth_client, session):
    """Docker-Engines vergeben ihren Containernamen im Template bzw. werden
    über das Label gefunden — für sie wäre die Pflicht eine Lüge."""
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x", process_name=None)
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is True
    assert entry["reason"] is None


@pytest.mark.asyncio
async def test_start_of_a_handleless_host_engine_is_422_and_creates_nothing(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-host", engine="ssh_process",
                  launch_template=HOST_ENGINE_TEMPLATE, process_name=None)
    start = AsyncMock()
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-host/start")
    assert resp.status_code == 422
    assert resp.json()["detail"] == recipe_switcher.REASON_NO_HANDLE
    start.assert_not_awaited()
    # Keine halbfertige Instanz zurückgelassen.
    assert (await session.exec(select(Runtime))).all() == []


# ── „läuft" braucht das Handle, nicht nur den Port (Live-Befund 03.09.2026) ──


async def _host_engine_pair(session, *, process_name: str | None):
    """Ein ssh_process-Rezept plus seine Instanz auf box-a."""
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-host", engine="ssh_process",
                           launch_template=HOST_ENGINE_TEMPLATE, process_name=process_name)
    await _runtime(session, "recipe-host-box-a", box_a, runtime_type="ssh_process",
                   model_identifier=recipe.model_identifier, process_name=process_name,
                   launch_command="cd ~/engines/x && PORT=8000 ./start.sh")
    return box_a


@pytest.mark.asyncio
async def test_instance_without_a_handle_is_not_running_even_if_the_port_answers(auth_client, session):
    """DER LIVE-BEFUND: eine nie gestartete Instanz galt als „läuft bereits",
    weil eine FREMDE Engine auf demselben Port antwortete."""
    box_a = await _host_engine_pair(session, process_name=None)
    port_answers = AsyncMock(return_value=True)
    with patch("app.services.runtime_manager._probe_http", port_answers):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["running"] is False
    assert entry["reason"] == recipe_switcher.REASON_NO_HANDLE
    # Der Port wird für eine Runtime ohne Handle gar nicht erst gefragt.
    port_answers.assert_not_awaited()


@pytest.mark.asyncio
async def test_instance_with_a_handle_that_is_not_alive_is_not_running(auth_client, session):
    """Port antwortet, Handle läuft nicht → das antwortet jemand anderes."""
    box_a = await _host_engine_pair(session, process_name="engine-server")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running", AsyncMock(return_value=False)):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["running"] is False
    assert entry["startable"] is True


@pytest.mark.asyncio
async def test_instance_counts_as_running_only_with_handle_and_port(auth_client, session):
    box_a = await _host_engine_pair(session, process_name="engine-server")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running", AsyncMock(return_value=True)):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["running"] is True
    assert entry["reason"] == recipe_switcher.REASON_RUNNING


@pytest.mark.asyncio
async def test_an_unreachable_box_is_no_proof_of_running(auth_client, session):
    """SSH kaputt heisst „wir wissen es nicht" — und ein unbelegtes „läuft"
    ist genau die Behauptung, die den Umschalter blockiert hat."""
    box_a = await _host_engine_pair(session, process_name="engine-server")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running",
                  AsyncMock(side_effect=OSError("connection refused"))):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-host"]
    assert entry["running"] is False


@pytest.mark.asyncio
async def test_instances_without_an_anchor_keep_the_plain_port_probe(auth_client, session):
    """Ohne Containernamen gibt es nichts Besseres zu fragen — Cloud- und
    lokale Docker-Runtimes behalten die reine HTTP-Probe."""
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-x")
    await _runtime(session, "recipe-x-box-a", box_a, model_identifier=recipe.model_identifier)
    anchor = AsyncMock(return_value=False)
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running", anchor):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["running"] is True
    anchor.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_instance_with_a_dead_container_is_not_running(auth_client, session):
    """DER ZWEITE LIVE-BEFUND: nach der Verdrängung war der Container dieses
    Rezepts beendet, aber der Nachfolger antwortete auf demselben Port — die
    Liste zeigte das verdrängte Rezept weiter als „läuft bereits"."""
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-x")
    await _runtime(session, "recipe-x-box-a", box_a, model_identifier=recipe.model_identifier,
                   container_name="recipe-x-head")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running", AsyncMock(return_value=False)):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["running"] is False
    assert entry["startable"] is True


@pytest.mark.asyncio
async def test_docker_instance_counts_as_running_with_container_and_port(auth_client, session):
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-x")
    await _runtime(session, "recipe-x-box-a", box_a, model_identifier=recipe.model_identifier,
                   container_name="recipe-x-head")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running", AsyncMock(return_value=True)):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["running"] is True
    assert entry["reason"] == recipe_switcher.REASON_RUNNING


@pytest.mark.asyncio
async def test_an_unreachable_box_is_no_proof_for_a_docker_instance_either(auth_client, session):
    box_a = await _host(session, "box-a")
    recipe = await _recipe(session, "recipe-x")
    await _runtime(session, "recipe-x-box-a", box_a, model_identifier=recipe.model_identifier,
                   container_name="recipe-x-head")
    with patch("app.services.runtime_manager._probe_http", AsyncMock(return_value=True)), \
            patch("app.services.runtime_manager.anchor_running",
                  AsyncMock(side_effect=OSError("connection refused"))):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["running"] is False


# ── POST /hosts/{id}/recipes/{slug}/start ────────────────────────────────────


@pytest.mark.asyncio
async def test_start_creates_the_instance_and_starts_it(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x", context_len=4096, min_vram_gb=90.0)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["created"] is True
    rt = (await session.exec(select(Runtime).where(Runtime.slug == body["runtime_slug"]))).first()
    assert rt is not None
    assert rt.host_id == box_a.id
    assert rt.runtime_type == "vllm_docker"
    assert rt.topology == {"nodes": 1, "recipe_slug": "recipe-x"}
    assert f"mc.runtime.slug={rt.slug}" in rt.launch_command
    assert rt.container_name == f"mc-{rt.slug}"
    assert rt.endpoint == "http://192.0.2.10:8000/v1"
    assert rt.exclusive_memory is True
    assert rt.max_context_len == 4096
    # Der bestehende Startpfad wurde mit der Instanz und ihrer Box aufgerufen.
    start.assert_awaited_once()
    assert start.call_args.args[0]["slug"] == rt.slug
    assert start.call_args.kwargs["host"].slug == "box-a"

    # Zweiter Klick: keine zweite Instanz.
    with patch("app.services.runtime_manager.start_runtime", start):
        again = (await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")).json()
    assert again["created"] is False
    assert again["runtime_id"] == body["runtime_id"]
    assert len((await session.exec(select(Runtime))).all()) == 1


@pytest.mark.asyncio
async def test_start_of_a_two_box_recipe_without_env_map_is_422(auth_client, session):
    """Bis P3 antwortete jeder Duo-Start mit „kommt in Phase 3". Seit P3 läuft
    er — aber nur, wenn der Katalog sagt, wohin die Adresse der zweiten Box
    gehört. Ohne das steht der Fehler von Anfang an fest, also 422 und keine
    halbfertige Instanz (der Rest des Duo-Starts: test_recipe_switcher_p3.py)."""
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-duo", topology={"nodes": 2})
    start = AsyncMock()
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")
    assert resp.status_code == 422
    assert resp.json()["detail"] == recipe_switcher.REASON_NO_ENV_MAP
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime))).all() == []


@pytest.mark.asyncio
async def test_start_without_command_is_422(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-nocmd", engine="ssh_process", launch_template=None)
    start = AsyncMock()
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-nocmd/start")
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Startbefehl fehlt")
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_on_a_box_without_ssh_is_409(auth_client, session):
    agent_box = await _host(session, "box-a", kind="agent", ssh_host=None)
    await _recipe(session, "recipe-x")
    resp = await auth_client.post(f"/api/v1/hosts/{agent_box.id}/recipes/recipe-x/start")
    assert resp.status_code == 409
    assert "SSH" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_start_is_admin_only(client, auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")
    headers = await _viewer_headers()
    resp = await client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start", headers=headers)
    assert resp.status_code == 403
    # Lesen bleibt offen.
    with _probe(set()):
        assert (await client.get(f"/api/v1/hosts/{box_a.id}/recipes", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_start_failure_surfaces_the_message(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")
    start = AsyncMock(return_value={"ok": False, "message": "Box-Speicher nicht frei"})
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Box-Speicher nicht frei"


@pytest.mark.asyncio
async def test_unknown_recipe_is_404(auth_client, session):
    box_a = await _host(session, "box-a")
    resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-zz/start")
    assert resp.status_code == 404


# ── Startbefehl-Pflicht in POST/PATCH /runtimes/db ───────────────────────────


def _runtime_body(host: Host, **overrides) -> dict:
    body = {
        "slug": "rt-x",
        "display_name": "RT X",
        "runtime_type": "vllm_docker",
        "endpoint": "http://192.0.2.10:8000/v1",
        "host_id": str(host.id),
        "enabled": True,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_create_runtime_requires_a_command_when_bound_and_enabled(auth_client, session):
    box_a = await _host(session, "box-a")

    resp = await auth_client.post("/api/v1/runtimes/db", json=_runtime_body(box_a))
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Startbefehl fehlt")

    ok = await auth_client.post("/api/v1/runtimes/db", json=_runtime_body(box_a, launch_command="docker run img"))
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_command_rule_leaves_cloud_lmstudio_and_disabled_rows_alone(auth_client, session):
    box_a = await _host(session, "box-a")
    cases = [
        _runtime_body(box_a, slug="cloud", runtime_type="cloud", host_id=None),
        _runtime_body(box_a, slug="lms", runtime_type="lmstudio", lms_identifier="m"),
        _runtime_body(box_a, slug="off", enabled=False),
    ]
    for body in cases:
        resp = await auth_client.post("/api/v1/runtimes/db", json=body)
        assert resp.status_code == 200, (body["slug"], resp.text)


@pytest.mark.asyncio
async def test_patch_keeps_old_rows_readable_but_blocks_re_enabling_without_command(auth_client, session):
    box_a = await _host(session, "box-a")
    await _runtime(session, "old-a", box_a, launch_command=None, enabled=False)

    # Ein Feld, das mit dem Start nichts zu tun hat, bleibt änderbar.
    assert (await auth_client.patch("/api/v1/runtimes/db/old-a", json={"ui_order": 3})).status_code == 200
    # Aktivieren ohne Befehl: nein, mit Satz.
    resp = await auth_client.patch("/api/v1/runtimes/db/old-a", json={"enabled": True})
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("Startbefehl fehlt")
    # Befehl nachtragen + aktivieren in einem Schritt: ja.
    resp = await auth_client.patch("/api/v1/runtimes/db/old-a", json={"enabled": True, "launch_command": "docker run img"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_patch_binding_a_commandless_enabled_row_to_a_host_is_422(auth_client, session):
    box_a = await _host(session, "box-a")
    await _runtime(session, "free-a", None, launch_command=None)
    resp = await auth_client.patch("/api/v1/runtimes/db/free-a", json={"host_id": str(box_a.id)})
    assert resp.status_code == 422


# ── Umwandlung alter sparkrun-Zeilen ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_sparkrun_catalog_rows_keep_their_command_and_become_plain_docker(session):
    session.add(LocalRecipe(slug="recipe-x", display_name="X", engine="sparkrun",
                            model_identifier="served-x", recipe_ref="@reg/recipe-x"))
    session.add(LocalRecipe(slug="recipe-own", display_name="Own", engine="sparkrun",
                            model_identifier="served-own", recipe_ref="@reg/recipe-own",
                            launch_template="uvx sparkrun run @reg/recipe-own --label mc.runtime.slug={slug}",
                            topology={"nodes": 2}))
    session.add(LocalRecipe(slug="recipe-plain", display_name="Plain", engine="llamacpp_docker",
                            model_identifier="org/plain"))
    await session.commit()
    box = await _host(session, "box-a")
    legacy_rt = await _runtime(session, "legacy-a", box,
                               launch_command="uvx sparkrun run @reg/recipe-x --solo --label mc.runtime.slug=legacy-a")

    changed = await local_registry.repair_legacy_sparkrun_rows(session)
    assert changed == 2

    rows = {r.slug: r for r in (await session.exec(select(LocalRecipe))).all()}
    assert rows["recipe-x"].engine == "vllm_docker"
    assert rows["recipe-x"].launch_template == (
        "uvx sparkrun run @reg/recipe-x --solo --no-rm --ensure --no-follow --label mc.runtime.slug={slug}"
    )
    assert rows["recipe-x"].topology == {"nodes": 1}
    # Ein eigener Befehl bleibt, ebenso eine gesetzte Topologie.
    assert rows["recipe-own"].engine == "vllm_docker"
    assert rows["recipe-own"].launch_template.startswith("uvx sparkrun run @reg/recipe-own")
    assert rows["recipe-own"].topology == {"nodes": 2}
    # Andere Engines und die Runtime-Zeilen werden nicht angefasst.
    assert rows["recipe-plain"].engine == "llamacpp_docker"
    await session.refresh(legacy_rt)
    assert legacy_rt.launch_command.startswith("uvx sparkrun run @reg/recipe-x")
    assert legacy_rt.runtime_type == "vllm_docker"

    # Zweiter Lauf: nichts mehr zu tun.
    assert await local_registry.repair_legacy_sparkrun_rows(session) == 0


def test_registry_spec_with_engine_sparkrun_is_normalised_on_import():
    """Eine fremde Registry darf weiterhin ``engine: sparkrun`` liefern — der
    Eintrag wird beim Import zum gewöhnlichen Startbefehl, nicht abgelehnt."""
    spec = local_registry.RecipeSpec(
        slug="recipe-x", display_name="X", engine="sparkrun",
        model_identifier="served-x", recipe_ref="@reg/recipe-x",
    )
    assert spec.validate_vocabulary() is None
    row = local_registry._row_from_spec(spec)  # noqa: SLF001
    assert row.engine == "vllm_docker"
    assert row.launch_template.startswith("uvx sparkrun run @reg/recipe-x")
    assert row.topology == {"nodes": 1}

    # Ohne recipe_ref gibt es nichts, woraus ein Befehl entstehen könnte:
    # der Eintrag bleibt lesbar, aber ohne Startweg (→ „Startbefehl fehlt").
    bare = local_registry.RecipeSpec(slug="recipe-y", display_name="Y", engine="sparkrun", model_identifier="m")
    assert bare.validate_vocabulary() is None
    bare_row = local_registry._row_from_spec(bare)  # noqa: SLF001
    assert bare_row.engine == "ssh_process"
    assert bare_row.launch_template is None
    assert recipe_switcher.has_launch_command(bare_row) is False


def test_seed_file_carries_no_sparkrun_engine_anymore():
    entries = local_registry._load_seed()  # noqa: SLF001
    assert entries
    for spec in entries:
        assert spec.engine != "sparkrun", spec.slug
        assert spec.engine in ENGINES, spec.slug


# ── Migration 0191 (Quelltext-Ebene, wie test_migration_0178_…) ──────────────


def test_migration_0191_adds_both_columns_nullable_and_takes_them_back():
    source = (MIGRATIONS / "0191_recipe_topology_port.py").read_text(encoding="utf-8")

    assert 'op.add_column("local_recipes", sa.Column("topology", sa.JSON(), nullable=True))' in source
    assert 'op.add_column("local_recipes", sa.Column("port", sa.Integer(), nullable=True))' in source
    assert 'op.drop_column("local_recipes", "port")' in source
    assert 'op.drop_column("local_recipes", "topology")' in source
    assert "server_default" not in source  # NULL = Solo, wie heute
    assert 'down_revision = "0190_host_device_state"' in source
    # Keine Datenzeilen: Gerätedaten sind Instanz-Sache, nicht Repo-Sache.
    for verb in ("INSERT", "UPDATE", "op.execute", "op.bulk_insert"):
        assert verb not in source, verb


@pytest.mark.asyncio
async def test_model_round_trips_topology_and_port(session):
    """Modell-Ebene: die neuen Spalten schreiben und lesen sich; NULL bleibt NULL."""
    session.add(LocalRecipe(slug="recipe-x", display_name="X", engine="vllm_docker",
                            model_identifier="m", topology={"nodes": 2}, port=8888))
    session.add(LocalRecipe(slug="recipe-y", display_name="Y", engine="vllm_docker", model_identifier="m2"))
    await session.commit()
    rows = {r.slug: r for r in (await session.exec(select(LocalRecipe))).all()}
    assert rows["recipe-x"].topology == {"nodes": 2} and rows["recipe-x"].port == 8888
    assert rows["recipe-y"].topology is None and rows["recipe-y"].port is None


@pytest.mark.asyncio
async def test_running_duo_is_fitted_not_reported_as_no_free_box(auth_client, session):
    """Live-Befund 02.09.: Ein laufendes Zweibox-Rezept belegt seine eigene
    zweite Box — das darf nicht als „keine freie zweite Box" gelten."""
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    recipe = await _recipe(session, "recipe-duo", topology={"nodes": 2})
    rt = await _runtime(session, "duo-a", box_a, exclusive_memory=True,
                        topology={"nodes": 2, "recipe_slug": recipe.slug})
    from app.models.runtime_host import RuntimeHost
    session.add(RuntimeHost(runtime_id=rt.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()

    with _probe({"duo-a"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]
    assert entry["running"] is True
    assert entry["fit"] == "duo"
    assert entry["startable"] is False
    assert entry["reason"] == "läuft bereits auf dieser Box"
    assert entry["busy_hosts"] == ["box-a", "box-b"]
    # Für einen NEUEN Start ist box-b nicht frei — Kandidatenliste bleibt leer.
    assert entry["candidate_workers"] == []

