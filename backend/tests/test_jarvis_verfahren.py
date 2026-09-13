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
