"""Waechter fuer die Interaktions-Verfahren (Spec docs/specs/jarvis-interaktions-verfahren.md).

Diese Tests pruefen den PROMPTTEXT, nicht das Modellverhalten. Sie fangen
Regressionen ab, wenn jemand einen Block loescht oder die Absolutregel
zurueckbaut, die mit der Backchannel-Politik kollidiert.
"""

from jarvis_core import persona


def test_voice_layer_has_openai_policy_blocks():
    """OpenAI gibt vier benannte Bloecke vor; alle vier muessen vorhanden sein."""
    text = persona.build_live_voice_instructions("Mark")
    for label in ("Backchannel policy:", "Interruption policy:", "Delegation policy:"):
        assert label in text, f"Block fehlt: {label}"


def test_voice_layer_has_do_not_delegate_block():
    """Der 'wann NICHT delegieren'-Block ist der, der heute fehlt."""
    text = persona.build_live_voice_instructions("Mark")
    assert "Do NOT delegate when:" in text


def test_voice_layer_has_no_absolute_silence_rule():
    """Eine pauschale 'nie erzaehlen'-Regel widerspricht der Backchannel-Politik.

    OpenAI warnt davor ausdruecklich. Erlaubt ist eine Dosierungsregel, nicht
    ein Verbot.
    """
    text = persona.build_live_voice_instructions("Mark")
    assert "Never narrate tool calls" not in text


def _backend(**kw) -> str:
    """Backend-Prompt in Kleinschreibung.

    Die Waechter pruefen Formulierungen, nicht Typografie — ein Satzanfang
    darf gross werden, ohne einen Test zu brechen.
    """
    kw.setdefault("operator_name", "Mark")
    kw.setdefault("frontier_enabled", False)
    return persona.build_live_delegation_instructions(**kw).lower()


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
