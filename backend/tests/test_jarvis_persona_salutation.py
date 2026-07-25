"""Die Persona darf den Operator nicht generisch "Operator" nennen.

Mark heisst in MC "Mark" — ihn "Operator" zu nennen war Bug #1 der Welle A+B.
Der Name kommt aus der DB (users.preferred_name) und wird beim Session-Start
in den Prompt gesetzt; faellt der Abruf aus, bleibt die neutrale Anrede.
"""

from jarvis_core import persona
from jarvis_core.channels import VOICE


def test_named_operator_appears_in_prompt():
    text = persona.build_instructions(VOICE, operator_name="Mark", frontier_enabled=False)
    assert "Mark" in text


def test_named_operator_replaces_generic_term():
    """Mit Namen taucht das generische Wort nicht mehr als Anrede auf."""
    text = persona.build_instructions(VOICE, operator_name="Mark", frontier_enabled=False)
    assert "des Operators" not in text
    assert "der Operator" not in text


def test_fallback_without_name_still_builds():
    """Ohne Namen muss die Persona weiterhin vollstaendig bauen (Backend weg)."""
    text = persona.build_instructions(VOICE, operator_name=None, frontier_enabled=False)
    assert "Jarvis" in text
    assert len(text) > 500


def test_placeholder_never_leaks():
    """Der Template-Platzhalter darf in keiner Variante im Prompt landen."""
    for name in ("Mark", None, ""):
        text = persona.build_instructions(VOICE, operator_name=name, frontier_enabled=False)
        assert "{operator}" not in text
        assert "{OPERATOR}" not in text
