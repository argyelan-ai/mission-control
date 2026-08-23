"""Vor `docker compose -f` erst nachsehen, ob die Dateien lesbar sind.

`docker-compose.yml` hat diesen Vorabtest seit 2026-07 — eingebaut, weil ein
undurchsichtiger compose-Fehler ("no such file or directory") eine ganze
Sitzung Diagnose gekostet hat: Docker-Desktop-Einzeldatei-Mounts werden schal,
wenn die Host-Datei darunter atomar ersetzt wird (git checkout, Editor-Save).

`docker/docker-compose.agents.yml` hatte ihn nicht — und ist seit dem OSS-Split
die Datei, die auf einer frischen Installation ueberhaupt fehlen kann. Ohne
Vorabtest sieht der Nutzer denselben nichtssagenden compose-Fehler, statt
"fuehre ./setup.sh aus".
"""
from __future__ import annotations

from pathlib import Path

from app.services.docker_agent_sync import compose_preflight_error


def _write(p: Path, text: str = "services: {}\n") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_both_files_present_is_no_error(tmp_path):
    main = _write(tmp_path / "docker-compose.yml")
    agents = _write(tmp_path / "docker" / "docker-compose.agents.yml")
    assert compose_preflight_error(main, agents) is None


def test_missing_main_names_the_stale_mount_fix(tmp_path):
    main = tmp_path / "docker-compose.yml"
    agents = _write(tmp_path / "docker" / "docker-compose.agents.yml")
    msg = compose_preflight_error(main, agents)
    assert msg and "docker compose restart backend" in msg


def test_empty_main_counts_as_missing(tmp_path):
    """Ein schaler Bind-Mount liest sich als LEER, nicht als fehlend."""
    main = _write(tmp_path / "docker-compose.yml", "")
    agents = _write(tmp_path / "docker" / "docker-compose.agents.yml")
    assert compose_preflight_error(main, agents) is not None


def test_missing_agents_file_points_at_setup(tmp_path):
    """Der Fall, den dieser PR erst moeglich macht: die eigene Agenten-Datei
    fehlt. Die Meldung muss sagen, was zu tun ist."""
    main = _write(tmp_path / "docker-compose.yml")
    agents = tmp_path / "docker" / "docker-compose.agents.yml"
    msg = compose_preflight_error(main, agents)
    assert msg, "der fehlende Agenten-Compose blieb unbemerkt"
    assert "docker-compose.agents.yml" in msg
    assert "setup.sh" in msg, msg


def test_empty_agents_file_counts_as_missing(tmp_path):
    main = _write(tmp_path / "docker-compose.yml")
    agents = _write(tmp_path / "docker" / "docker-compose.agents.yml", "")
    assert compose_preflight_error(main, agents) is not None


def test_both_call_sites_use_the_preflight():
    """Die Wache nuetzt nichts, wenn nur einer der beiden Aufrufer sie nutzt.

    Beide Stellen rufen `docker compose -f <main> -f <agents>` auf:
    `restart_docker_agent_container` (Runtime-Switch) und der
    cli_terminal-Recreate-Pfad.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    for rel in ("services/docker_agent_sync.py", "routers/cli_terminal.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "compose_preflight_error" in text, f"{rel} prueft nicht vorab"
