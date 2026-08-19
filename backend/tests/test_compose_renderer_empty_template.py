"""Der erste Agent in einer LEEREN Vorlage (19.08.2026).

Warum es diese Vorlage gibt: `docker/docker-compose.agents.yml` wird zur
Laufzeit fortgeschrieben. Solange sie in der Versionsverwaltung lag, hat jeder
Commit die Flotte ihres Autors mitveroeffentlicht — Agentennamen, Kunden- und
Projektbezuege. Ausgeliefert wird darum `docker-compose.agents.example.yml`
OHNE Agenten; `start-all.sh` kopiert sie beim ersten Start.

Der Haken: Ein blankes `services:` ist ungueltiges Compose ("services must be
a mapping", live geprueft), also steht dort die leere Abbildung `services: {}`.
Genau die fand der Renderer vorher NICHT — er haengte den ersten Agenten ans
Dateiende, hinter `networks:`/`volumes:`, wo er als top-level Schluessel landet
und die Datei zerstoert.
"""
from pathlib import Path

import pytest
import yaml

from app.services.compose_renderer import _insert_new_agent_blocks


EMPTY_TEMPLATE = """\
x-claude-agent-base: &claude-agent-base
  image: mc-claude-agent:latest
  restart: unless-stopped
  networks:
    - mission-control_default

services: {}

networks:
  mission-control_default:
    external: true

volumes:
  mc_shared_deliverables:
    external: true
    name: mission-control_mc_shared_deliverables
"""


def _render(template: str, agents):
    return _insert_new_agent_blocks(template, agents, vault_writers=set())


def test_first_agent_lands_inside_services_not_at_file_end():
    out = _render(EMPTY_TEMPLATE, [("alpha", "mc-claude-agent:latest")])

    data = yaml.safe_load(out)
    assert "mc-agent-alpha" in data["services"], out
    # Die Geschwister duerfen dabei nicht verloren gehen oder verrutschen.
    assert "networks" in data and "volumes" in data


def test_empty_map_marker_is_removed():
    """`services: {}` darf nicht ueber echten Eintraegen stehen bleiben — YAML
    naehme dann die leere Abbildung und ignorierte alles darunter."""
    out = _render(EMPTY_TEMPLATE, [("alpha", None)])

    assert "services: {}" not in out
    assert yaml.safe_load(out)["services"]["mc-agent-alpha"] is not None


def test_second_agent_joins_the_first():
    once = _render(EMPTY_TEMPLATE, [("alpha", None)])
    twice = _insert_new_agent_blocks(once, [("beta", None)], vault_writers=set())

    services = yaml.safe_load(twice)["services"]
    assert set(services) == {"mc-agent-alpha", "mc-agent-beta"}


def test_result_stays_valid_yaml():
    out = _render(EMPTY_TEMPLATE, [("alpha", None), ("beta", None)])
    yaml.safe_load(out)  # wirft, wenn die Struktur kaputt ist


def test_plain_services_header_still_works():
    """Bestehende Dateien tragen `services:` mit Eintraegen — unveraendert."""
    filled = EMPTY_TEMPLATE.replace(
        "services: {}",
        "services:\n"
        "  mc-agent-vorhanden:\n"
        "    <<: *claude-agent-base\n"
        "    container_name: mc-agent-vorhanden\n",
    )
    out = _insert_new_agent_blocks(filled, [("neu", None)], vault_writers=set())

    services = yaml.safe_load(out)["services"]
    assert "mc-agent-vorhanden" in services and "mc-agent-neu" in services


def test_example_template_shipped_in_the_repo_is_agent_free():
    """Die ausgelieferte Vorlage darf KEINE Agenten enthalten — das ist der
    ganze Zweck der Trennung."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "docker" / "docker-compose.agents.example.yml"
    assert example.exists(), f"Vorlage fehlt: {example}"

    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    assert data.get("services") in ({}, None), (
        f"Die ausgelieferte Vorlage enthaelt Agenten: {list((data.get('services') or {}))}"
    )


def test_example_template_names_no_person_or_project():
    """Gegen Rueckfall: kein Agentenname aus einer echten Flotte in der Vorlage."""
    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "docker" / "docker-compose.agents.example.yml").read_text(
        encoding="utf-8"
    )
    for name in ("rex", "freecode", "davinci", "sparky", "shakespeare",
                 "estrichvision", "deployer", "researcher", "tester"):
        assert f"mc-agent-{name}" not in text, f"'{name}' steht in der Vorlage"
