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
    kw.setdefault("operator_name", "Alex")
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
    kw.setdefault("operator_name", "Alex")
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
    """Befund 7: der Prompt IST das Backend — bestaetigen kann nur das
    Tool-Ergebnis, nicht "das Backend"."""
    text = _backend()
    assert "never claim an action has finished before the tool result" in text
    assert "before the backend confirms" not in text


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


def test_backend_answers_from_context_instead_of_delegating():
    """Befund 2: die sechste Kernregel — was aus dem Kontext beantwortbar ist,
    wird beantwortet, nicht delegiert. Ohne sie zieht "always call the real
    tool" in die Gegenrichtung."""
    text = _backend()
    assert "respond directly" in text
    assert "no tool call, no delegation" in text


def test_backend_tools_block_is_bound_to_the_procedure():
    """Befund 1: der TOOLS-Block darf die Zustimmungspflicht nicht aufheben.

    Der stille Boss-Default ("ruf ohne assignee auf") war woertlich der
    Vorfall vom 12.09. — er muss ein ausgesprochener Vorschlag sein.
    """
    text = _backend()
    assert "stay bound to taking an order" in text
    assert "an unclear target agent is never a reason to call anyway" in text
    assert "call without assignee (boss decides)" not in text


def test_backend_example_has_no_angle_bracket_placeholders():
    """Befund 8: Modelle sprechen "<Agent>" gelegentlich woertlich aus."""
    text = _backend()
    assert "<agent>" not in text
    assert "<repo>" not in text


def test_backend_abbreviation_rule_names_its_exception():
    """Befund 10: "Abkuerzungen ausschreiben" reibt an der Schutzliste, auf
    der PR steht — die Ausnahme muss ausdruecklich verklammert sein."""
    text = _backend()
    assert "spell out abbreviations the first time — except the everyday terms" in text


def test_voice_layer_keeps_talking_while_backend_works():
    """Befund 3: aus "rede weiter" wurde still "warte" — im echten Anruf
    wurden 24 Sekunden Funkstille gemessen."""
    text = _voice()
    assert "keep talking — don't go silent" in text
    assert "don't invent a status, a number or an outcome while waiting" in text


def test_voice_layer_protects_agent_names_from_translation():
    """Befund 3: die Agentennamen waren aus der Fachbegriff-Liste
    verschwunden."""
    text = _voice()
    assert "agent names stay untranslated" in text


def test_voice_layer_allows_length_for_names_and_numbers():
    """Befund 4: die Kuerze-Ausnahme des Backends muss auch in der Stimme
    stehen — sonst kappt sie genau die Saetze, die sie schuetzen soll."""
    text = _voice()
    assert "the length may stretch when you say a name, a number or a confirmation back" in text


def test_voice_layer_has_personality_label():
    """Die vorgeschriebene Blockstruktur verlangt vier benannte Bloecke."""
    assert "Personality:" in _voice()


def test_voice_layer_drops_conditions_it_cannot_judge():
    """Befund 5: Vollstaendigkeitspruefung und Doppel-Delegations-Zaehler
    gehoeren ins Backend bzw. in den Worker — die Stimme sieht beides nicht."""
    text = _voice()
    assert "missing a detail the backend would have to guess" not in text
    assert "a delegation for this same request is already running" not in text


def test_voice_layer_keeps_spoken_list_and_bullet_ban():
    """Befund 9: "numbers/lists" und "never bullets" waren still verlorengegangen."""
    text = _voice()
    assert "numbers/lists said out loud" in text
    assert "never bullets or a read-out document" in text
