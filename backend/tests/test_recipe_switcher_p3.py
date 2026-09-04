"""Rezept-Umschalter P3 (Entwurf 04.09.2026) — Zweibox-Start und Autostart je Box.

Was hier abgesichert wird:
  * Migration 0193 (Quelltext-Ebene wie 0191/0192) + Modell-Ebene.
  * ``services/recipe_env``: Platzhalter rendern (inkl. Fallback fabric→ssh),
    unbekannter Platzhalter = Satz, `.env` idempotent schreiben, Backup nur
    einmal, Rücklesen als Wirk-Beweis (Abweichung → 502).
  * Duo-Start: Worker-Wahl (genannt / erster Kandidat / keiner),
    .env-Werte, ``runtime_hosts`` mit zwei Zeilen, Verdrängung erst Worker
    dann Head, 409/422-Fälle, ``autostart_recipe_slug`` am Head.
  * Solo auf einer Box, die als zweite Box eines laufenden Verbunds arbeitet: 409.
  * Autostart-API: GET/PUT, 422 bei unbekanntem Rezept, Hosts-Liste.
  * Wächter: aus → kein Start, an + passendes Rezept → Start, an + fremdes
    Rezept → kein Start, Duo → über den Umschalter.

Testdaten heissen box-a / box-b / recipe-x. Kein Netz, keine Gerätedaten.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.redis_client import RedisKeys
from app.services import recipe_env, recipe_switcher
from app.services.agent_runtime_switch import ProbedModel
from app.services.host_resolver import ResolvedHost
from app.services.runtime_watcher import UNREACHABLE_EVENT_THRESHOLD, RuntimeWatcher

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"
ENV_FILE = "~/rezept/.env"
ENV_MAP = {"HEAD_IP": "{head_fabric_ip}", "WORKER_IP": "{worker_fabric_ip}"}


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(
    session: AsyncSession, slug: str, *, kind: str = "ssh", ssh_host: str | None = "192.0.2.10", **kw
) -> Host:
    host = Host(slug=slug, display_name=slug.upper(), kind=kind, ssh_host=ssh_host, **kw)
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


async def _viewer_headers() -> dict[str, str]:
    """Ein Nur-Lesen-Konto — dieselbe Machart wie in test_recipe_switcher.py."""
    import uuid as _uuid

    from app.auth import create_access_token
    from app.models.user import User
    from tests.conftest import test_engine

    uid = _uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer",
                   role="viewer", is_active=True))
        await s.commit()
    return {"Authorization": f"Bearer {create_access_token(str(uid), 'viewer')}"}


class _FakeBox:
    """Eine Box, die auf SSH antwortet — merkt sich, was geschrieben wurde,
    und liefert beim ``cat`` die Datei zurück, die daraus entstanden ist.

    Damit ist der `.env`-Pfad ohne Netz prüfbar: das Skript wird nicht
    ausgeführt, aber sein VERSPRECHEN (Zeilen ersetzen/anhängen) wird
    nachgebildet und die Rücklese-Prüfung läuft echt.
    """

    def __init__(self, initial: str = "") -> None:
        self.content = initial
        self.commands: list[str] = []
        self.backups = 0

    async def __call__(self, command: str, **kwargs) -> tuple[str, str, int]:
        self.commands.append(command)
        if command.strip() == "true":
            return "", "", 0
        if command.startswith("cat "):
            return self.content, "", 0
        if 'bak-mc' in command:
            self.backups += 1
        values: dict[str, str] = {}
        for line in command.splitlines():
            if "MC_K=" in line and "MC_V=" in line:
                key = line.split("MC_K=", 1)[1].split(" ", 1)[0].strip("'")
                value = line.split("MC_V=", 1)[1].split(" ", 1)[0].strip("'")
                values[key] = value
        parsed = recipe_env.parse_env_text(self.content)
        lines = self.content.splitlines()
        for key, value in values.items():
            if key in parsed:
                lines = [
                    (f"{key}={value}" if line.strip().startswith(f"{key}=") else line)
                    for line in lines
                ]
            else:
                lines.append(f"{key}={value}")
        self.content = "\n".join(lines) + "\n"
        return "", "", 0


# ── 1. Migration 0193 ────────────────────────────────────────────────────────


def test_migration_0193_adds_six_columns_and_takes_them_back():
    source = (MIGRATIONS / "0193_p3_duo_autostart.py").read_text(encoding="utf-8")
    assert 'op.add_column("local_recipes", sa.Column("env_file", sa.Text(), nullable=True))' in source
    assert 'op.add_column("local_recipes", sa.Column("env_map", sa.JSON(), nullable=True))' in source
    assert '"autostart_enabled"' in source
    assert 'server_default=sa.text("false")' in source
    assert 'op.add_column("hosts", sa.Column("autostart_recipe_slug", sa.Text(), nullable=True))' in source
    assert '"autostart_last_attempt_at"' in source
    assert 'op.add_column("hosts", sa.Column("autostart_last_result", sa.Text(), nullable=True))' in source
    for column in (
        "env_file",
        "env_map",
        "autostart_enabled",
        "autostart_recipe_slug",
        "autostart_last_attempt_at",
        "autostart_last_result",
    ):
        assert f'op.drop_column("hosts", "{column}")' in source or (
            f'op.drop_column("local_recipes", "{column}")' in source
        )
    assert 'down_revision = "0192_host_role_recipe_exclusive"' in source
    # Keine Datenzeilen (Regel 7 ADR-077).
    for verb in ("INSERT", "UPDATE", "op.execute", "op.bulk_insert"):
        assert verb not in source, verb


@pytest.mark.asyncio
async def test_model_round_trips_the_new_columns(session):
    session.add(
        LocalRecipe(
            slug="recipe-x",
            display_name="X",
            engine="vllm_docker",
            model_identifier="m",
            env_file=ENV_FILE,
            env_map={"HEAD_IP": "{head_ip}"},
        )
    )
    session.add(Host(slug="box-a", display_name="A", kind="ssh"))
    await session.commit()

    recipe = (await session.exec(select(LocalRecipe))).first()
    assert recipe.env_file == ENV_FILE
    assert recipe.env_map == {"HEAD_IP": "{head_ip}"}
    host = (await session.exec(select(Host))).first()
    # Standard ist AUS — MC startet ohne Zutun des Betreibers nichts von selbst.
    assert host.autostart_enabled is False
    assert host.autostart_recipe_slug is None
    assert host.autostart_last_attempt_at is None
    assert host.autostart_last_result is None


# ── 2. recipe_env: rendern ───────────────────────────────────────────────────


def test_render_env_map_fills_every_placeholder():
    head = Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10",
                ssh_user="op", fabric_ip="10.0.0.1")
    worker = Host(slug="box-b", display_name="B", kind="ssh", ssh_host="192.0.2.11",
                  ssh_user="op", fabric_ip="10.0.0.2")
    out = recipe_env.render_env_map(
        {
            "HEAD_IP": "{head_ip}",
            "WORKER_IP": "{worker_ip}",
            "HEAD_FABRIC": "{head_fabric_ip}",
            "WORKER_FABRIC": "{worker_fabric_ip}",
            "HEAD_SSH": "{head_ssh}",
            "WORKER_SSH": "{worker_ssh}",
            "MIXED": "tcp://{worker_fabric_ip}:29500",
        },
        head,
        worker,
    )
    assert out == {
        "HEAD_IP": "192.0.2.10",
        "WORKER_IP": "192.0.2.11",
        "HEAD_FABRIC": "10.0.0.1",
        "WORKER_FABRIC": "10.0.0.2",
        "HEAD_SSH": "op@192.0.2.10",
        "WORKER_SSH": "op@192.0.2.11",
        "MIXED": "tcp://10.0.0.2:29500",
    }


def test_fabric_ip_falls_back_to_the_ssh_address():
    """NULL heisst „nimm ssh_host" (Migration 0192) — eine Box ohne
    Verbund-Kabel bekommt keine erfundene Adresse, sondern die, die es gibt."""
    head = Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10")
    worker = Host(slug="box-b", display_name="B", kind="ssh", ssh_host="192.0.2.11")
    out = recipe_env.render_env_map(
        {"H": "{head_fabric_ip}", "W": "{worker_fabric_ip}"}, head, worker
    )
    assert out == {"H": "192.0.2.10", "W": "192.0.2.11"}


def test_unknown_placeholder_is_a_sentence_not_a_crash():
    head = Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10")
    with pytest.raises(ValueError) as exc:
        recipe_env.render_env_map({"X": "{gpu_count}"}, head, None)
    assert "{gpu_count}" in str(exc.value)
    assert "{head_ip}" in str(exc.value)  # sagt, was erlaubt WÄRE


def test_a_worker_placeholder_without_a_worker_is_refused():
    head = Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10")
    with pytest.raises(ValueError):
        recipe_env.render_env_map({"W": "{worker_ip}"}, head, None)


def test_a_bad_env_key_is_refused():
    head = Host(slug="box-a", display_name="A", kind="ssh", ssh_host="192.0.2.10")
    with pytest.raises(ValueError):
        recipe_env.render_env_map({"2BAD KEY": "{head_ip}"}, head, None)


def test_tilde_survives_quoting_but_the_rest_is_quoted():
    """``~`` gehört der Shell der BOX. Wer ihn mitquotet, sucht ein Zuhause
    namens „~" — und schreibt die Datei woanders hin."""
    assert recipe_env.quote_remote_path("~/rezept/.env") == "~/rezept/.env"
    assert recipe_env.quote_remote_path("~/mein ordner/.env") == "~/'mein ordner/.env'"
    assert recipe_env.quote_remote_path("/opt/x/.env") == "/opt/x/.env"
    assert "'" in recipe_env.quote_remote_path("/opt/a b/.env")


def test_special_characters_never_reach_the_awk_program():
    """Werte gehen über die Umgebung (``ENVIRON``) in awk, nicht in den
    Programmtext — sonst löste awk Backslash-Folgen ein zweites Mal auf."""
    command = recipe_env._upsert_command("~/x/.env", {"K": "a b';rm -rf /"})
    assert "rm -rf /" not in command.replace("'a b'\"'\"';rm -rf /'", "")
    assert 'ENVIRON["MC_V"]' in command


# ── 3. recipe_env: schreiben ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_replaces_one_line_appends_the_other_and_keeps_the_rest():
    box = _FakeBox("# meins\nHEAD_IP=1.1.1.1\nANDERES=bleibt\n")
    with patch("app.services.runtime_manager._ssh_run", box):
        written = await recipe_env.upsert_env_file(
            None, ENV_FILE, {"HEAD_IP": "10.0.0.1", "WORKER_IP": "10.0.0.2"}
        )
    assert written == ["HEAD_IP", "WORKER_IP"]
    parsed = recipe_env.parse_env_text(box.content)
    assert parsed["HEAD_IP"] == "10.0.0.1"
    assert parsed["WORKER_IP"] == "10.0.0.2"
    assert parsed["ANDERES"] == "bleibt"
    assert "# meins" in box.content


@pytest.mark.asyncio
async def test_upsert_is_idempotent_and_backs_up_only_once():
    box = _FakeBox("HEAD_IP=1.1.1.1\n")
    with patch("app.services.runtime_manager._ssh_run", box):
        await recipe_env.upsert_env_file(None, ENV_FILE, {"HEAD_IP": "10.0.0.1"})
        first = box.content
        await recipe_env.upsert_env_file(None, ENV_FILE, {"HEAD_IP": "10.0.0.1"})
    assert box.content == first
    # Das Backup-Kommando steht in jedem Skript, legt aber nur an, wenn keins da
    # ist ([ -f … ] || cp) — der Test prüft die Bedingung, nicht die Zahl.
    assert all('[ -f "$f.bak-mc" ] || cp' in c for c in box.commands if "bak-mc" in c)


@pytest.mark.asyncio
async def test_a_file_that_reads_back_differently_is_a_502():
    """Wirk-Beweis: „Befehl lief durch" zählt nicht. Steht danach etwas
    anderes da, bricht der Start ab, statt mit falschen Adressen zu starten."""

    async def _lying(command: str, **kwargs):
        if command.startswith("cat "):
            return "HEAD_IP=falsch\n", "", 0
        return "", "", 0

    with patch("app.services.runtime_manager._ssh_run", _lying):
        with pytest.raises(recipe_switcher.RecipeStartError) as exc:
            await recipe_env.upsert_env_file(None, ENV_FILE, {"HEAD_IP": "10.0.0.1"})
    assert exc.value.status == 502
    assert "HEAD_IP" in exc.value.detail


@pytest.mark.asyncio
async def test_an_unreachable_box_is_a_502_with_a_sentence():
    with patch("app.services.runtime_manager._ssh_run", AsyncMock(side_effect=OSError("weg"))):
        with pytest.raises(recipe_switcher.RecipeStartError) as exc:
            await recipe_env.upsert_env_file(None, ENV_FILE, {"HEAD_IP": "10.0.0.1"})
    assert exc.value.status == 502


def test_the_generated_script_really_works_in_a_posix_shell(tmp_path):
    """WIRK-BEWEIS: das Skript wird hier WIRKLICH ausgeführt (/bin/sh), nicht
    nachgebildet. Es prüft genau das, was auf der Box passieren muss —
    ersetzen, anhängen, Rest unangetastet, Backup einmalig, Sonderzeichen
    heil, zweiter Lauf ändert nichts."""
    import subprocess

    target = tmp_path / ".env"
    original = "# meins\nHEAD_IP=1.1.1.1\nANDERES=bleibt\n"
    target.write_text(original, encoding="utf-8")
    tricky = "tcp://a b';echo kaputt#\\n"
    command = recipe_env._upsert_command(
        str(target), {"HEAD_IP": "10.0.0.1", "WORKER_IP": tricky}
    )

    subprocess.run(["/bin/sh", "-c", command], check=True)

    parsed = recipe_env.parse_env_text(target.read_text(encoding="utf-8"))
    assert parsed["HEAD_IP"] == "10.0.0.1"
    assert parsed["WORKER_IP"] == tricky
    assert parsed["ANDERES"] == "bleibt"
    assert "# meins" in target.read_text(encoding="utf-8")
    assert (tmp_path / ".env.bak-mc").read_text(encoding="utf-8") == original
    assert not (tmp_path / ".env.mc-tmp").exists()

    after_first = target.read_text(encoding="utf-8")
    subprocess.run(["/bin/sh", "-c", command], check=True)
    assert target.read_text(encoding="utf-8") == after_first
    # Das Backup ist immer noch das ORIGINAL, nicht die geschriebene Fassung.
    assert (tmp_path / ".env.bak-mc").read_text(encoding="utf-8") == original


def test_the_script_refuses_a_folder_that_does_not_exist(tmp_path):
    """MC legt keine Rezept-Ordner an — ein Tippfehler im Katalog soll
    auffallen, statt eine `.env` im Nirgendwo zu erzeugen."""
    import subprocess

    command = recipe_env._upsert_command(str(tmp_path / "gibt-es-nicht" / ".env"), {"K": "v"})
    done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    assert done.returncode != 0


# ── 4. Liste: env_ready ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_duo_without_env_map_is_grey_with_a_sentence(auth_client, session):
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-duo", topology={"nodes": 2})
    await _duo_recipe(session, "recipe-duo-ok")

    with _probe(set()):
        body = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())

    assert body["recipe-duo"]["env_ready"] is False
    assert body["recipe-duo"]["startable"] is False
    assert body["recipe-duo"]["reason"] == recipe_switcher.REASON_NO_ENV_MAP
    assert body["recipe-duo-ok"]["env_ready"] is True
    assert body["recipe-duo-ok"]["startable"] is True


@pytest.mark.asyncio
async def test_solo_recipes_are_always_env_ready(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-x"]
    assert entry["env_ready"] is True


@pytest.mark.asyncio
async def test_a_box_without_ssh_is_no_worker_candidate(auth_client, session):
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", kind="agent", ssh_host=None)
    await _duo_recipe(session)
    with _probe(set()):
        entry = _by_slug((await auth_client.get(f"/api/v1/hosts/{box_a.id}/recipes")).json())["recipe-duo"]
    assert entry["candidate_workers"] == []
    assert entry["startable"] is False
    assert entry["reason"] == recipe_switcher.REASON_NO_SECOND_BOX


# ── 5. Duo-Start ─────────────────────────────────────────────────────────────


async def _start_duo(auth_client, box_a, *, slug="recipe-duo", body=None, box=None):
    """Startet ein Duo mit gemocktem SSH und gemocktem start_runtime."""
    box = box or _FakeBox()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", box),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box_a.id}/recipes/{slug}/start", json=body
        )
    return resp, start, box


@pytest.mark.asyncio
async def test_duo_start_writes_the_env_members_and_the_autostart_slug(auth_client, session):
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2", role="worker")
    await _duo_recipe(session)

    resp, start, box = await _start_duo(auth_client, box_a)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["worker_host_id"] == str(box_b.id)
    assert body["worker_slug"] == "box-b"
    assert body["env_written"] == ["HEAD_IP", "WORKER_IP"]
    start.assert_awaited_once()

    # Die .env trägt die Verbund-Adressen, nicht die SSH-Adressen.
    parsed = recipe_env.parse_env_text(box.content)
    assert parsed == {"HEAD_IP": "10.0.0.1", "WORKER_IP": "10.0.0.2"}

    # Zwei Mitgliedschaften: Head rank 0, Worker rank 1.
    members = (await session.exec(select(RuntimeHost))).all()
    assert sorted((m.role, m.node_rank) for m in members) == [("head", 0), ("worker", 1)]
    assert {m.host_id for m in members} == {box_a.id, box_b.id}

    runtime = (await session.exec(select(Runtime))).one()
    assert runtime.topology["worker_host_id"] == str(box_b.id)
    assert runtime.topology["nodes"] == 2
    assert runtime.host_id == box_a.id

    await session.refresh(box_a)
    assert box_a.autostart_recipe_slug == "recipe-duo"


@pytest.mark.asyncio
async def test_starting_twice_keeps_exactly_two_membership_rows(auth_client, session):
    """Der Schreibpfad ist idempotent — sonst liefe der zweite Start in die
    Unique-Regel (runtime_id, node_rank)."""
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)

    await _start_duo(auth_client, box_a)
    resp, _, _ = await _start_duo(auth_client, box_a)

    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is False
    assert len((await session.exec(select(RuntimeHost))).all()) == 2
    assert len((await session.exec(select(Runtime))).all()) == 1


@pytest.mark.asyncio
async def test_the_named_worker_wins_over_the_default_order(auth_client, session):
    box_a = await _host(session, "box-a")
    # box-b hätte die Worker-Rolle und käme zuerst — der Betreiber will box-c.
    await _host(session, "box-b", ssh_host="192.0.2.11", role="worker", ui_order=1)
    box_c = await _host(session, "box-c", ssh_host="192.0.2.12", ui_order=2)
    await _duo_recipe(session)

    resp, _, _ = await _start_duo(auth_client, box_a, body={"worker_host_id": str(box_c.id)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["worker_slug"] == "box-c"


@pytest.mark.asyncio
async def test_a_busy_named_worker_is_409_with_a_sentence(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)
    await _runtime(session, "other-on-b", box_b, endpoint="http://192.0.2.11:8000/v1")

    start = AsyncMock()
    with (
        _probe({"other-on-b"}),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(
            f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start",
            json={"worker_host_id": str(box_b.id)},
        )
    assert resp.status_code == 409
    assert "box-b" in resp.json()["detail"]
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime))).all() != []  # nur die fremde Instanz
    assert (await session.exec(select(RuntimeHost))).all() == []


@pytest.mark.asyncio
async def test_no_free_worker_at_all_is_409(auth_client, session):
    box_a = await _host(session, "box-a")
    await _duo_recipe(session)
    start = AsyncMock()
    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")
    assert resp.status_code == 409
    assert resp.json()["detail"] == recipe_switcher.REASON_NO_FREE_WORKER
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime))).all() == []


@pytest.mark.asyncio
async def test_duo_start_without_env_map_is_422_and_creates_nothing(auth_client, session):
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _recipe(session, "recipe-duo", topology={"nodes": 2}, env_file=ENV_FILE)
    start = AsyncMock()
    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")
    assert resp.status_code == 422
    assert resp.json()["detail"] == recipe_switcher.REASON_NO_ENV_MAP
    start.assert_not_awaited()
    assert (await session.exec(select(Runtime))).all() == []


@pytest.mark.asyncio
async def test_an_unreachable_worker_stops_the_start_before_anything_is_freed(auth_client, session):
    """Erreichbarkeit VOR der Verdrängung — sonst wäre das laufende Modell tot
    und der Verbund nie gestartet (Vorfall-Muster 03.09.2026)."""
    box_a = await _host(session, "box-a")
    await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)

    start = AsyncMock()
    exclusive = AsyncMock(return_value={"ok": True, "stopped": [], "message": ""})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", AsyncMock(side_effect=OSError("weg"))),
        patch("app.services.runtime_manager.ensure_exclusive_host", exclusive),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")

    assert resp.status_code == 502
    assert "box-a" in resp.json()["detail"]
    exclusive.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_worker_is_freed_before_the_head(auth_client, session):
    """Reihenfolge: erst die zweite Box, dann der Head (den räumt
    ``start_runtime`` selbst). Andersherum stünde der Head leer, während die
    Worker-Box noch das alte Modell hält."""
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    await _duo_recipe(session)

    order: list[str] = []
    box = _FakeBox()

    async def _ensure(runtime, *, host=None, session=None, host_id=None):
        order.append(f"exclusive:{host_id}")
        return {"ok": True, "stopped": [], "message": "frei"}

    async def _start(runtime, **kwargs):
        order.append("start")
        return {"ok": True, "message": "läuft an"}

    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", box),
        patch("app.services.runtime_manager.ensure_exclusive_host", _ensure),
        patch("app.services.runtime_manager.start_runtime", _start),
    ):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-duo/start")

    assert resp.status_code == 200, resp.text
    assert order == [f"exclusive:{box_b.id}", "start"]


@pytest.mark.asyncio
async def test_ensure_exclusive_host_sees_a_verbund_member_on_the_target_box(session):
    """Die Verdrängung muss ``runtime_hosts`` kennen: eine Box wird auch von
    einem Verbund belegt, dessen Head woanders steht."""
    from app.services import runtime_manager

    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    verbund = await _runtime(session, "verbund-a", box_a)
    session.add(RuntimeHost(runtime_id=verbund.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()

    stopped: list[str] = []

    async def _state(runtime, **kwargs):
        return {"state": "running"}

    async def _evict(slug, **kwargs):
        stopped.append(slug)
        return {"ok": True, "message": "gestoppt"}

    with (
        patch("app.services.runtime_manager.get_runtime_state", _state),
        patch("app.services.runtime_manager.evict_spark_runtime_containers", _evict),
    ):
        result = await runtime_manager.ensure_exclusive_host(
            {"slug": "neu", "exclusive_memory": True, "runtime_type": "vllm_docker"},
            session=session,
            host_id=box_b.id,
        )

    assert result["ok"] is True
    assert stopped == ["verbund-a"]


# ── 6. Solo auf einer Verbund-Box ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solo_on_a_box_that_serves_as_a_worker_is_409(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    verbund = await _runtime(session, "verbund-a", box_a, display_name="Verbund A")
    session.add(RuntimeHost(runtime_id=verbund.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()
    await _recipe(session, "recipe-solo")

    start = AsyncMock()
    with _probe({"verbund-a"}), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_b.id}/recipes/recipe-solo/start")

    assert resp.status_code == 409
    assert "Verbund A" in resp.json()["detail"]
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stopped_verbund_does_not_block_the_worker_box(auth_client, session):
    """Belegt ist nur, was LÄUFT — eine tote Verbund-Zeile sperrt nichts."""
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    verbund = await _runtime(session, "verbund-a", box_a)
    session.add(RuntimeHost(runtime_id=verbund.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()
    await _recipe(session, "recipe-solo")

    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_b.id}/recipes/recipe-solo/start")

    assert resp.status_code == 200, resp.text
    start.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_solo_start_also_remembers_the_recipe_on_the_box(auth_client, session):
    box_a = await _host(session, "box-a")
    await _recipe(session, "recipe-x")
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with _probe(set()), patch("app.services.runtime_manager.start_runtime", start):
        resp = await auth_client.post(f"/api/v1/hosts/{box_a.id}/recipes/recipe-x/start")
    assert resp.status_code == 200
    await session.refresh(box_a)
    assert box_a.autostart_recipe_slug == "recipe-x"
    # Der Schalter selbst bleibt aus: gemerkt ≠ eingeschaltet.
    assert box_a.autostart_enabled is False


# ── 7. Autostart-API ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_autostart_get_and_put_round_trip(auth_client, session):
    box_a = await _host(session, "box-a", role="head")
    await _recipe(session, "recipe-x")

    got = await auth_client.get(f"/api/v1/hosts/{box_a.id}/autostart")
    assert got.status_code == 200
    assert got.json()["enabled"] is False
    assert got.json()["recipe_slug"] is None
    assert got.json()["role"] == "head"
    assert got.json()["via_head"] is None

    put = await auth_client.put(
        f"/api/v1/hosts/{box_a.id}/autostart", json={"enabled": True, "recipe_slug": "recipe-x"}
    )
    assert put.status_code == 200, put.text
    assert put.json()["enabled"] is True
    assert put.json()["recipe_slug"] == "recipe-x"
    assert put.json()["recipe_display_name"] == "recipe-x"

    await session.refresh(box_a)
    assert box_a.autostart_enabled is True

    off = await auth_client.put(f"/api/v1/hosts/{box_a.id}/autostart", json={"enabled": False})
    assert off.json()["enabled"] is False
    # Das Rezept bleibt gemerkt, auch wenn der Schalter aus ist.
    assert off.json()["recipe_slug"] == "recipe-x"


@pytest.mark.asyncio
async def test_autostart_with_an_unknown_recipe_is_422(auth_client, session):
    box_a = await _host(session, "box-a")
    resp = await auth_client.put(
        f"/api/v1/hosts/{box_a.id}/autostart", json={"enabled": True, "recipe_slug": "gibt-es-nicht"}
    )
    assert resp.status_code == 422
    assert "gibt-es-nicht" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_switching_on_without_any_recipe_is_refused_with_a_sentence(auth_client, session):
    box_a = await _host(session, "box-a")
    resp = await auth_client.put(f"/api/v1/hosts/{box_a.id}/autostart", json={"enabled": True})
    assert resp.status_code == 422
    assert "Rezept" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_worker_box_is_told_it_runs_through_its_head(auth_client, session):
    box_a = await _host(session, "box-a")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11")
    verbund = await _runtime(session, "verbund-a", box_a)
    session.add(RuntimeHost(runtime_id=verbund.id, host_id=box_b.id, role="worker", node_rank=1))
    await session.commit()

    view = (await auth_client.get(f"/api/v1/hosts/{box_b.id}/autostart")).json()
    assert view["via_head"] == {"host_id": str(box_a.id), "slug": "box-a"}


@pytest.mark.asyncio
async def test_hosts_list_carries_the_two_autostart_fields(auth_client, session):
    await _host(session, "box-a", autostart_enabled=True, autostart_recipe_slug="recipe-x")
    row = (await auth_client.get("/api/v1/hosts")).json()[0]
    assert row["autostart_enabled"] is True
    assert row["autostart_recipe_slug"] == "recipe-x"


@pytest.mark.asyncio
async def test_autostart_put_is_admin_only(auth_client, session):
    """Lesen darf jeder, schalten nur ein Admin — dieselbe Regel wie beim
    Rezept-Start: hier wird entschieden, ob eine Box Befehle bekommt."""
    box_a = await _host(session, "box-a")
    headers = await _viewer_headers()
    assert (await auth_client.get(f"/api/v1/hosts/{box_a.id}/autostart", headers=headers)).status_code == 200
    resp = await auth_client.put(
        f"/api/v1/hosts/{box_a.id}/autostart", json={"enabled": False}, headers=headers
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_autostart_on_an_unknown_box_is_404(auth_client):
    assert (await auth_client.get("/api/v1/hosts/box-z/autostart")).status_code == 404


# ── 8. Wächter ───────────────────────────────────────────────────────────────


def _fake_get_redis(fake_redis):
    async def _get():
        return fake_redis

    return _get


async def _run_watcher(session, fake_redis, *, start_mock, ticks=UNREACHABLE_EVENT_THRESHOLD + 1):
    watcher = RuntimeWatcher(interval=90)
    with (
        patch("app.services.runtime_watcher.probe_runtime_model_info",
              new=AsyncMock(return_value=ProbedModel(None, None))),
        patch("app.services.runtime_watcher.get_redis", _fake_get_redis(fake_redis)),
        patch("app.services.runtime_watcher.resolve_host_for_runtime",
              new=AsyncMock(return_value=ResolvedHost(ssh_host="192.0.2.10", ssh_user="op", kind="ssh"))),
        patch("app.services.runtime_manager._ssh_run", AsyncMock(return_value=("", "", 0))),
        patch("app.services.runtime_manager.start_runtime", start_mock),
    ):
        for _ in range(ticks):
            await watcher.tick(session=session)


async def _watcher_setup(session, *, enabled: bool, recipe_slug: str | None = "recipe-x"):
    recipe = await _recipe(session, "recipe-x")
    box_a = await _host(
        session, "box-a", autostart_enabled=enabled, autostart_recipe_slug=recipe_slug
    )
    rt = await _runtime(
        session, "recipe-x-box-a", box_a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 1, "recipe_slug": "recipe-x"},
    )
    return box_a, rt


@pytest.mark.asyncio
async def test_watcher_does_not_revive_when_the_switch_is_off(async_session, fake_redis):
    """MARKS AUS-SCHALTER: keine Wiederbelebung, egal wie lange die Runtime
    schon tot ist. Das ersetzt den alten runtimes.enabled=false-Trick."""
    await _watcher_setup(async_session, enabled=False)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    await _run_watcher(async_session, fake_redis, start_mock=start)
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_revives_the_hosts_own_recipe_when_the_switch_is_on(async_session, fake_redis):
    box_a, _ = await _watcher_setup(async_session, enabled=True)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    await _run_watcher(async_session, fake_redis, start_mock=start)
    start.assert_awaited_once()

    await async_session.refresh(box_a)
    assert box_a.autostart_last_attempt_at is not None
    assert box_a.autostart_last_result.startswith("Gestartet")


@pytest.mark.asyncio
async def test_watcher_never_revives_a_foreign_runtime_on_the_box(async_session, fake_redis):
    """Der Schalter gilt für EIN Rezept — nicht für alles, was auf der Box
    ausfällt."""
    await _recipe(async_session, "recipe-x")
    box_a = await _host(
        async_session, "box-a", autostart_enabled=True, autostart_recipe_slug="recipe-x"
    )
    await _runtime(
        async_session, "etwas-anderes", box_a,
        model_identifier="org/anderes",
        topology={"nodes": 1, "recipe_slug": "recipe-y"},
    )
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    await _run_watcher(async_session, fake_redis, start_mock=start)
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_starts_a_verbund_through_the_recipe_switcher(async_session, fake_redis):
    """Ein Verbund darf NICHT einfach start_runtime bekommen: seine .env muss
    vorher die Adressen tragen. Also derselbe Weg wie ein Klick."""
    recipe = await _duo_recipe(async_session)
    box_a = await _host(
        async_session, "box-a", autostart_enabled=True, autostart_recipe_slug=recipe.slug
    )
    box_b = await _host(async_session, "box-b", ssh_host="192.0.2.11")
    await _runtime(
        async_session, "recipe-duo-box-a", box_a,
        model_identifier=recipe.model_identifier,
        topology={"nodes": 2, "recipe_slug": recipe.slug, "worker_host_id": str(box_b.id)},
    )

    switcher = AsyncMock(return_value={"ok": True, "message": "Verbund startet"})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with patch("app.services.recipe_switcher.start_recipe_on_host", switcher):
        await _run_watcher(async_session, fake_redis, start_mock=start)

    switcher.assert_awaited_once()
    assert switcher.await_args.kwargs["worker_host_id"] == str(box_b.id)
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_autostart_is_written_down_as_a_sentence(async_session, fake_redis):
    box_a, _ = await _watcher_setup(async_session, enabled=True)
    start = AsyncMock(return_value={"ok": False, "message": "Box hat keinen Platz"})
    await _run_watcher(async_session, fake_redis, start_mock=start)

    await async_session.refresh(box_a)
    assert box_a.autostart_last_result.startswith("Fehlgeschlagen")
    assert "Box hat keinen Platz" in box_a.autostart_last_result


@pytest.mark.asyncio
async def test_the_global_kill_switch_still_wins(async_session, fake_redis):
    from app.config import settings

    await _watcher_setup(async_session, enabled=True)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    original = settings.runtime_auto_recovery_enabled
    settings.runtime_auto_recovery_enabled = False
    try:
        await _run_watcher(async_session, fake_redis, start_mock=start)
    finally:
        settings.runtime_auto_recovery_enabled = original
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_cooldown_claim_still_limits_it_to_one_attempt(async_session, fake_redis):
    _, rt = await _watcher_setup(async_session, enabled=True)
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    await _run_watcher(async_session, fake_redis, start_mock=start, ticks=UNREACHABLE_EVENT_THRESHOLD + 4)
    start.assert_awaited_once()
    assert await fake_redis.get(RedisKeys.runtime_recovery_cooldown(rt.slug)) is not None


# ── 9. Katalog ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_exposes_env_file_env_map_and_env_ready(auth_client, session):
    await _duo_recipe(session)
    await _recipe(session, "recipe-solo")
    body = (await auth_client.get("/api/v1/local-registry")).json()
    rows = {r["slug"]: r for r in body["recipes"]}
    assert rows["recipe-duo"]["env_file"] == ENV_FILE
    assert rows["recipe-duo"]["env_map"] == ENV_MAP
    assert rows["recipe-duo"]["env_ready"] is True
    assert rows["recipe-solo"]["env_map"] is None
    assert rows["recipe-solo"]["env_ready"] is True


def test_recipe_spec_round_trips_env_file_and_env_map_and_an_update_moves_them():
    from app.services import local_registry

    spec = local_registry.RecipeSpec(
        slug="recipe-x",
        display_name="X",
        engine="vllm_docker",
        model_identifier="org/x",
        env_file=ENV_FILE,
        env_map={"HEAD_IP": "{head_ip}"},
    )
    row = local_registry._row_from_spec(spec)
    assert row.env_file == ENV_FILE
    assert row.env_map == {"HEAD_IP": "{head_ip}"}

    moved = local_registry.RecipeSpec(
        **{**json.loads(spec.model_dump_json()), "env_map": {"HEAD_IP": "{head_fabric_ip}"}}
    )
    assert local_registry._apply_update(row, moved) is True
    assert row.env_map == {"HEAD_IP": "{head_fabric_ip}"}
    # Und die Engine-Tuning-Spalte bleibt davon unberührt (zwei Felder, ein Name
    # war die Falle: `env` = Tuning, `env_map` = Adressen).
    assert row.env is None
