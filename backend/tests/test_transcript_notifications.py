"""Hintergrund-Meldungen der CLI werden gedeutet, nicht abgedruckt.

Die CLI schiebt dem Agenten eine Nachricht in die Sitzung, sobald ein
Hintergrund-Vorgang endet — ein Subagent ODER ein Hintergrund-Befehl:

    <task-notification>
    <task-id>ae172f74…</task-id>
    <tool-use-id>toolu_019o…</tool-use-id>
    <output-file>/tmp/claude-999/…/tasks/ae172f74….output</output-file>
    <status>completed</status>
    <summary>Agent "pruef-demo OS check" finished</summary>
    </task-notification>

Die Zeile traegt die Rolle ``user``. Ungedeutet stand sie darum als
NACHRICHT DES OPERATORS im Chat — eine Wand aus Kennungen und Host-Pfaden,
die Mark nie getippt hat (Befund am Live-Bild, 22.08.2026).

Gemessen ueber 400 Transkripte: 77 solcher Zeilen, immer dieselbe Form;
``tool-use-id`` in 66 von 77, ``status``/``summary`` durchgaengig.
"""

import json

from app.services.transcript_chat import parse_transcript_line


def _user(text: str, uuid: str = "u1") -> str:
    return json.dumps(
        {
            "type": "user",
            "uuid": uuid,
            "timestamp": "2026-08-22T10:00:00Z",
            "message": {"content": text},
        }
    )


NOTIFICATION = """<task-notification>
<task-id>ae172f74745a2d817</task-id>
<tool-use-id>toolu_019oNcj841wxTseqEuEzZcWm</tool-use-id>
<output-file>/tmp/claude-999/-home-agent/b852af42/tasks/ae172f74745a2d817.output</output-file>
<status>completed</status>
<summary>Agent "pruef-demo OS check" finished</summary>
</task-notification>"""


def test_a_notification_becomes_a_notification_not_a_user_message():
    evs = parse_transcript_line(_user(NOTIFICATION))

    assert [e["kind"] for e in evs] == ["notification"]
    ev = evs[0]
    assert ev["status"] == "completed"
    assert ev["summary"] == 'Agent "pruef-demo OS check" finished'
    assert ev["toolUseId"] == "toolu_019oNcj841wxTseqEuEzZcWm"
    assert ev["taskId"] == "ae172f74745a2d817"


def test_the_host_path_never_leaves_the_backend():
    """``output-file`` ist ein Pfad auf der Maschine des Operators. Er hilft
    in der Oberflaeche niemandem und gehoert nicht ueber die Leitung — das
    Repo ist oeffentlich, Bildschirmfotos landen in Issues."""
    ev = parse_transcript_line(_user(NOTIFICATION))[0]

    assert "output-file" not in json.dumps(ev)
    assert "/tmp/" not in json.dumps(ev)
    assert "b852af42" not in json.dumps(ev)


def test_a_failed_background_command_keeps_its_verdict():
    """Dieselbe Form entsteht auch fuer Hintergrund-BEFEHLE, nicht nur fuer
    Subagenten — und ein Fehlschlag muss als solcher erkennbar bleiben."""
    text = (
        "<task-notification>\n<task-id>bj4bkuyb5</task-id>\n"
        "<status>failed</status>\n"
        '<summary>Background command "Check page state" failed with exit code 1</summary>\n'
        "</task-notification>"
    )

    ev = parse_transcript_line(_user(text))[0]

    assert ev["kind"] == "notification"
    assert ev["status"] == "failed"
    assert ev["toolUseId"] is None


def test_normal_user_text_is_untouched():
    """Gegenprobe — die Erkennung darf nicht auf echte Nachrichten anspringen,
    auch nicht auf solche, die ueber Hintergrund-Meldungen SPRECHEN."""
    evs = parse_transcript_line(_user("was macht eigentlich <task-notification> im chat?"))

    assert [e["kind"] for e in evs] == ["message"]
    assert evs[0]["role"] == "user"


def test_a_truncated_notification_is_not_swallowed():
    """Wer nur den Anfang hat, ist keine gueltige Meldung. Sie als solche zu
    deuten hiesse, Text des Operators verschwinden zu lassen — der teurere
    Fehler von beiden."""
    evs = parse_transcript_line(_user("<task-notification>\n<task-id>x</task-id>"))

    assert [e["kind"] for e in evs] == ["message"]
