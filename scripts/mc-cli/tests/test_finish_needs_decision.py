"""Tests fuer `mc finish --needs-decision "<Frage>"` (Vorfall 11.09.2026).

Der Vorfall: eine Karte war um 05:40 fertig — CI gruen, Beweislauf bestanden,
Reflexion geschrieben. Um 08:30 stand sie immer noch auf `in_progress`, weil
der Worker statt `mc finish` einen Kommentar "Zurueckgestellt, wartet auf
Rollout-Entscheid" gepostet hatte. Ueber `blocked_by_task_id` hing die
Elternkarte dadurch auf `blocked`. Der Worker hat sich nicht falsch verhalten —
"fertig, aber jemand muss entscheiden" war kein Zustand, den die CLI
ausdruecken konnte.

`--needs-decision` schliesst die Karte regulaer UND legt die Frage auf den
Weg an den Operator (offene Thread-Frage, #496) und an die Karte selbst
(Kommentar `needs_decision`). KEIN neuer Status.

Reihenfolge ist Teil des Vertrags und wird hier festgenagelt:
  Preflight → ask → Karten-Kommentar → Reflexion → Status-PATCH
Der ask-Endpunkt loest den Task ueber `agent.current_task_id` auf — nach dem
PATCH auf `done` ist das fuer einen gewoehnlichen Worker ein 409. Die Frage
MUSS also vor dem Statuswechsel raus, sonst verpufft sie.
"""
import datetime as _dt
import os
import sys
from unittest.mock import MagicMock

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli import commands  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402


GOOD_REFLECTION = (
    "## Was wurde gemacht\nRollout-Skript gebaut, CI 6/6 gruen.\n\n"
    "## Was hat funktioniert\nBeweislauf gegen die echte Stack-Instanz.\n\n"
    "## Was war unklar\nOb der Rollout heute oder nach dem Release faehrt.\n\n"
    "## Lesson fuer Agent-Memory\nFertige Arbeit wird geschlossen, nicht geparkt.\n"
)
QUESTION = "Rollout heute abend oder erst nach dem Release am Montag?"

BOARD_ID = "11111111-1111-1111-1111-111111111111"
TASK_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"


class _Args:
    def __init__(self, message=GOOD_REFLECTION, review=False, task_id=None,
                 needs_decision=None, force=False):
        self.message = message
        self.review = review
        self.task_id = task_id
        self.needs_decision = needs_decision
        self.force = force


def _mock_cfg():
    cfg = MagicMock()
    cfg.require_task_context.return_value = (BOARD_ID, TASK_ID)
    return cfg


def _mock_client(responses):
    """responses: list of (method, path_substr, value-or-exception), in order."""
    client = MagicMock()
    calls = []

    def request(method, path, body=None, **kw):
        calls.append({"method": method, "path": path, "body": body})
        for i, (m, p, value) in enumerate(responses):
            if m == method and p in path:
                responses.pop(i)
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(
            f"unmocked request: {method} {path}\nremaining: {responses}"
        )

    client.request.side_effect = request
    client.calls = calls
    return client


def _task(status="in_progress", agent_id=AGENT_ID):
    return {"id": TASK_ID, "status": status, "assigned_agent_id": agent_id}


def _happy_responses(target="done"):
    return [
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/tasks/current/ask", {"message_id": "m1", "thread_id": "t1"}),
        ("POST", "/comments", {"id": "c-question"}),
        ("POST", "/comments", {"id": "c-reflection"}),
        ("PATCH", "/tasks/", {"status": target}),
    ]


def _posts(client):
    return [c for c in client.calls if c["method"] == "POST"]


# ── Eine leere Frage ist schlimmer als keine ───────────────────────────────


@pytest.mark.parametrize("empty", ["", "   ", "\n", "\t  \n"])
def test_empty_question_rejected_before_any_http(empty):
    cfg = _mock_cfg()
    client = MagicMock()
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(_Args(needs_decision=empty), client, cfg)
    assert "--needs-decision" in str(exc.value)
    # Kein einziger HTTP-Call — weder Frage noch Reflexion noch Status.
    client.request.assert_not_called()


def test_empty_question_rejected_even_with_broken_reflection():
    """Die Frage-Pruefung darf die Reflexionspflicht nicht ersetzen — aber
    eine leere Frage bleibt in jedem Fall ein Abbruch."""
    cfg = _mock_cfg()
    client = MagicMock()
    with pytest.raises(UsageError):
        commands._cmd_finish(_Args(message="zu kurz", needs_decision=""), client, cfg)
    client.request.assert_not_called()


def test_reflection_still_mandatory_with_needs_decision():
    """`--needs-decision` ist keine Abkuerzung an der Reflexionspflicht vorbei."""
    cfg = _mock_cfg()
    client = MagicMock()
    with pytest.raises(UsageError) as exc:
        commands._cmd_finish(
            _Args(message="## Was wurde gemacht\nnur ein Feld\n", needs_decision=QUESTION),
            client, cfg,
        )
    assert "unvollstaendig" in str(exc.value)
    client.request.assert_not_called()


# ── Happy path: Frage geht raus, Karte schliesst ───────────────────────────


def test_question_reaches_operator_and_card_then_status_closes():
    cfg = _mock_cfg()
    client = _mock_client(_happy_responses())
    rc = commands._cmd_finish(_Args(needs_decision=QUESTION), client, cfg)
    assert rc == 0

    posts = _posts(client)
    assert len(posts) == 3

    # 1. Der Weg an den Operator (#496): offene Thread-Frage, non-blocking.
    ask = posts[0]
    assert ask["path"].endswith("/tasks/current/ask")
    assert ask["body"]["question"] == QUESTION
    assert ask["body"]["blocking"] is False, "eine schliessende Karte darf nicht parken"
    assert ask["body"]["to"] == "mark"

    # 2. Auffindbar AN der Karte, nicht nur als Meldung.
    card = posts[1]
    assert card["path"].endswith("/comments")
    assert card["body"]["comment_type"] == "needs_decision"
    assert QUESTION in card["body"]["content"]

    # 3. Reflexionspflicht unveraendert.
    refl = posts[2]
    assert refl["body"]["comment_type"] == "reflection"

    # 4. Status-Wechsel zuletzt — sonst kann der ask-Endpunkt den Task nicht
    #    mehr ueber current_task_id aufloesen.
    assert client.calls[-1]["method"] == "PATCH"
    assert client.calls[-1]["body"]["status"] == "done"


def test_needs_decision_combines_with_review():
    cfg = _mock_cfg()
    client = _mock_client(_happy_responses(target="review"))
    rc = commands._cmd_finish(_Args(needs_decision=QUESTION, review=True), client, cfg)
    assert rc == 0
    assert client.calls[-1]["body"]["status"] == "review"
    assert any(c["path"].endswith("/tasks/current/ask") for c in _posts(client))


def test_question_is_stripped_not_trimmed_away():
    cfg = _mock_cfg()
    client = _mock_client(_happy_responses())
    commands._cmd_finish(_Args(needs_decision=f"  {QUESTION}  "), client, cfg)
    assert _posts(client)[0]["body"]["question"] == QUESTION


# ── Ohne das Flag bleibt alles wie vorher ──────────────────────────────────


def test_without_flag_no_ask_call():
    """Regressionsschutz: der normale Abschluss darf keine Frage erzeugen."""
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/comments", {"id": "c-reflection"}),
        ("PATCH", "/tasks/", {"status": "done"}),
    ])
    rc = commands._cmd_finish(_Args(), client, cfg)
    assert rc == 0
    assert not any("/ask" in c["path"] for c in client.calls)
    assert all(c["body"]["comment_type"] == "reflection" for c in _posts(client))


# ── Fehlschlaege hinterlassen keinen Muell ─────────────────────────────────


def test_failing_ask_aborts_before_reflection():
    """Wenn der Weg an den Operator nicht funktioniert, wird die Karte NICHT
    geschlossen — eine stumme Frage ist genau der Fehlermodus, den das Flag
    verhindern soll. Und es bleibt kein halber Audit-Trail zurueck."""
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", []),
        ("POST", "/tasks/current/ask", RuntimeError("HTTP 409: Kein aktiver Task")),
    ])
    with pytest.raises(RuntimeError):
        commands._cmd_finish(_Args(needs_decision=QUESTION), client, cfg)
    assert not any(c["path"].endswith("/comments") and c["method"] == "POST"
                   for c in client.calls)
    assert not any(c["method"] == "PATCH" for c in client.calls)


def test_retry_after_reflection_does_not_duplicate_the_question():
    """Ehrlicher Retry-Pfad: die Reflexion wird NACH der Frage gepostet, also
    beweist eine frische eigene Reflexion, dass die Frage schon raus ist.
    Der zweite `mc finish` darf nur noch den Status setzen."""
    recent = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=30)).isoformat()
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", []),
        ("GET", "/comments", [{
            "comment_type": "reflection",
            "author_type": "agent",
            "author_agent_id": AGENT_ID,
            "created_at": recent,
        }]),
        ("PATCH", "/tasks/", {"status": "done"}),
    ])
    rc = commands._cmd_finish(_Args(needs_decision=QUESTION), client, cfg)
    assert rc == 0
    assert not _posts(client), "kein zweiter ask, kein zweiter Kommentar"


def test_already_closed_card_posts_nothing():
    cfg = _mock_cfg()
    client = _mock_client([("GET", "/detail", _task(status="done"))])
    rc = commands._cmd_finish(_Args(needs_decision=QUESTION), client, cfg)
    assert rc == 0
    assert not any(c["method"] in ("POST", "PATCH") for c in client.calls)


def test_open_checklist_blocks_before_the_question_goes_out():
    cfg = _mock_cfg()
    client = _mock_client([
        ("GET", "/detail", _task()),
        ("GET", "/checklist", [{"id": "i-1", "title": "Beweislauf", "status": "pending"}]),
    ])
    with pytest.raises(UsageError):
        commands._cmd_finish(_Args(needs_decision=QUESTION), client, cfg)
    assert not any(c["method"] == "POST" for c in client.calls)


# ── argparse-Verdrahtung ───────────────────────────────────────────────────


def test_flag_is_wired_and_requires_a_value():
    import argparse

    p = argparse.ArgumentParser(prog="finish")
    commands._add_finish_args(p)
    parsed = p.parse_args([GOOD_REFLECTION, "--needs-decision", QUESTION])
    assert parsed.needs_decision == QUESTION
    # Ohne Flag: None — der normale Abschluss.
    assert p.parse_args([GOOD_REFLECTION]).needs_decision is None
    # Flag ohne Wert ist ein argparse-Fehler, kein stilles "irgendeine Frage".
    with pytest.raises(SystemExit):
        p.parse_args([GOOD_REFLECTION, "--needs-decision"])
