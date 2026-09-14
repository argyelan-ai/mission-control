"""W5-E-Nacharbeit (2026-09-11): same-card redispatch guard in `_cmd_recover`.

`mc recover` schreibt die aktuelle Attempt-ID der aktiven Karte nach
/tmp/mc-context.env — ein Zombie-Run kann sich damit OHNE PR #511 neu
scharf machen (die Luecke existiert schon auf main; #511 weitet sie
auf `mc ack` aus). Gleiche Fallunterscheidung wie dort:

  Fall (a) HEILEN  — context_task_id != task.id: mein Header sagt nichts
      ueber die Karte, Fortschreibung ist richtig.
  Fall (b) ABLEHNEN — context_task_id == task.id, aber die Attempt-ID
      differiert: DIESE Karte wurde unter mir neu dispatcht
      (poll_orphan_run rotiert die ID bei Status in_progress, selber
      Agent). UsageError statt stiller Identitaetsuebernahme.

`mc park` bleibt bewusst unangetastet — dort ist der Rebind auf die
Ziel-Attempt-ID semantisch berechtigt (Lead-Werkzeug auf fremden Karten).
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _cmd_recover  # noqa: E402
from mc_cli.config import Config  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

CTX_PATH = "/tmp/mc-context.env"

TASK_A = "aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "bbbbbbbb-1111-2222-3333-444444444444"
BOARD_1 = "board-1"


def _payload(task_id=TASK_A, attempt="attempt-A", prompt="TU DIES."):
    return {
        "active": True,
        "task": {
            "id": task_id,
            "board_id": BOARD_1,
            "title": "Karte",
            "status": "in_progress",
            "dispatch_attempt_id": attempt,
            "prompt": prompt,
        },
    }


class _Client:
    def __init__(self, payload):
        self.payload = payload

    def request(self, method, path, body=None, **kw):
        return self.payload


def _cfg(task_id, attempt):
    return Config(api_url="http://test:8000", agent_token="tok",
                  task_id=task_id, board_id=BOARD_1, dispatch_attempt_id=attempt)


@pytest.fixture(autouse=True)
def _ctx_file():
    yield
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)


def _read_ctx() -> dict[str, str]:
    out: dict[str, str] = {}
    if os.path.exists(CTX_PATH):
        with open(CTX_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, _, v = line.partition("=")
                    out[k] = v
    return out


class _Args:
    pass


def test_recover_other_card_advances_context():
    """Fall (a) HEILEN: Kontext gehoert zu Karte A, recover liefert Karte B
    — mein A-Header sagt nichts ueber B, Fortschreibung ist richtig."""
    client = _Client(_payload(task_id=TASK_B, attempt="attempt-B"))
    assert _cmd_recover(_Args(), client, _cfg(TASK_A, "attempt-A")) == 0
    ctx = _read_ctx()
    assert ctx["TASK_ID"] == TASK_B
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == "attempt-B"


def test_recover_same_card_adopted_before_fix_mutation_probe():
    """MUTATIONSPROBE: ohne den Guard adoptiert recover die frische ID.
    Kontext auf Karte A (attempt-OLD-RUN), Backend meldet dieselbe Karte
    mit attempt-NEW-RUN -> ohne Fix stünde attempt-NEW-RUN im Context-File
    und der Zombie-Run schreibt mit gueltigem Header weiter. Der Test
    dokumentiert das Loch, das der Guard schliesst — mit dem Fix wirft
    recover, dieser Pfad ist hier nur historisch; er bleibt bewusst als
    Kontrast stehen und wird NICHT zum SOLL erkoren."""
    client = _Client(_payload(task_id=TASK_A, attempt="attempt-NEW-RUN"))
    # Mit Fix: UsageError — siehe test_recover_same_card_redispatched_rejects.


def test_recover_same_card_redispatched_rejects():
    """Fall (b) ABLEHNEN: ich hielt DIESE Karte (context_task_id == task.id)
    mit gueltigem Header, sie wurde unter mir neu dispatcht — recover darf
    die neue Attempt-ID nicht ins Context-File schreiben (der Zombie-Run
    wuerde sich sonst neu scharf machen). UsageError mit beiden IDs,
    Kontext unangetastet."""
    with open(CTX_PATH, "w", encoding="utf-8") as f:
        f.write(f"TASK_ID={TASK_A}\nBOARD_ID={BOARD_1}\n"
                f"X_DISPATCH_ATTEMPT_ID=attempt-OLD-RUN\n")
    client = _Client(_payload(task_id=TASK_A, attempt="attempt-NEW-RUN"))
    with pytest.raises(UsageError, match="neu dispatcht"):
        _cmd_recover(_Args(), client, _cfg(TASK_A, "attempt-OLD-RUN"))
    ctx = _read_ctx()
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == "attempt-OLD-RUN", (
        "beim Abbruch darf der alte Kontext unangetastet bleiben"
    )


def test_recover_same_card_no_held_header_still_adopts():
    """Sonderfall: gleiche Karte, aber ich halte KEINEN Header
    (dispatch_attempt_id leer) — kein Besitzanspruch, Adoption bleibt
    richtig (Spiegel zu _cmd_ack missing-header-Pfad)."""
    client = _Client(_payload(task_id=TASK_A, attempt="attempt-A"))
    assert _cmd_recover(_Args(), client, _cfg(TASK_A, None)) == 0
    ctx = _read_ctx()
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == "attempt-A"


def test_recover_write_failure_is_loud(monkeypatch):
    """Schreibfehler LAUT (UsageError) statt stiller Warnung — Spiegel zu
    _cmd_ack/_write_context_file."""
    import builtins
    real_open = builtins.open

    def _boom(path, *a, **kw):
        if path == CTX_PATH:
            raise OSError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", _boom)
    client = _Client(_payload(task_id=TASK_B, attempt="attempt-B"))
    with pytest.raises(UsageError, match="mc-context.env"):
        _cmd_recover(_Args(), client, _cfg(TASK_A, "attempt-A"))
