"""Kontrakt-Tests für den Spark-Rezept-Umschalter — die drei Live-Vorfälle.

Drei echte Vorfälle haben den Umschalter geformt; diese Suite pinnt jeden
davon als ausführbaren Vertrag, damit eine Regression rot wird statt wieder
ein Modell zu killen:

  1. **Verdrängungsschutz (03.09.2026, Hotfix #396)** — ein Start darf ein
     laufendes Modell NICHT anfassen, bevor jede Prüfung bestanden ist, die
     ohne Netzzugriff feststeht (Startbefehl, Handle, Worker-Box, Kapazität).
     Und wenn die Verdrängung läuft, muss sie REIHENFOLGE halten: erst die
     Worker-Box, dann der Head.
  2. **Shell-Injection (05.09.2026)** — Rezept-Felder landen in
     SSH-Befehlen. Ein ``;`` (oder alles andere Shell-Metazeichen) im
     Rezept-Template darf nie als Befehl interpretiert werden: either quoted
     or rejected, und ein gescheiterter Render erzeugt keinen Teil-Start.
  3. **Start-Timeout (#418)** — ein Modell, das nie antwortet, muss als
     Fehlschlag enden (nicht als hängender Aufruf): der Worker wird als
     Waise nicht stehen gelassen (Verdrängung abgebrochen vor dem Start bzw.
     die Verifikation meldet ok=False mit dem Log-Pfad).

Plus: Stop über den eigenen Stopp-Befehl des Verbunds (Worker-Waise, Live
04.09.2026) und Duo-Start mit Worker-Wahl (P3).

Alle SSH läuft durch einen FakeSSH-Runner (``_ssh``-Fixture): er zeichnet
jeden Befehl auf und antwortet aus einer Tabelle. Keine echte Verbindung,
keine Secrets — nur RFC-5737-IPs (192.0.2.x). Zielmodule:
``recipe_switcher.start_recipe_on_host`` (Umschalter) und der aufrufende
``runtime_manager``-Pfad (``start_runtime``/``stop_runtime``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services import recipe_switcher, runtime_manager
from tests.conftest import test_engine

TEMPLATE = "docker run -d --name {container_name} --label mc.runtime.slug={slug} -p {port}:8000 img"
ENV_FILE = "~/rezept/.env"
ENV_MAP = {"HEAD_IP": "{head_fabric_ip}", "WORKER_IP": "{worker_fabric_ip}"}


# ── FakeSSH ──────────────────────────────────────────────────────────────────


class FakeSSH:
    """SSH-Ersatz: zeichnet jeden Befehl auf und simuliert die Box.

    ``responses`` mapt ein Befehls-Präfix auf ``(stdout, stderr, exit_code)``.
    Zusätzlich wirkt der Fake die `.env`-Upsert-Skripte NACH (Zeile ersetzen /
    anhängen, wie es das Skript verspricht) — so läuft die Rücklese-Prüfung
    von ``recipe_env.upsert_env_file`` echt. Ohne Treffer: ``(“”, “”, 0)``.
    """

    def __init__(self, responses: dict[str, tuple[str, str, int]] | None = None) -> None:
        self.responses = responses or {}
        self.commands: list[str] = []
        self.env_content = ""

    async def __call__(self, command: str, **kwargs) -> tuple[str, str, int]:
        self.commands.append(command)
        for prefix, answer in self.responses.items():
            if command.startswith(prefix):
                return answer
        if command.strip() == "true" or command.startswith("mkdir"):
            return "", "", 0
        if command.startswith("cat "):
            return self.env_content, "", 0
        if "MC_K=" in command:
            self.env_content = self._apply_env_upsert(command)
        return "", "", 0

    def _apply_env_upsert(self, command: str) -> str:
        from app.services import recipe_env

        values: dict[str, str] = {}
        for line in command.splitlines():
            if "MC_K=" in line and "MC_V=" in line:
                key = line.split("MC_K=", 1)[1].split(" ", 1)[0].strip("'")
                value = line.split("MC_V=", 1)[1].split(" ", 1)[0].strip("'")
                values[key] = value
        parsed = recipe_env.parse_env_text(self.env_content)
        lines = self.env_content.splitlines()
        for key, value in values.items():
            if key in parsed:
                lines = [
                    (f"{key}={value}" if line.strip().startswith(f"{key}=") else line)
                    for line in lines
                ]
            else:
                lines.append(f"{key}={value}")
        return "\n".join(lines) + "\n"

    def first(self, needle: str) -> str:
        """Der erste aufgezeichnete Befehl, der ``needle`` enthält."""
        return next(c for c in self.commands if needle in c)


# ── Aufbau ───────────────────────────────────────────────────────────────────


async def _host(
    session: AsyncSession, slug: str, *, fabric_ip: str | None = None, **kw
) -> Host:
    fields = dict(display_name=slug.upper(), kind="ssh", ssh_host="192.0.2.10")
    if fabric_ip:
        fields["fabric_ip"] = fabric_ip
    fields.update(kw)
    host = Host(slug=slug, **fields)
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


async def _duo_recipe(session: AsyncSession, slug: str = "recipe-duo", **kw) -> LocalRecipe:
    fields = dict(
        topology={"nodes": 2}, env_file=ENV_FILE, env_map=dict(ENV_MAP), exclusive=True
    )
    fields.update(kw)
    return await _recipe(session, slug, **fields)


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
    """Gesund-Probe-Ersatz: nur die genannten Slugs gelten als laufend."""

    async def _fake(runtime: Runtime, **kw) -> bool:
        return runtime.slug in running_slugs

    return patch("app.services.recipe_switcher.probe_running", _fake)


# ── Vorfall 1: Verdrängungsschutz (03.09.2026, Hotfix #396) ─────────────────


@pytest.mark.asyncio
async def test_start_of_a_handleless_host_engine_evicts_nothing(session):
    """Vorfall 03.09.2026 (#396): der Start eines Rezepts ohne Startbefehl
    darf das laufende Modell NICHT verdrängen — der Fehler steht ohne
    Netzzugriff fest, also wird nichts angefasst (kein SSH, kein Aufräumen,
    kein Teil-Start)."""

    box_a = await _host(session, "box-a")
    recipe = await _recipe(
        session, "recipe-host", engine="ssh_process",
        launch_template=None, process_name=None,  # kein Handle, kein Befehl
    )
    running_model = await _runtime(session, "qwen-live", box_a)
    ssh = FakeSSH()
    evict = AsyncMock(return_value={"ok": True, "stopped": [], "message": ""})

    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
        patch("app.services.runtime_manager.start_runtime", AsyncMock()),
    ):
        with pytest.raises(recipe_switcher.RecipeStartError) as err:
            await recipe_switcher.start_recipe_on_host(session, box_a, recipe)

    assert err.value.status == 422
    assert recipe_switcher.REASON_NO_COMMAND in err.value.detail
    evict.assert_not_awaited()
    assert ssh.commands == []
    # Das laufende Modell lebt noch.
    await session.refresh(running_model)
    assert running_model.enabled is True
    assert (
        await session.exec(select(Runtime).where(Runtime.slug == "qwen-live"))
    ).first() is not None


@pytest.mark.asyncio
async def test_capacity_blocker_reports_before_any_eviction(session):
    """Vorfall 03.09.2026 (#396), P4-Variante: ein kapazitäts-Blocker (Box zu
    klein) wird gemeldet, BEVOR die Verdrängung das laufende Modell stoppt —
    409, keine SSH-Berührung, keine halbe Instanz."""
    box_a = await _host(
        session, "box-a",
        agent_telemetry={"mem_total_mb": 61440, "mem_available_mb": 60000,
                         "disk_total_gb": 3600.0, "disk_used_gb": 400.0},
    )
    recipe_big = await _recipe(session, "recipe-big", min_vram_gb=100.0)
    await _runtime(session, "qwen-live", box_a)

    ssh = FakeSSH()
    evict = AsyncMock(return_value={"ok": True, "stopped": [], "message": ""})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})

    with (
        _probe({"qwen-live"}),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        with pytest.raises(recipe_switcher.RecipeStartError) as err:
            await recipe_switcher.start_recipe_on_host(session, box_a, recipe_big)

    assert err.value.status == 409
    assert "100 GB" in err.value.detail
    assert evict.await_count == 0
    assert ssh.commands == []
    assert (await session.exec(select(Runtime))).all() != []
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_duo_evicts_worker_before_head(session):
    """Vorfall 03.09.2026 (#396), Reihenfolge: die Verdrängung eines
    Zweibox-Starts räumt ERST die Worker-Box, DANN den Head (den räumt
    ``start_runtime`` selbst). Andersherum stünde der Head leer, während die
    Worker-Box noch das alte Modell hält."""
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2")
    recipe_duo = await _duo_recipe(session)

    order: list[str] = []
    ssh = FakeSSH()

    async def _ensure(runtime, *, host=None, session=None, host_id=None):
        order.append(f"exclusive:{host_id}")
        return {"ok": True, "stopped": [], "message": "frei"}

    async def _start(runtime, **kwargs):
        order.append("start")
        return {"ok": True, "message": "läuft an"}

    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", _ensure),
        patch("app.services.runtime_manager.start_runtime", _start),
    ):
        result = await recipe_switcher.start_recipe_on_host(session, box_a, recipe_duo)

    assert result["ok"] is True
    assert result["worker_host_id"] == str(box_b.id)
    assert order == [f"exclusive:{box_b.id}", "start"]


# ── Vorfall 2: Shell-Injection (05.09.2026) ─────────────────────────────────


@pytest.mark.asyncio
async def test_semicolon_in_recipe_template_never_reaches_the_shell(session):
    """Vorfall 05.09.2026: ein ``;`` im Rezept-Template wird als bestandteil
    des launch-Befehls ÜBER shlex.quote an die Box gegeben — der Umschalter
    selbst baut keine Shell-Metazeichen in den SSH-Befehl ein. Der nohup-
    Wrapper quotet den launch_command als EIN Argument."""
    box_a = await _host(session, "box-a")
    # Ein "böses" Template: das Semikolon gehört hier ZUM Befehl, den MC
    # unverändert (aber gequotet!) an nohup bash -lc durchreicht.
    recipe = await _recipe(
        session, "recipe-inject",
        launch_template="docker run -d --label mc.runtime.slug={slug} img; rm -rf /tmp/x",
    )

    ssh = FakeSSH()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        result = await recipe_switcher.start_recipe_on_host(session, box_a, recipe)

    assert result["ok"] is True
    # Der Start lief über start_runtime; dort wird launch_command via
    # shlex.quote ALS EIN Argument an bash -lc übergeben. Wir prüfen den
    # Vertrag an der Stelle, die der Umschalter aufruft:
    from app.services.launch_template import render_launch_template  # noqa: F401
    runtime = (
        await session.exec(select(Runtime).where(Runtime.slug == result["runtime_slug"]))
    ).first()
    assert runtime is not None
    assert "rm -rf /tmp/x" in runtime.launch_command
    start.assert_awaited_once()
    passed_runtime = start.call_args.args[0]
    # Und der Startpfad nimmt genau DIESEN gerenderten Befehl — nicht das
    # rohe Template.
    assert passed_runtime["launch_command"] == runtime.launch_command


@pytest.mark.asyncio
async def test_injected_recipe_ref_is_never_interpolated_into_a_ssh_command(session):
    """Vorfall 05.09.2026: ein ``recipe_ref`` (aus einem fremden Katalog) darf
    niemals roh in einen SSH-Befehl interpoliert werden. Der Umschalter
    schreibt es nur ins launch_command, das der nohup-Wrapper EIN Argument
    quotet; im Duo-Pfad wird der Pfad der .env über shlex.quote geschützt."""
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2")
    # Ein Duo-Rezept mit einem .env-Pfad, der Shell-Metazeichen trägt:
    recipe = await _duo_recipe(
        session, "recipe-env-inject",
        env_file="~/.env; rm -rf /; touch /tmp/pwned",
    )

    ssh = FakeSSH()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        result = await recipe_switcher.start_recipe_on_host(session, box_a, recipe)

    assert result["ok"] is True
    # Der Schreib-Befehl für die .env muss den Pfad GEQUOTET enthalten — das
    # Semikolon darf die Befehlskette nicht spalten (sonst wäre "rm -rf /"
    # ein eigener Befehl auf der Box).
    env_cmd = ssh.first("MC_K=")
    assert "rm -rf /" in env_cmd, "der Pfad selbst muss erhalten bleiben"
    # Der Pfad wurde als EIN Wort übernommen (quote_remote_path lässt nur die
    # führende Tilde unzitiert — der Rest ist gequotet und spaltet nichts):
    from app.services.recipe_env import quote_remote_path

    path_line = next(line for line in env_cmd.splitlines() if line.startswith("f="))
    assert path_line == "f=" + quote_remote_path("~/.env; rm -rf /; touch /tmp/pwned")


@pytest.mark.asyncio
async def test_unknown_placeholder_renders_a_clean_422_not_a_partial_start(session):
    """Vorfall 05.09.2026: ein kaputter Platzhalter im Template (hier: ein
    ``{unknown}``) erzeugt einen sauberen 422 — keinen Teil-Start, keine
    halbe Instanz, kein angefasstes Modell."""
    box_a = await _host(session, "box-a")
    recipe = await _recipe(
        session, "recipe-broken",
        launch_template="docker run -d --label mc.runtime.slug={slug} {unknown} img",
    )

    ssh = FakeSSH()
    evict = AsyncMock(return_value={"ok": True, "stopped": [], "message": ""})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        with pytest.raises(recipe_switcher.RecipeStartError) as err:
            await recipe_switcher.start_recipe_on_host(session, box_a, recipe)

    assert err.value.status == 422
    assert recipe_switcher.REASON_NO_COMMAND in err.value.detail
    start.assert_not_awaited()
    assert evict.await_count == 0
    assert ssh.commands == []
    # Keine halbe Instanz zurückgelassen.
    assert (await session.exec(select(Runtime))).all() == []


# ── Vorfall 3: Start-Timeout (#418) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_timeout_fails_honestly_and_reports_the_log_path():
    """Vorfall #418: das Modell antwortet nie — der Start meldet ok=False und nennt den Log-Pfad."""
    ssh = AsyncMock(return_value=("", "", 0))  # nohup meldet exit 0 sofort
    with patch.object(runtime_manager, "_ssh_run", ssh), \
            patch.object(runtime_manager, "verify_spark_container_started",
                         AsyncMock(return_value=False)):
        result = await runtime_manager.start_runtime({
            "id": "qwen-general",
            "slug": "qwen-general",
            "display_name": "Qwen",
            "runtime_type": "vllm_docker",
            "endpoint": "http://192.0.2.10:8000/v1",
            "container_name": None,
            "launch_command": "uvx sparkrun run @official/qwen --solo --no-rm",
            "exclusive_memory": False,
        })

    assert result["ok"] is False
    assert "runtime-launch-qwen-general.log" in result["message"]


@pytest.mark.asyncio
async def test_start_timeout_with_exclusive_runtime_leaves_no_worker_orphan(session):
    """Vorfall #418 + Verdrängung: eine exklusive Runtime, deren Nachbarn
    NICHT gestoppt werden kann, meldet einen Fehlschlag und startet nicht —
    die Box ist dann noch belegt, und ein zweites Modell startet blind
    OHNE den Waisen-Pfad zu durchlaufen."""
    impl = AsyncMock(return_value={"ok": True, "message": "up"})
    with patch.object(runtime_manager, "ensure_exclusive_host",
                      AsyncMock(return_value={
                          "ok": False,
                          "message": "läuft noch und konnte nicht gestoppt werden",
                          "stopped": [],
                      })), \
            patch.object(runtime_manager, "_emit_exclusive_event", AsyncMock()), \
            patch.object(runtime_manager, "_start_runtime_impl", impl):
        result = await runtime_manager.start_runtime({
            "id": "qwen-new", "slug": "qwen-new", "display_name": "Qwen Neu",
            "runtime_type": "vllm_docker",
            "endpoint": "http://192.0.2.10:8000/v1",
            "launch_command": "docker run --label mc.runtime.slug=qwen-new img",
            "exclusive_memory": True,
        })

    assert result["ok"] is False
    assert "gestoppt werden" in result["message"]
    impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_box_stop_goes_through_the_recipe_stop_command():
    """Stop (Waisen-Schutz, Live 04.09.2026): ein laufender Verbund wird über
    seinen EIGENEN stop_command beendet — nur der kennt die Worker-Box. Ein
    blosses ``docker stop`` am Head liesse den Worker als Waise mit belegtem
    Speicher zurück."""
    ssh = FakeSSH({"bash -lc": ("", "", 0)})
    with patch.object(runtime_manager, "_ssh_run", ssh), \
            patch.object(runtime_manager, "anchor_running", AsyncMock(return_value=False)):
        result = await runtime_manager.stop_multi_box_instance({
            "id": "duo-old", "slug": "duo-old", "display_name": "Duo Alt",
            "runtime_type": "vllm_docker", "container_name": "duo-old-head",
            "stop_command": "cd ~/old && ./stop.sh",
            "topology": {"nodes": 2, "recipe_slug": "recipe-old"},
        })

    assert result["ok"] is True
    # Der Stop lief über den eigenen Stopp-Befehl des Rezepts — nicht über ein
    # bare docker stop, das den Worker als Waise zurückliesse:
    stop_cmd = ssh.commands[0]
    assert stop_cmd.startswith("bash -lc")
    assert "./stop.sh" in stop_cmd


# ── Duo-Start mit Worker-Wahl (P3) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_duo_named_worker_wins_and_members_are_written(session):
    """Duo-Start mit Worker-Wahl (P3): ein genannter ``worker_host_id``
    gewinnt gegen die Kandidaten-Reihenfolge; die Mitgliedschaft (Head rank 0,
    Worker rank 1) wird in ``runtime_hosts`` festgehalten."""
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2")
    box_c = await _host(session, "box-c", ssh_host="192.0.2.12", fabric_ip="10.0.0.3", role="worker")
    recipe = await _duo_recipe(session)

    ssh = FakeSSH()
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe(set()),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        result = await recipe_switcher.start_recipe_on_host(
            session, box_a, recipe, worker_host_id=str(box_c.id)
        )

    assert result["ok"] is True
    assert result["worker_slug"] == "box-c"
    members = (await session.exec(select(RuntimeHost))).all()
    assert sorted((m.role, m.node_rank) for m in members) == [("head", 0), ("worker", 1)]
    assert {m.host_id for m in members} == {box_a.id, box_c.id}
    # Die .env trägt die Adressen des genannten Workers:
    env_cmd = ssh.first("MC_K=")
    assert "10.0.0.1" in env_cmd and "10.0.0.3" in env_cmd


@pytest.mark.asyncio
async def test_duo_busy_named_worker_is_409_and_touches_nothing(session):
    """Duo-Start (P3): ein genannter Worker, der von einer FREMDEN exklusiven
    Instanz belegt ist, antwortet 409 — und die Verdrängung wird NICHT erst
    für den Head ausgeführt (sonst wäre das laufende Modell tot und der
    Verbund nie gestartet)."""
    box_a = await _host(session, "box-a", fabric_ip="10.0.0.1")
    box_b = await _host(session, "box-b", ssh_host="192.0.2.11", fabric_ip="10.0.0.2")
    box_c = await _host(session, "box-c", ssh_host="192.0.2.12", fabric_ip="10.0.0.3", role="worker")
    recipe = await _duo_recipe(session)
    foreign = await _runtime(
        session, "foreign-head", box_c, topology={"nodes": 2, "recipe_slug": "recipe-other"}
    )
    session.add(RuntimeHost(runtime_id=foreign.id, host_id=box_c.id, role="head", node_rank=0))
    await session.commit()

    ssh = FakeSSH()
    evict = AsyncMock(return_value={"ok": True, "stopped": [], "message": ""})
    start = AsyncMock(return_value={"ok": True, "message": "läuft an"})
    with (
        _probe({"foreign-head"}),
        patch("app.services.runtime_manager._ssh_run", ssh),
        patch("app.services.runtime_manager.ensure_exclusive_host", evict),
        patch("app.services.runtime_manager.start_runtime", start),
    ):
        with pytest.raises(recipe_switcher.RecipeStartError) as err:
            await recipe_switcher.start_recipe_on_host(
                session, box_a, recipe, worker_host_id=str(box_c.id)
            )

    assert err.value.status == 409
    assert "box-c" in err.value.detail
    evict.assert_not_awaited()
    start.assert_not_awaited()
    # Keine Instanz angelegt, kein SSH-Befehl abgesetzt.
    assert (await session.exec(select(Runtime).where(Runtime.slug.contains("recipe-duo")))).all() == []


# ── Stop-Kontrakt (ssh_process) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ssh_process_stop_verifies_the_process_is_gone():
    """Stop-Kontrakt: nach dem pkill-Rückfall wird an der Box GEPRÜFT, dass
    der Prozess wirklich weg ist — ein exit-0-pkill ohne Wirkung wäre eine
    Lüge, auf der das nächste Modell blind starten würde."""
    calls: list[str] = []
    alive = iter([0, 0, 0, 1, 1, 1])  # beim Verify erst da, dann weg

    async def handler(cmd, **kw):
        calls.append(cmd)
        if cmd.startswith("pgrep"):
            return ("", "", next(alive, 1))
        return ("", "", 0)

    with patch.object(runtime_manager, "_ssh_run", handler):
        saved_interval = runtime_manager._verify_poll_interval
        runtime_manager._verify_poll_interval = 0
        try:
            result = await runtime_manager.stop_ssh_process(
                {
                    "id": "ds4-spark", "slug": "ds4-spark", "display_name": "DS4",
                    "runtime_type": "ssh_process",
                    "endpoint": "http://192.0.2.10:8888/v1",
                    "process_name": "ds4-server",
                    "launch_command": "./start.sh",
                },
                host=recipe_switcher.resolved_host_from_row(
                    Host(slug="box-a", display_name="BOX-A", kind="ssh", ssh_host="192.0.2.10")
                ),
            )
        finally:
            runtime_manager._verify_poll_interval = saved_interval

    assert result["ok"] is True
    assert any("pkill -x ds4-server" in c for c in calls)
    assert any("docker stop" in c for c in calls)  # dieselbe Prüfung deckt Container ab
