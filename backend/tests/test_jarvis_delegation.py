"""Jarvis bleibt Vorzimmer: Denkarbeit geht an Boss, nicht an ein API-Modell.

Kostenbild (Operator-Entscheid 27.07.2026): Boss laeuft auf dem Claude-Max-
Abo — seine Arbeit kostet nichts extra. Jarvis' ``ask_frontier`` ruft dagegen
ein Frontier-Modell PRO ANFRAGE ueber die API. Damit ist Jarvis' Auftrag
geschaerft: Er nimmt auf, meldet Status, ruft Wissen ab — inhaltliche Arbeit
delegiert er an Boss.

Der heikle Teil ist nicht das Abschalten des Tools (das Flag existiert bereits),
sondern was Jarvis DANN tut: ohne Ersatzregel beantwortet er schwere Fragen
einfach selbst mit dem kleinen Chat-Modell — schlechtere Antworten, und die
Arbeit landet nie im Board. Deshalb muss die Persona bei deaktiviertem Frontier
eine explizite Delegations-Anweisung tragen.
"""
import pytest

from jarvis_core.channels import TELEGRAM, VOICE
from jarvis_core.persona import build_instructions


def _instr(frontier: bool, channel=TELEGRAM) -> str:
    return build_instructions(channel, frontier_enabled=frontier, operator_name="Mark")


# ── Frontier AUS: Delegation an Boss muss dastehen ────────────────────────

def test_delegation_block_present_when_frontier_disabled():
    text = _instr(frontier=False)
    assert "ask_frontier" not in text, "totes Tool darf nicht erwaehnt werden"
    lowered = text.lower()
    assert "boss" in lowered
    # Die Anweisung muss handlungsleitend sein, nicht nur Boss erwaehnen:
    # irgendeine Form von "gib es an Boss / delegiere" muss vorkommen.
    assert any(
        marker in lowered
        for marker in ("an boss", "delegier", "dispatch_to_agent", "create_task")
    ), "keine ausfuehrbare Delegations-Anweisung in der Persona"


def test_delegation_block_names_the_hard_question_triggers():
    """Dieselben Ausloeser, die frueher ask_frontier gestartet haben, muessen
    jetzt die Delegation ausloesen — sonst faellt Jarvis in Selbstbeantwortung."""
    text = _instr(frontier=False).lower()
    for trigger in ("analys", "plan", "konzept"):
        assert trigger in text, f"Trigger '{trigger}' fehlt in der Delegationsregel"


def test_no_self_answering_on_hard_questions():
    """Explizites Verbot: nicht selbst ausdenken."""
    text = _instr(frontier=False).lower()
    assert "nicht selbst" in text or "niemals selbst" in text


# ── Frontier AN: altes Verhalten bleibt unveraendert ──────────────────────

def test_frontier_block_unchanged_when_enabled():
    text = _instr(frontier=True)
    assert "ask_frontier" in text
    # Bei aktivem Tool KEINE widerspruechliche Delegationsregel einblenden.
    assert "SCHWERE FRAGEN — an Boss" not in text


def test_exactly_one_hard_question_rule_per_mode():
    """Nie beide Bloecke gleichzeitig — das waere ein Widerspruch in der
    Anweisung und der Grund, warum Modelle inkonsistent handeln."""
    on, off = _instr(frontier=True), _instr(frontier=False)
    assert ("ask_frontier" in on) and ("ask_frontier" not in off)
    assert on != off


# ── Kanal-Unabhaengigkeit ─────────────────────────────────────────────────

@pytest.mark.parametrize("channel", [TELEGRAM, VOICE])
def test_delegation_applies_to_every_channel(channel):
    """Die Kostenregel gilt am Desk wie mobil."""
    text = build_instructions(channel, frontier_enabled=False, operator_name="Mark")
    assert "ask_frontier" not in text
    assert "boss" in text.lower()


def test_operator_placeholder_still_resolves_in_new_block():
    """Der neue Block darf keinen rohen {operator}-Platzhalter hinterlassen."""
    text = _instr(frontier=False)
    assert "{operator}" not in text
