"""Die eigene Agenten-Datei muss da sein, BEVOR jemand einen Agenten anlegt.

`docker/docker-compose.agents.yml` liegt nicht mehr in der Versionsverwaltung.
Auf einer frischen Installation entstand sie bisher erst in `start-all.sh` —
also viel zu spaet: der Ablauf in `docs/setup/first-agent.md` bindet in Schritt
5.1 eine Runtime, `agent_runtime_switch` ruft `write_compose_agents`, und
`render_compose_agents` brach mit `FileNotFoundError` ab. Provisioning meldet
so einen Fehler laut eigenem Docstring nur an den BackgroundTask-Logger — der
Nutzer sah einen stummen Nicht-Effekt.

Zwei Schichten dagegen, beide hier geprueft:
  1. `setup.sh` legt die Datei aus der Vorlage an (wie schon `.env` aus
     `.env.example`) — der sichtbare, dokumentierte Weg.
  2. Der Renderer zieht die Vorlage notfalls selbst heran, statt hart zu
     scheitern — das Netz fuer alle, die `setup.sh` nicht ausfuehren
     (`docker compose up -d` direkt aus dem README-Schnellstart).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from app.services.compose_renderer import (
    COMPOSE_TEMPLATE_FILENAME,
    DEFAULT_COMPOSE_PATH,
    DEFAULT_COMPOSE_TEMPLATE_PATH,
    render_compose_agents,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHIPPED_TEMPLATE = _REPO_ROOT / "docker" / COMPOSE_TEMPLATE_FILENAME
ENSURE_SH = _REPO_ROOT / "scripts" / "ensure-agents-yml.sh"


@pytest.fixture(autouse=True)
def _patch_compose_redis(fake_redis):
    async def _get_redis():
        return fake_redis

    with patch("app.services.compose_renderer.get_redis", _get_redis):
        yield


# ── Schicht 2: der Renderer ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_uses_the_template_when_the_own_file_is_missing(
    async_session, tmp_path: Path
):
    """Fehlt die eigene Datei, rendert der Renderer aus der Vorlage daneben —
    statt mit FileNotFoundError abzubrechen und den Nutzer im Dunkeln zu
    lassen."""
    target = tmp_path / "docker-compose.agents.yml"
    (tmp_path / COMPOSE_TEMPLATE_FILENAME).write_text(
        _SHIPPED_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert not target.exists()

    rendered = await render_compose_agents(async_session, compose_path=target)

    data = yaml.safe_load(rendered)
    assert isinstance(data.get("services"), dict), rendered
    # Der Renderer schreibt nicht — write_compose_agents tut das.
    assert not target.exists()


@pytest.mark.asyncio
async def test_render_still_fails_loudly_when_template_is_gone_too(
    async_session, tmp_path: Path
):
    """Ohne Datei UND ohne Vorlage bleibt es ein Fehler — aber einer, der
    beide gesuchten Pfade nennt."""
    target = tmp_path / "docker-compose.agents.yml"

    with patch(
        "app.services.compose_renderer.DEFAULT_COMPOSE_TEMPLATE_PATH",
        tmp_path / "gibt-es-nicht.yml",
    ):
        with pytest.raises(FileNotFoundError) as exc:
            await render_compose_agents(async_session, compose_path=target)

    message = str(exc.value)
    assert "docker-compose.agents.yml" in message
    assert COMPOSE_TEMPLATE_FILENAME in message


def test_default_template_path_points_at_the_shipped_file():
    """Die Konstante muss neben der eigenen Datei liegen — sonst greift der
    Fallback auf einer echten Installation ins Leere."""
    assert DEFAULT_COMPOSE_TEMPLATE_PATH.name == COMPOSE_TEMPLATE_FILENAME
    assert DEFAULT_COMPOSE_TEMPLATE_PATH.parent == DEFAULT_COMPOSE_PATH.parent
    assert _SHIPPED_TEMPLATE.exists(), f"Vorlage fehlt im Repo: {_SHIPPED_TEMPLATE}"


# ── Schicht 1: setup.sh ──────────────────────────────────────────────────────


def _run_setup_sh(workdir: Path) -> subprocess.CompletedProcess:
    """setup.sh in einer Wegwerf-Kopie des Projekts ausfuehren.

    Kopiert wird nur, was das Skript anfasst: `.env.example` und `docker/`.
    `scripts/init-mc-deliverables-dirs.sh` fehlt bewusst — das Skript
    ueberspringt den Schritt dann (`[ -x ... ]`), es legt also nichts unter
    dem echten HOME an.
    """
    (workdir / "docker").mkdir(parents=True, exist_ok=True)
    (workdir / ".env.example").write_text(
        (_REPO_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (workdir / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        _SHIPPED_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # setup.sh delegiert an dieselbe Vorrichtung wie start-all.sh.
    (workdir / "scripts").mkdir(exist_ok=True)
    (workdir / "scripts" / "ensure-agents-yml.sh").write_text(
        ENSURE_SH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return subprocess.run(
        ["bash", str(_REPO_ROOT / "setup.sh")],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HOME": str(workdir)},
    )


def test_setup_sh_creates_the_own_agents_file_from_the_template(tmp_path: Path):
    """Frische Installation: nach `./setup.sh` liegt die eigene Datei da —
    gueltiges Compose, ohne einen einzigen Agenten."""
    proc = _run_setup_sh(tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout

    own = tmp_path / "docker" / "docker-compose.agents.yml"
    assert own.exists(), (
        "setup.sh hat die eigene Agenten-Datei nicht angelegt — der erste "
        f"Runtime-Wechsel scheitert dann stumm.\n{proc.stdout}"
    )
    data = yaml.safe_load(own.read_text(encoding="utf-8"))
    assert isinstance(data.get("services"), dict)
    assert data["services"] == {}, "die frische Kopie darf keine Agenten tragen"


def test_setup_sh_never_overwrites_an_existing_fleet(tmp_path: Path):
    """Zweiter Lauf (Update): die eigene Flotte bleibt unangetastet — sonst
    waeren die Agenten des Betreibers weg."""
    (tmp_path / "docker").mkdir(parents=True, exist_ok=True)
    own = tmp_path / "docker" / "docker-compose.agents.yml"
    meins = "services:\n  mc-agent-meiner:\n    image: mc-claude-agent:latest\n"
    own.write_text(meins, encoding="utf-8")

    proc = _run_setup_sh(tmp_path)
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert own.read_text(encoding="utf-8") == meins


# ── Die gemeinsame Vorrichtung hinter setup.sh und start-all.sh ─────────────


def _run_ensure(workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(ENSURE_SH)],
        cwd=workdir, capture_output=True, text=True, timeout=60,
    )


def test_ensure_script_creates_the_file_from_the_template(tmp_path: Path):
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        _SHIPPED_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    proc = _run_ensure(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "docker" / "docker-compose.agents.yml").exists()


def test_ensure_script_keeps_an_existing_file(tmp_path: Path):
    (tmp_path / "docker").mkdir()
    own = tmp_path / "docker" / "docker-compose.agents.yml"
    own.write_text("services:\n  mc-agent-meiner: {}\n", encoding="utf-8")
    (tmp_path / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        "services: {}\n", encoding="utf-8"
    )
    assert _run_ensure(tmp_path).returncode == 0
    assert "mc-agent-meiner" in own.read_text(encoding="utf-8")


def test_ensure_script_fails_loudly_when_the_template_is_gone(tmp_path: Path):
    """Nicht warnen und weitermachen: start-all.sh liefe sonst bis Schritt 3
    weiter und stuerbe dort an einem rohen compose-Fehler, der die hilfreiche
    Meldung von eben ueberschreibt."""
    (tmp_path / "docker").mkdir()
    proc = _run_ensure(tmp_path)
    assert proc.returncode != 0, "das Skript hat nur gewarnt und weitergemacht"
    assert COMPOSE_TEMPLATE_FILENAME in (proc.stderr + proc.stdout)


def test_start_all_stops_instead_of_warning_and_continuing():
    """Der Aufrufer darf den Fehler nicht schlucken."""
    text = (_REPO_ROOT / "scripts" / "start-all.sh").read_text(encoding="utf-8")
    assert "ensure-agents-yml.sh" in text, "start-all.sh nutzt die Vorrichtung nicht"
    assert "WARNUNG: weder" not in text, "start-all.sh warnt immer noch und macht weiter"


# ── Die Doku muss die Trennung kennen ───────────────────────────────────────


@pytest.mark.parametrize(
    "doc",
    ["README.md", "docs/setup/first-agent.md", "docs/setup/updating.md"],
)
def test_user_facing_docs_explain_the_template(doc: str):
    """Kein nutzerseitiges Dokument erwaehnte die Vorlage — sie nannten nur
    die Datei, die auf einer frischen Installation gar nicht existiert."""
    text = (_REPO_ROOT / doc).read_text(encoding="utf-8")
    if "docker-compose.agents.yml" not in text:
        pytest.skip(f"{doc} spricht nicht ueber die Agenten-Compose")
    assert COMPOSE_TEMPLATE_FILENAME in text, (
        f"{doc} nennt die eigene Datei, aber nicht die ausgelieferte Vorlage"
    )


@pytest.mark.asyncio
async def test_render_refuses_to_fall_back_when_the_whole_dir_is_gone(
    async_session, tmp_path: Path
):
    """Die einzige Art, wie der Fallback schaden koennte: das `docker/`-
    Verzeichnis ist im Backend-Container weg (Mount verloren), die eigene
    Datei sieht dadurch "abwesend" aus, und ein Render aus der Vorlage
    ersetzte die echte Flotte durch eine frisch erzeugte — Hand-Mounts weg.
    Fehlt das Verzeichnis, wird darum NICHT gerendert.
    """
    target = tmp_path / "gibt-es-nicht" / "docker-compose.agents.yml"

    # Die Vorlage ist ausdruecklich VORHANDEN — nur so prueft der Test die
    # Verzeichnis-Wache und nicht zufaellig eine fehlende Vorlage.
    with patch(
        "app.services.compose_renderer.DEFAULT_COMPOSE_TEMPLATE_PATH",
        _SHIPPED_TEMPLATE,
    ):
        with pytest.raises(FileNotFoundError) as exc:
            await render_compose_agents(async_session, compose_path=target)

    assert "gibt-es-nicht" in str(exc.value)


# ── Die Datei beschreibt die eigene Flotte — sie geht niemanden sonst an ────


def test_ensure_script_creates_the_file_unreadable_for_others(tmp_path: Path):
    """`cp` uebernimmt die umask, also blieb die Datei ueblicherweise 644 —
    weltlesbar. Drei Zeilen weiter setzt dasselbe Skript `chmod 600` fuer
    `docker/.env.shared`; hier stehen Agentennamen, Kunden- und Projektbezuege
    und Mount-Pfade drin, also derselbe Massstab."""
    (tmp_path / "docker").mkdir()
    (tmp_path / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        _SHIPPED_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _run_ensure(tmp_path).returncode == 0

    mode = (tmp_path / "docker" / "docker-compose.agents.yml").stat().st_mode & 0o777
    assert mode == 0o600, f"Modus {oct(mode)} statt 0o600"


def test_ensure_script_tightens_a_file_that_is_already_there(tmp_path: Path):
    """Bestehende Installationen: die Datei liegt seit Monaten mit 644 da.
    start-all.sh laeuft bei jedem Start durch hier, also wird sie dabei
    nachgezogen — ohne ihren Inhalt anzufassen."""
    (tmp_path / "docker").mkdir()
    own = tmp_path / "docker" / "docker-compose.agents.yml"
    meins = "services:\n  mc-agent-meiner: {}\n"
    own.write_text(meins, encoding="utf-8")
    own.chmod(0o644)
    (tmp_path / "docker" / COMPOSE_TEMPLATE_FILENAME).write_text(
        "services: {}\n", encoding="utf-8"
    )

    assert _run_ensure(tmp_path).returncode == 0
    assert own.stat().st_mode & 0o777 == 0o600
    assert own.read_text(encoding="utf-8") == meins


@pytest.mark.asyncio
async def test_renderer_writes_the_file_unreadable_for_others(
    async_session, tmp_path: Path
):
    """Der Renderer schreibt die Datei bei jedem Agenten und jedem
    Runtime-Wechsel neu — ohne `chmod` waere die Haertung nach dem ersten
    Schreiben wieder weg. Die Sicherungskopie traegt denselben Inhalt und
    darum denselben Modus."""
    from app.services.compose_renderer import write_compose_agents

    target = tmp_path / "docker-compose.agents.yml"
    target.write_text(
        "services:\n  mc-agent-alt:\n    image: mc-claude-agent:latest\n",
        encoding="utf-8",
    )
    target.chmod(0o644)

    result = await write_compose_agents(async_session, compose_path=target)
    assert result["changed"] == "true", result

    assert target.stat().st_mode & 0o777 == 0o600
    backup = Path(result["backup"])
    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600, "die .bak traegt dieselbe Flotte"
