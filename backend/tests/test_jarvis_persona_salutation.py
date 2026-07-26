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


# ── Grammatik-Gate ────────────────────────────────────────────────────
# Im Deutschen steht ein Eigenname ohne Artikel, ein Gattungsname braucht
# einen. Deshalb passt KEIN generischer Fallback in jede Satzposition — jeder
# Satz mit {operator} muss so gebaut sein, dass "Mark" UND "dem Operator"
# tragen. Beim ersten Umbau fielen genau hier drei Fehler durch, die der
# Anwesenheits-Test oben nicht sieht.

_BAD_PATTERNS = [
    ("an dem Operator", "'an' verlangt Akkusativ ('an den ...') — Satz umbauen"),
    ("fuer dem Operator", "'fuer' verlangt Akkusativ"),
    ("ueber dem Operator", "'ueber' hier Akkusativ"),
    ("VON dem Operator", "Gross/klein-Mix in Versalien-Ueberschrift"),
    ("DES OPERATORS", "Versalien-Ueberschrift mit Genitiv — mit Namen unlesbar"),
]


def _render_both():
    return {
        "Mark": persona.build_instructions(VOICE, operator_name="Mark", frontier_enabled=False),
        "fallback": persona.build_instructions(VOICE, operator_name=None, frontier_enabled=False),
    }


def test_no_broken_case_in_either_variant():
    for label, text in _render_both().items():
        for bad, why in _BAD_PATTERNS:
            assert bad not in text, f"[{label}] '{bad}' — {why}"


def test_no_sentence_starts_with_lowercase_fallback():
    """'... technisch. dem Operator geht es um ...' — Satzanfang klein."""
    text = _render_both()["fallback"]
    for sep in (". ", "! ", "? "):
        assert f"{sep}dem Operator" not in text, (
            f"Satzanfang '{sep.strip()} dem Operator' — Platzhalter darf nicht "
            "am Satzanfang stehen, sonst ist er im Fallback kleingeschrieben"
        )


def test_both_variants_have_equal_placeholder_count():
    """Beide Varianten muessen aus demselben Template stammen.

    Zaehlt mit einem kollisionsfreien Sentinel statt mit "Mark" — letzteres
    steckt als Substring in "Marktanalyse" (Researcher-Rolle im Team-Roster)
    und ergibt einen Zaehler zu viel.
    """
    sentinel = "Qwxyz"
    a = persona.build_instructions(
        VOICE, operator_name=sentinel, frontier_enabled=False
    ).count(sentinel)
    b = _render_both()["fallback"].count(persona.DEFAULT_OPERATOR)
    assert a > 0, "Platzhalter wird gar nicht ersetzt"
    assert a == b, f"Sentinel {a}x, Fallback {b}x — Templates driften auseinander"
