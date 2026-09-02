"""Rezept-Umschalter P2 (Vertrag 02.09.2026) — Rolle, SSH für jede Box,
exklusiv-Flag, Install-Adressen, Agent-Skript-Verzeichnis.

Was hier abgesichert wird:
  * Migration 0192 (Quelltext-Ebene wie 0191) + Modell-Ebene: die drei
    Spalten schreiben und lesen sich, NULL bleibt NULL.
  * ``hosts.role``: Create/Update/Out, 422 mit Satz bei Tippfehler, PATCH
    jederzeit — und KEINE Sperre: eine Worker-Box startet Solo-Rezepte.
  * ``candidate_workers[].role`` + Reihenfolge (worker zuerst, dann übrige,
    stabil nach ui_order/slug).
  * SSH für ``kind=agent``: mit ssh_host startbar (Start läuft durch den
    bestehenden start_runtime), ohne → der Grau-Satz. Pairing nimmt ssh_host
    an; der Eintrag des Betreibers gewinnt gegen den vom Gerät.
  * ``local_recipes.exclusive``: Feld schlägt Heuristik, Heuristik als
    Fallback — beim Anlegen der Instanz UND in der Port-Kollisions-Regel.
  * ``install_commands``: eine Zeile je Adresse, Beschriftung über die
    Adressklasse, ``install_command`` = erste Zeile.
  * Agent-Skript: Leseroutine liest aus einem VERZEICHNIS, sieht eine
    ausgetauschte Datei (Deploy) und liefert None ohne Mount.

Testdaten heissen box-a / box-b / recipe-x. Kein Netz.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.routers import nodes as nodes_router
from app.services import host_resolver, local_registry, recipe_switcher

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(session: AsyncSession, slug: str, *, kind: str = "ssh", ssh_host: str | None = "192.0.2.10", **kw) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind=kind, ssh_host=ssh_host, **kw)
    session.add(host)
    await session.commit()
    await session.refresh(host)
    return host


async def _recipe(session: AsyncSession, slug: str = "recipe-x", **kw) -> LocalRecipe:
    fields = dict(display_name=slug, engine="vllm_docker", model_identifier=f"org/{slug}",
                  launch_template=TEMPLATE, port=8000)
    fields.update(kw)
    recipe = LocalRecipe(slug=slug, **fields)
    session.add(recipe)
    await session.commit()
    await session.refresh(recipe)
    return recipe


async def _runtime(session: AsyncSession, slug: str, host: Host, **kw) -> Runtime:
    fields = dict(display_name=slug, runtime_type="vllm_docker", endpoint="http://192.0.2.10:8000/v1",
                  launch_command="docker run --label mc.runtime.slug=x img", exclusive_memory=True)
    fields.update(kw)
    rt = Runtime(slug=slug, host_id=host.id, **fields)
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


def _probe(running_slugs: set[str]):
    async def _fake(runtime: Runtime) -> bool:
        return runtime.slug in running_slugs

    return patch("app.services.recipe_switcher.probe_running", _fake)


def _by_slug(body: list[dict]) -> dict[str, dict]:
    return {e["slug"]: e for e in body}


# ── 1. Migration 0192 ────────────────────────────────────────────────────────


def test_migration_0192_adds_three_nullable_columns_and_takes_them_back():
    source = (MIGRATIONS / "0192_host_role_recipe_exclusive.py").read_text(encoding="utf-8")
    assert 'op.add_column("hosts", sa.Column("role", sa.Text(), nullable=True))' in source
    assert 'op.add_column("hosts", sa.Column("fabric_ip", sa.Text(), nullable=True))' in source
    assert 'op.add_column("local_recipes", sa.Column("exclusive", sa.Boolean(), nullable=True))' in source
    assert 'op.add_column("host_pairing_codes", sa.Column("role", sa.Text(), nullable=True))' in source
    assert 'op.add_column("host_pairing_codes", sa.Column("ssh_host", sa.Text(), nullable=True))' in source
    assert 'op.drop_column("host_pairing_codes", "ssh_host")' in source
    assert 'op.drop_column("host_pairing_codes", "role")' in source
    assert 'op.drop_column("local_recipes", "exclusive")' in source
    assert 'op.drop_column("hosts", "fabric_ip")' in source
    assert 'op.drop_column("hosts", "role")' in source
    assert 'down_revision = "0191_recipe_topology_port"' in source
    assert "server_default" not in source  # NULL = „keine Meinung", wie jede alte Zeile
    for verb in ("INSERT", "UPDATE", "op.execute", "op.bulk_insert"):
        assert verb not in source, verb


@pytest.mark.asyncio
async def test_model_round_trips_role_fabric_ip_and_exclusive(session):
    session.add(Host(slug="box-a", display_name="A", kind="agent", role="worker", fabric_ip="192.0.2.77"))
    session.add(Host(slug="box-b", display_name="B", kind="agent"))
    session.add(LocalRecipe(slug="recipe-x", display_name="X", engine="vllm_docker", model_identifier="m", exclusive=False))
    session.add(LocalRecipe(slug="recipe-y", display_name="Y", engine="vllm_docker", model_identifier="m2"))
    await session.commit()
    hosts = {h.slug: h for h in (await session.exec(select(Host))).all()}
    recipes = {r.slug: r for r in (await session.exec(select(LocalRecipe))).all()}
    assert hosts["box-a"].role == "worker" and hosts["box-a"].fabric_ip == "192.0.2.77"
    assert hosts["box-b"].role is None and hosts["box-b"].fabric_ip is None
    assert recipes["recipe-x"].exclusive is False
    assert recipes["recipe-y"].exclusive is None


# ── 2. Host-Schemas: role + fabric_ip ────────────────────────────────────────


@pytest.mark.asyncio
async def test_host_create_and_out_carry_role_and_fabric_ip(auth_client):
    resp = await auth_client.post("/api/v1/hosts", json={
        "slug": "box-a", "display_name": "A", "kind": "agent", "role": "head", "fabric_ip": "192.0.2.70",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "head" and resp.json()["fabric_ip"] == "192.0.2.70"
    listed = (await auth_client.get("/api/v1/hosts")).json()
    assert listed[0]["role"] == "head" and listed[0]["fabric_ip"] == "192.0.2.70"


@pytest.mark.asyncio
async def test_host_role_typo_is_422_with_a_sentence(auth_client, session):
    resp = await auth_client.post("/api/v1/hosts", json={
        "slug": "box-a", "display_name": "A", "kind": "agent", "role": "master",
    })
    assert resp.status_code == 422
    assert "'head' oder 'worker'" in resp.text

    box_b = await _host(session, "box-b", kind="agent", ssh_host=None)
    resp = await auth_client.patch(f"/api/v1/hosts/{box_b.id}", json={"role": "slave"})
    assert resp.status_code == 422
    assert "'head' oder 'worker'" in resp.text


@pytest.mark.asyncio
async def test_host_role_can_be_changed_and_cleared_any_time(auth_client, session):
    box_a = await _host(session, "box-a", role="head")
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"role": "worker"})
    assert r.status_code == 200 and r.json()["role"] == "worker"
    # Gross-/Kleinschreibung wird vergeben, leerer String heisst „keine Rolle".
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"role": "HEAD"})
    assert r.json()["role"] == "head"
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"role": ""})
    assert r.json()["role"] is None
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"role": None, "fabric_ip": "192.0.2.71"})
    assert r.json()["role"] is None and r.json()["fabric_ip"] == "192.0.2.71"
    # fabric_ip wird getrimmt, leer → None (P3 schreibt es 1:1 in die .env).
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"fabric_ip": "  192.0.2.72 "})
    assert r.json()["fabric_ip"] == "192.0.2.72"
    r = await auth_client.patch(f"/api/v1/hosts/{box_a.id}", json={"fabric_ip": "   "})
    assert r.json()["fabric_ip"] is None
    r = await auth_client.post("/api/v1/hosts", json={"slug": "box-b", "display_name": "B", "kind": "agent", "fabric_ip": " "})
    assert r.status_code == 200 and r.json()["fabric_ip"] is None


@pytest.mark.asyncio
async def test_role_never_blocks_a_solo_start(auth_client, session):
    """Rolle = Vorbelegung, keine Regel: eine Worker-Box startet Solo-Rezepte."""
    worker = await _host(session, "box-a", role="worker")
    await _recipe(session, "recipe-x")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{worker.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is True and entry["reason"] is None

    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{worker.id}/recipes/recipe-x/start")
    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()


# ── 3. candidate_workers: Rolle + Reihenfolge ────────────────────────────────


@pytest.mark.asyncio
async def test_candidate_workers_carry_role_and_put_workers_first(auth_client, session):
    head = await _host(session, "box-a", role="head", ui_order=0)
    # ui_order würde box-b VOR box-c und box-d stellen — die Rolle gewinnt.
    await _host(session, "box-b", ui_order=1)
    await _host(session, "box-c", ui_order=2, role="head")
    await _host(session, "box-d", ui_order=3, role="worker")
    await _recipe(session, "recipe-x", topology={"nodes": 2})

    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{head.id}/recipes")).json())["recipe-x"]

    assert [(w["slug"], w["role"]) for w in entry["candidate_workers"]] == [
        ("box-d", "worker"),
        ("box-b", None),
        ("box-c", "head"),
    ]
    assert entry["fit"] == "duo"


def test_worker_candidates_is_stable_within_a_role_group():
    head = Host(slug="box-a", display_name="A", kind="ssh")
    hosts = [
        head,
        Host(slug="box-c", display_name="C", kind="ssh", ui_order=5, role="worker"),
        Host(slug="box-b", display_name="B", kind="ssh", ui_order=5, role="worker"),
        Host(slug="box-d", display_name="D", kind="ssh", ui_order=1),
    ]
    out = recipe_switcher.worker_candidates(hosts, head, set())
    assert [w["slug"] for w in out] == ["box-b", "box-c", "box-d"]
    # Eine belegte Box fällt weg, egal welche Rolle sie hat.
    out = recipe_switcher.worker_candidates(hosts, head, {hosts[2].id})
    assert [w["slug"] for w in out] == ["box-c", "box-d"]


# ── 4. SSH für kind=agent ────────────────────────────────────────────────────


def test_ssh_capable_is_the_one_rule():
    assert host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10"))
    assert host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="agent", ssh_host="192.0.2.10"))
    assert not host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="agent", ssh_host=None))
    assert not host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="agent", ssh_host="   "))
    assert not host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="ssh", ssh_host=None))
    assert not host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="flask_wol", ssh_host="192.0.2.10"))
    assert not host_resolver.ssh_capable(Host(slug="box-a", display_name="A", kind="local", ssh_host="192.0.2.10"))
    assert not host_resolver.ssh_capable(None)
    # Gilt auch für den session-freien ResolvedHost (Wächter, Lebenszyklus).
    assert host_resolver.ssh_capable(host_resolver.ResolvedHost(kind="agent", ssh_host="192.0.2.10"))
    assert not host_resolver.ssh_capable(host_resolver.ResolvedHost(kind="agent"))


@pytest.mark.asyncio
async def test_agent_host_with_ssh_host_is_startable_and_starts(auth_client, session):
    box_a = await _host(session, "box-a", kind="agent", ssh_host="192.0.2.10")
    await _recipe(session, "recipe-x")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is True and entry["reason"] is None

    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 200, resp.text
    # Der Start ging an die Box unter ihrer SSH-Adresse — wie bei kind=ssh.
    resolved = start.await_args.kwargs["host"]
    assert resolved.kind == "agent" and resolved.ssh_host == "192.0.2.10"


@pytest.mark.asyncio
async def test_agent_host_without_ssh_host_reports_it_honestly(auth_client, session):
    box_a = await _host(session, "box-a", kind="agent", ssh_host=None)
    await _recipe(session, "recipe-x")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is False
    assert entry["reason"].startswith("Box hat keinen SSH-Zugang")
    resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_resolved_host_carries_credential_role_and_fabric_ip(session):
    import uuid

    cred = uuid.uuid4()
    box_a = Host(slug="box-a", display_name="A", kind="agent", ssh_host="192.0.2.10",
                 ssh_credential_id=None, role="worker", fabric_ip="192.0.2.80")
    resolved = host_resolver.resolved_host_from_row(box_a)
    assert resolved.role == "worker" and resolved.fabric_ip == "192.0.2.80"
    box_a.ssh_credential_id = cred
    assert host_resolver.resolved_host_from_row(box_a).ssh_credential_id == cred


@pytest.mark.asyncio
async def test_probe_and_bootstrap_accept_agent_host_with_ssh_and_refuse_without(auth_client, session):
    with_ssh = await _host(session, "box-a", kind="agent", ssh_host="192.0.2.10")
    without = await _host(session, "box-b", kind="agent", ssh_host=None)

    probe = AsyncMock(return_value={"reachable": True})
    with patch("app.services.host_probe.probe_host", probe):
        ok = await auth_client.post("/api/v1/hosts/probe", json={"host_id": str(with_ssh.id)})
        refused = await auth_client.post("/api/v1/hosts/probe", json={"host_id": str(without.id)})
    assert ok.status_code == 200, ok.text
    assert refused.status_code == 400 and "keinen SSH-Zugang" in refused.json()["detail"]

    with patch("app.services.host_bootstrap.get_status", AsyncMock(return_value=None)), \
         patch("app.services.host_bootstrap.start_bootstrap", AsyncMock()) as start:
        ok = await auth_client.post(f"/api/v1/hosts/{with_ssh.id}/bootstrap")
        refused = await auth_client.post(f"/api/v1/hosts/{without.id}/bootstrap")
    assert ok.status_code == 202, ok.text
    start.assert_awaited_once()
    assert refused.status_code == 400 and "keinen SSH-Zugang" in refused.json()["detail"]


@pytest.mark.asyncio
async def test_pair_never_takes_the_ssh_host_the_device_reports(client, auth_client, session):
    """Review 03.09.2026: /pair ist unauthentifiziert. Das Gerät darf nicht
    bestimmen, wohin MC später per SSH verbindet — die Geräte-Adresse landet
    NIE, auch wenn Host und Code keine tragen. Feld bleibt aus Kompatibilität."""
    import uuid

    # Leerer Host, leerer Code → trotzdem keine Adresse vom Gerät.
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    resp = await client.post("/api/v1/nodes/pair", json={
        "code": minted["code"], "hostname": "box-a", "ssh_host": "192.0.2.99",
    })
    assert resp.status_code == 200, resp.text
    host = await session.get(Host, uuid.UUID(resp.json()["host_id"]))
    assert host.kind == "agent" and host.ssh_host is None
    assert not host_resolver.ssh_capable(host)

    # Vorhandener Host mit Betreiber-Eintrag: bleibt unverändert.
    box_b = await _host(session, "box-b", kind="agent", ssh_host="192.0.2.20")
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={"host_id": str(box_b.id)})).json()
    resp = await client.post("/api/v1/nodes/pair", json={
        "code": minted["code"], "hostname": "box-b", "ssh_host": "192.0.2.99",
    })
    assert resp.status_code == 200, resp.text
    await session.refresh(box_b)
    assert box_b.ssh_host == "192.0.2.20"

    # Ohne ssh_host im Pairing: wie vorher.
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={})).json()
    resp = await client.post("/api/v1/nodes/pair", json={"code": minted["code"], "hostname": "box-c"})
    assert resp.status_code == 200
    host = await session.get(Host, uuid.UUID(resp.json()["host_id"]))
    assert host.ssh_host is None


# ── 5. exclusive: Feld schlägt Heuristik ─────────────────────────────────────


def test_recipe_is_exclusive_field_beats_heuristic_and_heuristic_is_fallback():
    base = dict(display_name="X", engine="vllm_docker", model_identifier="m")
    # Feld gesetzt → Feld gewinnt, egal was min_vram_gb sagt.
    assert recipe_switcher.recipe_is_exclusive(LocalRecipe(slug="recipe-x", exclusive=True, **base)) is True
    assert recipe_switcher.recipe_is_exclusive(LocalRecipe(slug="recipe-x", exclusive=False, min_vram_gb=90.0, **base)) is False
    assert recipe_switcher.recipe_is_exclusive(LocalRecipe(slug="recipe-x", exclusive=True, min_vram_gb=None, **base)) is True
    # Feld leer → Heuristik: min_vram_gb gesetzt heisst exklusiv.
    assert recipe_switcher.recipe_is_exclusive(LocalRecipe(slug="recipe-x", min_vram_gb=90.0, **base)) is True
    assert recipe_switcher.recipe_is_exclusive(LocalRecipe(slug="recipe-x", **base)) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exclusive, min_vram_gb, expected",
    [
        (False, 90.0, False),  # Feld schlägt Heuristik
        (True, None, True),    # Feld schlägt Heuristik
        (None, 90.0, True),    # Heuristik als Fallback
        (None, None, False),   # Heuristik als Fallback
    ],
)
async def test_instance_exclusive_memory_comes_from_field_then_heuristic(auth_client, session, exclusive, min_vram_gb, expected):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x", exclusive=exclusive, min_vram_gb=min_vram_gb)
    with patch("app.services.runtime_manager.start_runtime", AsyncMock(return_value={"ok": True})):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 200, resp.text
    rt = (await session.exec(select(Runtime).where(Runtime.slug == resp.json()["runtime_slug"]))).first()
    assert rt.exclusive_memory is expected


@pytest.mark.asyncio
async def test_exclusive_field_drives_the_port_collision_rule(auth_client, session):
    """Ein Rezept mit exclusive=False verdrängt nichts — der Port bleibt belegt.
    Vor P2 hätte min_vram_gb allein „verdrängt statt Kollision" ergeben."""
    box_a = await _host(session, "box-a")
    await _runtime(session, "blocker", box_a, display_name="Blocker", exclusive_memory=True)
    await _recipe(session, "recipe-x", exclusive=False, min_vram_gb=90.0, port=8000)
    with _probe({"blocker"}):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["startable"] is False
    assert entry["reason"] == "Port 8000 auf dieser Box belegt durch Blocker"


@pytest.mark.asyncio
async def test_catalog_exposes_exclusive_and_the_effective_value(auth_client, session):
    await _recipe(session, "recipe-x", exclusive=False, min_vram_gb=90.0)
    await _recipe(session, "recipe-y", min_vram_gb=90.0)
    await _recipe(session, "recipe-z")
    body = {e["slug"]: e for e in (await auth_client.get("/api/v1/local-registry")).json()["recipes"]}
    assert body["recipe-x"]["exclusive"] is False and body["recipe-x"]["exclusive_effective"] is False
    assert body["recipe-y"]["exclusive"] is None and body["recipe-y"]["exclusive_effective"] is True
    assert body["recipe-z"]["exclusive"] is None and body["recipe-z"]["exclusive_effective"] is False


def test_recipe_spec_round_trips_exclusive_and_update_moves_it():
    spec = local_registry.RecipeSpec(slug="recipe-x", display_name="X", engine="vllm_docker",
                                     model_identifier="m", exclusive=False)
    row = local_registry._row_from_spec(spec)  # noqa: SLF001
    assert row.exclusive is False
    # Fehlendes Feld bleibt NULL (Heuristik-Fallback), wird nicht zu False.
    row2 = local_registry._row_from_spec(local_registry.RecipeSpec(  # noqa: SLF001
        slug="recipe-y", display_name="Y", engine="vllm_docker", model_identifier="m"))
    assert row2.exclusive is None
    # Ein Refresh trägt eine Änderung nach.
    changed = local_registry._apply_update(row, spec.model_copy(update={"exclusive": True}))  # noqa: SLF001
    assert changed is True and row.exclusive is True


# ── 6. Install-Befehle mit Adress-Wahl ───────────────────────────────────────


def _no_interfaces():
    return patch.object(nodes_router, "_interface_addresses", lambda: [])


def test_install_base_urls_labels_and_orders_all_addresses():
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "http://192.0.2.5:8000, https://mc.tailnet-name.ts.net"), \
         patch.object(settings, "mc_base_url", "http://localhost"), \
         patch.object(settings, "public_host", "10.0.0.5"), _no_interfaces():
        entries = nodes_router.install_base_urls()
    assert entries == [
        {"label": "Tailscale", "url": "https://mc.tailnet-name.ts.net"},
        {"label": "LAN", "url": "http://192.0.2.5:8000"},
        {"label": "LAN 2", "url": "http://10.0.0.5"},
    ]


def test_install_base_urls_drops_localhost_and_duplicates_but_never_ends_empty():
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "http://100.100.1.1, http://100.100.1.1/"), \
         patch.object(settings, "mc_base_url", "http://localhost:8000"), \
         patch.object(settings, "public_host", ""), _no_interfaces():
        assert nodes_router.install_base_urls() == [{"label": "Tailscale", "url": "http://100.100.1.1"}]

    with patch.object(settings, "mc_node_agent_base_url", ""), \
         patch.object(settings, "mc_base_url", "http://localhost"), \
         patch.object(settings, "public_host", ""), _no_interfaces():
        # Nichts Brauchbares konfiguriert → die bisherige eine Adresse, nie leer.
        assert nodes_router.install_base_urls() == [{"label": "Adresse", "url": "http://localhost"}]


def test_install_base_urls_never_downgrades_a_configured_https_address_via_public_host():
    """Marks Fall: https-only über Tailscale-Cert, PUBLIC_HOST = derselbe Name.
    Eine zweite Zeile mit http:// wäre auf so einer Instanz tot."""
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "https://mc.tailnet-name.ts.net"), \
         patch.object(settings, "mc_base_url", "http://localhost"), \
         patch.object(settings, "public_host", "mc.tailnet-name.ts.net"), _no_interfaces():
        entries = nodes_router.install_base_urls()
    assert entries == [{"label": "Tailscale", "url": "https://mc.tailnet-name.ts.net"}]


def test_install_base_urls_adds_interface_addresses_with_scheme_and_port_of_the_reference():
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "https://mc.tailnet-name.ts.net:8443"), \
         patch.object(settings, "mc_base_url", "http://localhost"), \
         patch.object(settings, "public_host", ""), \
         patch.object(nodes_router, "_interface_addresses", lambda: ["192.0.2.9"]):
        entries = nodes_router.install_base_urls()
    assert entries == [
        {"label": "Tailscale", "url": "https://mc.tailnet-name.ts.net:8443"},
        {"label": "LAN", "url": "https://192.0.2.9:8443"},
    ]


def test_interface_addresses_are_empty_inside_a_container():
    with patch.object(nodes_router, "_running_in_container", lambda: True):
        assert nodes_router._interface_addresses() == []  # noqa: SLF001


@pytest.mark.asyncio
async def test_pairing_code_response_has_one_command_per_address_and_first_is_legacy(auth_client):
    from app.config import settings

    with patch.object(settings, "mc_node_agent_base_url", "https://mc.tailnet-name.ts.net, http://192.0.2.5"), \
         patch.object(settings, "public_host", ""), _no_interfaces():
        resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    cmds = data["install_commands"]
    assert [c["label"] for c in cmds] == ["Tailscale", "LAN"]
    assert cmds[0]["url"] == "https://mc.tailnet-name.ts.net"
    assert cmds[1]["url"] == "http://192.0.2.5"
    for c in cmds:
        assert data["code"] in c["cmd"]
        assert f"{c['url']}/api/v1/nodes/agent-script" in c["cmd"]
        assert f"--mc-url {c['url']} " in c["cmd"]
    assert data["install_command"] == cmds[0]["cmd"]


def test_node_agent_base_urls_single_value_behaves_as_before():
    from app.config import node_agent_base_url, node_agent_base_urls, settings

    with patch.object(settings, "mc_node_agent_base_url", "https://one.example"):
        assert node_agent_base_urls() == ["https://one.example"]
        assert node_agent_base_url() == "https://one.example"
    with patch.object(settings, "mc_node_agent_base_url", ""), patch.object(settings, "mc_base_url", "http://fallback"):
        assert node_agent_base_urls() == []
        assert node_agent_base_url() == "http://fallback"
    # Review 03.09.: gesetzt, aber leer ("  " / ",") → kein IndexError, Fallback.
    for raw in ("  ", ",", " , "):
        with patch.object(settings, "mc_node_agent_base_url", raw), patch.object(settings, "mc_base_url", "http://fallback"), \
             patch.object(settings, "public_host", ""), _no_interfaces():
            assert node_agent_base_urls() == []
            assert node_agent_base_url() == "http://fallback"
            assert nodes_router.install_base_urls() == [{"label": "Adresse", "url": "http://fallback"}]


# ── 7. Agent-Skript aus dem Verzeichnis ──────────────────────────────────────


def test_read_agent_script_reads_from_a_directory_and_sees_a_replaced_file(tmp_path):
    script_dir = tmp_path / "node-agent"
    script_dir.mkdir()
    target = script_dir / "mc-node-agent.py"
    target.write_text("print('v1')\n", encoding="utf-8")
    assert nodes_router.read_agent_script_or_none(script_dir) == "print('v1')\n"

    # Deploy-Fall: `git checkout` ersetzt die Datei (neuer Inode) — der
    # Verzeichnis-Mount sieht die neue Datei, der Einzeldatei-Mount sah 404.
    target.unlink()
    target.write_text("print('v2')\n", encoding="utf-8")
    assert nodes_router.read_agent_script_or_none(script_dir) == "print('v2')\n"

    # Ohne Mount (Verzeichnis fehlt / Datei fehlt) → None, keine Exception.
    assert nodes_router.read_agent_script_or_none(tmp_path / "missing") is None
    target.unlink()
    assert nodes_router.read_agent_script_or_none(script_dir) is None


def test_default_script_dir_matches_compose_mount_and_file_exists_in_repo():
    repo = Path(__file__).resolve().parents[2]
    compose = (repo / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./scripts/node-agent:/app/scripts/node-agent:ro" in compose
    assert "./scripts/mc-node-agent.py:/app/scripts/mc-node-agent.py:ro" not in compose
    assert nodes_router._AGENT_SCRIPT_DIR == Path("/app/scripts/node-agent")  # noqa: SLF001
    assert (repo / "scripts" / "node-agent" / nodes_router._AGENT_SCRIPT_NAME).is_file()  # noqa: SLF001
    assert not (repo / "scripts" / "mc-node-agent.py").exists()
    # Das Sync-Skript der Steuer-Dateien zeigt auf den neuen Ort.
    sync = (repo / "scripts" / "device" / "sync-into-agent.py").read_text(encoding="utf-8")
    assert '"node-agent" / "mc-node-agent.py"' in sync


# ── Ergänzung (Chef-Entscheid 02.09.): Rolle auf jedem Anlege-Weg ────────────


@pytest.mark.asyncio
async def test_pairing_code_carries_role_and_ssh_host_onto_the_new_host(client, auth_client, session):
    import uuid

    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={
        "role": "worker", "ssh_host": "192.0.2.30",
    })).json()
    resp = await client.post("/api/v1/nodes/pair", json={"code": minted["code"], "hostname": "box-a"})
    assert resp.status_code == 200, resp.text
    host = await session.get(Host, uuid.UUID(resp.json()["host_id"]))
    assert host.role == "worker" and host.ssh_host == "192.0.2.30"

    # Nur der Code (Betreiber) liefert die Adresse; die Geräte-Meldung zählt nicht.
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={"ssh_host": "192.0.2.31"})).json()
    resp = await client.post("/api/v1/nodes/pair", json={
        "code": minted["code"], "hostname": "box-b", "ssh_host": "192.0.2.99",
    })
    host = await session.get(Host, uuid.UUID(resp.json()["host_id"]))
    assert host.ssh_host == "192.0.2.31" and host.role is None

    # Vorhandener Host mit eigener Rolle: der Code überschreibt sie NICHT.
    box_c = await _host(session, "box-c", kind="agent", ssh_host=None, role="head")
    minted = (await auth_client.post("/api/v1/nodes/pairing-codes", json={
        "host_id": str(box_c.id), "role": "worker", "ssh_host": "192.0.2.32",
    })).json()
    resp = await client.post("/api/v1/nodes/pair", json={"code": minted["code"], "hostname": "x"})
    assert resp.status_code == 200, resp.text
    await session.refresh(box_c)
    assert box_c.role == "head" and box_c.ssh_host == "192.0.2.32"


@pytest.mark.asyncio
async def test_pairing_code_rejects_a_bad_role_and_ignores_unknown_fields(auth_client):
    resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={"role": "master"})
    assert resp.status_code == 422 and "'head' oder 'worker'" in resp.text
    # Unbekannte Schlüssel sind kein Fehler (kein extra="forbid") — ein
    # älteres/neueres Frontend darf mehr schicken, als das Backend kennt.
    resp = await auth_client.post("/api/v1/nodes/pairing-codes", json={"role": "HEAD", "future_field": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["install_commands"]


@pytest.mark.asyncio
async def test_host_create_and_patch_ignore_unknown_fields_and_patch_keeps_role(auth_client, session):
    resp = await auth_client.post("/api/v1/hosts", json={
        "slug": "box-a", "display_name": "A", "kind": "agent", "role": "head", "future_field": True,
    })
    assert resp.status_code == 200, resp.text
    host_id = resp.json()["id"]
    # PATCH ohne role/fabric_ip lässt beide unangetastet (exclude_unset).
    resp = await auth_client.patch(f"/api/v1/hosts/{host_id}", json={"display_name": "A2", "future_field": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "head" and resp.json()["display_name"] == "A2"


@pytest.mark.asyncio
async def test_onboard_endpoint_accepts_role_and_rejects_a_bad_one(auth_client):
    with patch("app.services.host_onboarding.start_onboarding", AsyncMock(return_value="job-1")) as start:
        resp = await auth_client.post("/api/v1/hosts/onboard", json={
            "address": "192.0.2.50", "username": "u", "auth": {"password": "p"}, "role": "Worker",
        })
    assert resp.status_code == 202, resp.text
    assert start.await_args.args[0].role == "worker"
    resp = await auth_client.post("/api/v1/hosts/onboard", json={
        "address": "192.0.2.50", "username": "u", "auth": {"password": "p"}, "role": "boss",
    })
    assert resp.status_code == 422 and "'head' oder 'worker'" in resp.text


@pytest.mark.asyncio
async def test_recipe_install_accepts_agent_box_with_ssh_and_refuses_without(auth_client, session):
    """Rezept-Install (routers/local_registry) — dieselbe ssh_capable-Regel."""
    from app.services import recipe_install

    with_ssh = await _host(session, "box-a", kind="agent", ssh_host="192.0.2.10")
    without = await _host(session, "box-b", kind="agent", ssh_host=None)
    await _recipe(session, "recipe-x", install_template="echo install {slug} {port}")
    started = []

    async def fake_start(host_id, slug, resolved, **kwargs):
        started.append((host_id, resolved.ssh_host))

    with patch.object(recipe_install, "start_install", fake_start):
        ok = await auth_client.post("/api/v1/local-registry/recipe-x/install", json={"host_id": str(with_ssh.id)})
        refused = await auth_client.post("/api/v1/local-registry/recipe-x/install", json={"host_id": str(without.id)})
    assert ok.status_code == 202, ok.text
    assert started == [(str(with_ssh.id), "192.0.2.10")]
    assert refused.status_code == 409
    assert refused.json()["detail"] == "Box 'box-b' hat keinen SSH-Zugang — Installation braucht eine SSH-Adresse."
