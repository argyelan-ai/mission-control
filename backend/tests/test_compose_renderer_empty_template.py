"""Der erste Agent in einer LEEREN Vorlage (19.08.2026).

Warum es diese Vorlage gibt: `docker/docker-compose.agents.yml` wird zur
Laufzeit fortgeschrieben. Solange sie in der Versionsverwaltung lag, hat jeder
Commit die Flotte ihres Autors mitveroeffentlicht — Agentennamen, Kunden- und
Projektbezuege. Ausgeliefert wird darum `docker-compose.agents.example.yml`
OHNE Agenten; `setup.sh` legt daraus die eigene Kopie an (`start-all.sh`
springt ein, falls sie doch fehlt).

Der Haken: Ein blankes `services:` ist ungueltiges Compose ("services must be
a mapping", live geprueft), also steht dort die leere Abbildung `services: {}`.
Genau die fand der Renderer vorher NICHT — er haengte den ersten Agenten ans
Dateiende, hinter `networks:`/`volumes:`, wo er als top-level Schluessel landet
und die Datei zerstoert.
"""
import re
from pathlib import Path

import yaml

from app.services.compose_renderer import (
    _insert_new_agent_blocks,
    prune_compose_agent_block,
)


# Geprueft wird die ECHTE ausgelieferte Vorlage, keine Handkopie. Eine Kopie
# im Test driftet ab (sie tat es bereits: ohne Kommentarblock, ohne die
# kimi-/omp-/openclaude-Anker) und beweist dann nichts mehr ueber das, was
# Leute tatsaechlich herunterladen.
_REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TEMPLATE_PATH = _REPO_ROOT / "docker" / "docker-compose.agents.example.yml"
EMPTY_TEMPLATE = EXAMPLE_TEMPLATE_PATH.read_text(encoding="utf-8")


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
    assert EXAMPLE_TEMPLATE_PATH.exists(), f"Vorlage fehlt: {EXAMPLE_TEMPLATE_PATH}"

    text = EXAMPLE_TEMPLATE_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data.get("services") in ({}, None), (
        f"Die ausgelieferte Vorlage enthaelt Agenten: {list((data.get('services') or {}))}"
    )

    # Ein leeres ``services`` allein reicht nicht: ein ``container_name`` in
    # einem der ``x-``-Anker taucht dort nicht auf, benennt aber trotzdem einen
    # Agenten. (Ein AUSKOMMENTIERTER Block entkommt beiden Pruefungen — den
    # faengt scripts/privacy-scan.py ueber den Namen selbst.)
    container_names = re.findall(r"^\s*container_name:\s*(\S+)", text, re.M)
    assert not container_names, f"container_name in der Vorlage: {container_names}"




def test_full_cycle_empty_to_agent_and_back_stays_valid_compose():
    """Der Rueckweg: leer → ein Agent → geloescht → wieder gueltiges Compose.

    ``_insert_new_agent_blocks`` ersetzt die leere Abbildung beim ersten
    Agenten durch ein blankes ``services:``. Wird danach der letzte Agent
    geloescht (``prune_compose_agent_block``, aufgerufen beim Loeschen und
    Archivieren), bleibt genau dieses blanke ``services:`` uebrig — und das
    ist ungueltiges Compose ("services must be a mapping"). Danach scheitert
    JEDES compose-Kommando: start-all.sh Schritt 3, der Container-Recreate,
    der Runtime-Switch.
    """
    with_agent = _render(EMPTY_TEMPLATE, [("alpha", None)])
    pruned, removed = prune_compose_agent_block(with_agent, "alpha")

    assert removed, "der Block wurde gar nicht gefunden"
    data = yaml.safe_load(pruned)
    assert isinstance(data.get("services"), dict), (
        "services muss eine Abbildung bleiben, sonst lehnt docker compose die "
        f"Datei ab. Ergebnis:\n{pruned}"
    )
    assert data["services"] == {}, data["services"]
    # Die Geschwister muessen den Rueckweg ueberleben.
    assert "networks" in data and "volumes" in data


def test_the_explainer_comment_does_not_outlive_its_own_truth():
    """Der Kommentar erklaert, warum dort eine leere Abbildung steht.

    Stand er UNTER `services: {}`, blieb er nach dem ersten Agenten fuer immer
    ueber den echten Bloecken stehen und beschrieb einen Zustand, den es dann
    nicht mehr gibt. Er gehoert darum ueber die Zeile — ausserhalb des
    Bereichs, den der Splice anfasst.
    """
    marker = "Hier stehen nach dem ersten Agenten"
    services_line = "services: {}"
    assert EMPTY_TEMPLATE.index(marker) < EMPTY_TEMPLATE.index(services_line), (
        "der Erklaer-Kommentar steht unter `services: {}`"
    )

    out = _render(EMPTY_TEMPLATE, [("alpha", None)])
    lines = out.splitlines()
    comment_at = next(i for i, l in enumerate(lines) if marker in l)
    services_at = next(i for i, l in enumerate(lines) if l.rstrip() == "services:")
    agent_at = next(i for i, l in enumerate(lines) if l.strip() == "mc-agent-alpha:")

    assert comment_at < services_at < agent_at, (
        "nach dem ersten Agenten steht der Kommentar mitten in der "
        f"services-Sektion:\n{out}"
    )
