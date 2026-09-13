"""Waechter fuer die Interaktions-Verfahren (Spec docs/specs/jarvis-interaktions-verfahren.md).

Diese Tests pruefen den PROMPTTEXT, nicht das Modellverhalten. Sie fangen
Regressionen ab, wenn jemand einen Block loescht oder die Absolutregel
zurueckbaut, die mit der Backchannel-Politik kollidiert.
"""

import re

from jarvis_core import persona


def _normalize_whitespace(text: str) -> str:
    """Zieht jede Folge von Leerraum (inkl. Zeilenumbruechen) zu einem Space zusammen.

    Die Waechter pruefen Formulierungen, nicht Typografie — ein Zeilenumbruch
    mitten in einer geprueften Phrase darf sich verschieben, ohne einen Test
    zu brechen.
    """
    return re.sub(r"\s+", " ", text)


def _voice(**kw) -> str:
    """Voice-Prompt mit normalisiertem Whitespace, Gross-/Kleinschreibung unveraendert.

    Case bleibt erhalten, weil die geprueften Blocklabels (z. B. "Do NOT
    delegate when:") auf exakte Schreibweise pruefen.
    """
    kw.setdefault("operator_name", "Mark")
    return _normalize_whitespace(persona.build_live_voice_instructions(**kw))


def test_voice_layer_has_openai_policy_blocks():
    """OpenAI gibt vier benannte Bloecke vor; alle vier muessen vorhanden sein."""
    text = _voice()
    for label in ("Backchannel policy:", "Interruption policy:", "Delegation policy:"):
        assert label in text, f"Block fehlt: {label}"


def test_voice_layer_has_do_not_delegate_block():
    """Der 'wann NICHT delegieren'-Block ist der, der heute fehlt."""
    text = _voice()
    assert "Do NOT delegate when:" in text


def test_voice_layer_has_no_absolute_silence_rule():
    """Eine pauschale 'nie erzaehlen'-Regel widerspricht der Backchannel-Politik.

    OpenAI warnt davor ausdruecklich. Erlaubt ist eine Dosierungsregel, nicht
    ein Verbot.
    """
    text = _voice()
    assert "Never narrate tool calls" not in text


def _backend(**kw) -> str:
    """Backend-Prompt, whitespace-normalisiert und kleingeschrieben.

    Die Waechter pruefen Formulierungen, nicht Typografie — Zeilenumbruch und
    Gross-/Kleinschreibung duerfen sich aendern, ohne einen Test zu brechen.
    """
    kw.setdefault("operator_name", "Mark")
    kw.setdefault("frontier_enabled", False)
    text = persona.build_live_delegation_instructions(**kw)
    return _normalize_whitespace(text).lower()


def test_backend_forbids_inventing_details():
    """Der gemessene Fehler vom 12.09.: erfundene Anforderungen im Auftrag."""
    text = _backend()
    assert "never assume details" in text
    assert "ask for clarification before action" in text


def test_backend_has_correction_precedence():
    """'nee, doch Reviewer' muss den ersten Namen schlagen."""
    text = _backend()
    assert "latest user intent overrides prior statements" in text


def test_backend_requires_consent_before_dispatch():
    """Nichts geht raus, bevor der Operator zugestimmt hat."""
    text = _backend()
    assert "do not send anything before the operator has agreed" in text


def test_backend_forbids_unspoken_requirements():
    """Kernregel gegen das Dazuerfinden im Auftragstext."""
    text = _backend()
    assert "must not contain any requirement the operator did not state" in text


def test_backend_guards_against_double_execution():
    """LiveKit #7230: ein Tool kann doppelt ausgefuehrt werden."""
    text = _backend()
    assert "never re-call the same tool in one delegation" in text


def test_backend_forbids_premature_completion_claims():
    text = _backend()
    assert "never claim an action has finished before the backend confirms" in text


def test_backend_requires_plain_language():
    text = _backend()
    assert "plain language" in text
    assert "spell out abbreviations" in text


def test_backend_forbids_explaining_everyday_terms():
    """Die Bremse gegen Belehren — Spec 4.7.

    Task, Approval, Branch, Deploy, PR und Agent sind der Alltag des
    Operators. Wer die erklaert, behandelt ihn wie einen Anfaenger.
    """
    text = _backend()
    assert "never explain terms the operator uses daily" in text


def test_backend_echoes_names_and_ids():
    """Eigennamen werden vom Modell verstuemmelt — der erkannte Wert muss
    hoerbar zurueckkommen, bevor etwas passiert."""
    text = _backend()
    assert "say the recognised name or number back" in text


def test_backend_brevity_is_default_not_hard_cap():
    """Die 1-2-Satz-Regel und die neue Ausnahme-Klausel muessen im selben
    Prompt stehen — sonst raet das Modell, welche Regel gewinnt, wenn ein
    Name zurueckgespiegelt oder eine Abkuerzung ausgeschrieben werden muss."""
    text = _backend()
    assert "keep it short (1-2 sentences)" in text
    assert "short is the default, not a hard cap" in text
    assert "accuracy wins over brevity" in text
    assert "never pad" in text


def test_backend_example_does_not_spell_out_protected_term():
    """Der PR-Kennungs-Beispiel darf 'Pull Request' nicht ausschreiben — PR
    steht wenige Zeilen darunter auf der Liste der Begriffe, die NIE erklaert
    werden. Ein Beispiel, das der eigenen Regel widerspricht, bringt dem
    Modell das Gegenteil bei: es lernt aus Beispielen staerker als aus der
    Regel selbst."""
    text = _backend()
    assert "pull request" not in text
