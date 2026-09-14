"""REX-Review-Proben zu PR #511 (W5-E), 11.09.2026.

Kernfrage des Auftrags: hebelt die Header-Adoption in `_cmd_ack` die
serverseitige Stale-Pruefung aus?

Der Fix holt per Detail-GET die AKTUELLE dispatch_attempt_id des Ziels und
sendet genau die als Header. Der Serverguard lautet:

    if task.dispatch_attempt_id and _req_attempt_id != task.dispatch_attempt_id:
        -> 409

Wer den erwarteten Wert unmittelbar vorher liest, kann per Konstruktion nie
mehr danebenliegen. Entscheidend ist deshalb nicht OB adoptiert wird,
sondern WANN: der Client hat die Information, um die zwei Faelle zu trennen.

  Fall (a) HEILEN  — ctx.task_id != ziel.task_id
      Mein Kontext gehoert zu einer ANDEREN Karte. Meine Attempt-ID war nie
      eine Aussage ueber das Ziel. Adoptieren ist richtig (der Live-Bug).

  Fall (b) ABLEHNEN — ctx.task_id == ziel.task_id, aber Attempt differiert
      Ich hielt DIESE Karte und sie wurde seither neu dispatcht. Genau der
      Zustand, den der Guard meldet: "Der Task wurde neu dispatcht — dein
      Update stammt von einem alten Run." Adoptieren heisst: der alte Run
      uebernimmt die Identitaet des neuen.

Der Fix unterscheidet nicht — er vergleicht nur die Attempt-IDs, nie die
Task-IDs.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _cmd_ack  # noqa: E402
from mc_cli.config import Config  # noqa: E402

CTX_PATH = "/tmp/mc-context.env"
TASK_A = "aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "bbbbbbbb-1111-2222-3333-444444444444"
BOARD_1 = "board-1"


class _Client:
    shared: list | None = None

    def __init__(self, cfg, attempt_by_task=None, calls=None):
        self.cfg = cfg
        self.calls = calls if calls is not None else (
            type(self).shared if type(self).shared is not None else []
        )
        self.attempt_by_task = attempt_by_task or {}

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
        if path.endswith("/detail"):
            tid = path.split("/tasks/")[1].split("/")[0]
            return {"id": tid, "board_id": BOARD_1,
                    "dispatch_attempt_id": self.attempt_by_task.get(tid)}
        return {"ok": True}


class _Args:
    task_id = None


@pytest.fixture(autouse=True)
def _ctx_file():
    yield
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)


def _ctx(task_id, attempt):
    with open(CTX_PATH, "w", encoding="utf-8") as f:
        f.write(f"TASK_ID={task_id}\nBOARD_ID={BOARD_1}\n"
                f"X_DISPATCH_ATTEMPT_ID={attempt}\n")


def _cfg(task_id, attempt):
    return Config(api_url="http://test:8000", agent_token="tok",
                  task_id=task_id, board_id=BOARD_1, dispatch_attempt_id=attempt)


# ── Fall (a): heilen — das ist der Live-Bug, hier ist Adoption richtig ──

def test_probe_case_a_context_points_at_other_card_adoption_is_correct():
    """ctx zeigt auf A, geackt wird B. Die Attempt-ID von A sagt nichts ueber
    B aus -> Adoption der B-ID ist die richtige Heilung. Diese Probe muss
    GRUEN sein; sie dokumentiert, was der Fix richtig macht."""
    _ctx(TASK_A, "attempt-A")
    _Client.shared = calls = []
    client = _Client(_cfg(TASK_A, "attempt-A"),
                     attempt_by_task={TASK_A: "attempt-A", TASK_B: "attempt-B"})
    assert _cmd_ack(_Args(), client, _cfg(TASK_A, "attempt-A").with_task_id(TASK_B)) == 0
    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[3] == "attempt-B"


# ── Fall (b): ablehnen — DIESE Karte wurde unter mir neu dispatcht ──

def test_probe_case_b_same_card_redispatched_under_me_must_not_adopt():
    """BEFUND. Szenario poll_orphan_run (agents.py:_maybe_redispatch_orphaned_run):
    Karte galt als verwaist, bekam eine FRISCHE dispatch_attempt_id, blieb
    in_progress und beim SELBEN Agenten. Der alte Run lebt noch und ruft
    `mc ack` auf seine eigene Karte.

    ctx.task_id == ziel.task_id, aber die Attempt-ID hat sich geaendert ->
    der Guard wuerde 409 melden. Der Fix adoptiert stattdessen die neue ID.
    """
    _ctx(TASK_A, "attempt-OLD-RUN")
    _Client.shared = calls = []
    cfg = _cfg(TASK_A, "attempt-OLD-RUN")
    client = _Client(cfg, attempt_by_task={TASK_A: "attempt-NEW-RUN"})
    _cmd_ack(_Args(), client, cfg)

    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[3] == "attempt-OLD-RUN", (
        "Der alte Run hat die Attempt-ID des NEUEN Runs adoptiert "
        f"(gesendet: {patch[3]!r}). Auf der eigenen, unter einem neu "
        "dispatcht — hier muss die CLI ablehnen statt zu adoptieren, "
        "sonst laeuft die serverseitige Pruefung fuer diesen Pfad leer."
    )


def test_probe_case_b_poisons_context_for_all_following_calls():
    """BEFUND, Folgeschaden. Nach dem adoptierenden ACK steht die NEUE
    Attempt-ID in /tmp/mc-context.env — der alte Run ist damit nicht nur
    fuer einen Call, sondern fuer seine gesamte Restlaufzeit wieder
    schreibberechtigt (`mc comment`, `mc patch`, `mc done`)."""
    _ctx(TASK_A, "attempt-OLD-RUN")
    cfg = _cfg(TASK_A, "attempt-OLD-RUN")
    _Client.shared = []
    client = _Client(cfg, attempt_by_task={TASK_A: "attempt-NEW-RUN"})
    _cmd_ack(_Args(), client, cfg)

    with open(CTX_PATH, encoding="utf-8") as f:
        ctx = dict(l.strip().partition("=")[::2] for l in f if "=" in l)
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == "attempt-OLD-RUN", (
        f"Context-File traegt jetzt {ctx['X_DISPATCH_ATTEMPT_ID']!r} — der "
        "alte Run schreibt ab hier mit gueltigem Header weiter."
    )


def test_probe_write_failure_is_loud_against_real_filesystem():
    """Kontrollprobe zur Lautstaerke OHNE monkeypatch von builtins.open:
    echter OSError aus dem Dateisystem (Pfad ist ein Verzeichnis)."""
    from mc_cli.errors import UsageError
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)
    os.mkdir(CTX_PATH)
    try:
        cfg = _cfg(TASK_A, "attempt-A")
        client = _Client(cfg, attempt_by_task={TASK_B: "attempt-B"})
        with pytest.raises(UsageError, match="mc-context.env"):
            _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B))
    finally:
        os.rmdir(CTX_PATH)
