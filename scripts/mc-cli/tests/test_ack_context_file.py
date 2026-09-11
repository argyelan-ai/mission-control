"""W5-E (2026-09-11): `mc ack` must advance /tmp/mc-context.env.

Live bug: Boss closed card 8e677227 via `mc ack 8e677227...` while
/tmp/mc-context.env still pointed at the PREVIOUS card (94fda9f9). The CLI
sent the stale X-Dispatch-Attempt-Id → HTTP 409 "Stale dispatch_attempt_id
— Erwartet: 5fbfe809..., gesendet: 067dc41a...". Worse than a failure: as
long as the old attempt id is still valid, downstream verbs (`mc patch`,
`mc comment`, ...) silently hit the WRONG card.

Fix contract:
1. `mc ack <B>` (context pointing at A) writes TASK_ID=B, BOARD_ID=B's
   board and B's CURRENT dispatch_attempt_id to /tmp/mc-context.env, and a
   directly-following PATCH hits B — with B's attempt id in the header.
2. Write failure is LOUD (UsageError), not a silent stderr warning.
3. Target assigned but never dispatched (dispatch_attempt_id=None): the
   PATCH goes out WITHOUT a stale attempt header and the context file gets
   an empty attempt id — no stale value survives for follow-up calls.
"""
from __future__ import annotations

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _cmd_ack  # noqa: E402
from mc_cli.config import Config  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

CTX_PATH = "/tmp/mc-context.env"

TASK_A = "aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "bbbbbbbb-1111-2222-3333-444444444444"
BOARD_1 = "board-1"
ATTEMPT_A = "attempt-A"
ATTEMPT_B = "attempt-B"


class _Client:
    """Records calls; detail returns the TARGET task's attempt id.

    `_cmd_ack` rebinds the client (`type(client)(_replace(...))`) when the
    target attempt id differs — the rebound instance must keep recording
    into the SAME list as the original. Tests set `_Client.shared` before
    calling `_cmd_ack`; the constructor falls back to it when no explicit
    list is passed.
    """

    shared: list | None = None

    def __init__(self, cfg, attempt_by_task=None, calls=None):
        self.cfg = cfg
        self.calls = calls if calls is not None else (type(self).shared if type(self).shared is not None else [])
        self.attempt_by_task = attempt_by_task or {}

    def request(self, method, path, body=None, **kw):
        self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
        if path.endswith("/detail"):
            tid = path.split("/tasks/")[1].split("/")[0]
            return {
                "id": tid,
                "board_id": BOARD_1,
                "dispatch_attempt_id": self.attempt_by_task.get(tid),
            }
        return {"ok": True}


class _Args:
    task_id = None


def _write_ctx(task_id: str, attempt_id: str) -> None:
    with open(CTX_PATH, "w", encoding="utf-8") as f:
        f.write(f"TASK_ID={task_id}\n")
        f.write(f"BOARD_ID={BOARD_1}\n")
        f.write(f"X_DISPATCH_ATTEMPT_ID={attempt_id}\n")


def _read_ctx() -> dict[str, str]:
    out: dict[str, str] = {}
    with open(CTX_PATH, encoding="utf-8") as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            out[k] = v
    return out


@pytest.fixture(autouse=True)
def _ctx_file():
    yield
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)


def _read_ctx_after_ack_with_followup_patch():
    """DoD: nach `mc ack B` zeigt der Kontext auf B, und ein direkt
    folgender PATCH geht an B — nicht an A."""
    _write_ctx(TASK_A, ATTEMPT_A)
    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id=ATTEMPT_A,
    )
    _Client.shared = calls = []
    client = _Client(cfg, attempt_by_task={TASK_A: ATTEMPT_A, TASK_B: ATTEMPT_B})
    # `mc ack <B>` — explizite ID via positional arg (__main__ wendet
    # with_task_id an; hier direkt, gleiche Semantik).
    rc = _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B))
    assert rc == 0

    ctx = _read_ctx()
    assert ctx["TASK_ID"] == TASK_B, "context must follow the acked card"
    assert ctx["BOARD_ID"] == BOARD_1
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == ATTEMPT_B

    # Der ACK-PATCH selbst traegt B's attempt id (sonst 409 stale):
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    method, path, body, attempt = patch_calls[0]
    assert path == f"/api/v1/agent/boards/{BOARD_1}/tasks/{TASK_B}"
    assert body == {"status": "in_progress"}
    assert attempt == ATTEMPT_B
    return ctx


def test_ack_b_moves_context_from_a_to_b_and_patch_hits_b():
    _read_ctx_after_ack_with_followup_patch()


def test_ack_without_explicit_id_rewrites_same_context():
    """env-style ack (keine positionale ID): Kontext bleibt konsistent,
    Attempt-ID kommt aus dem Backend-Detail, nicht blind aus der Env."""
    _write_ctx(TASK_A, "STALE-FROM-ENV")
    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id="STALE-FROM-ENV",
    )
    client = _Client(cfg, attempt_by_task={TASK_A: ATTEMPT_A})
    assert _cmd_ack(_Args(), client, cfg) == 0
    ctx = _read_ctx()
    assert ctx["TASK_ID"] == TASK_A
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == ATTEMPT_A


def test_ack_patch_header_follows_target_attempt_id():
    """Header-Bindung wie _cmd_park: PATCH muss die Attempt-ID des ZIELS
    tragen — der Ursprung des 409 im Live-Bug."""
    _write_ctx(TASK_A, ATTEMPT_A)
    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id=ATTEMPT_A,
    )
    _Client.shared = calls = []
    client = _Client(cfg, attempt_by_task={TASK_B: ATTEMPT_B})
    _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B))
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert patch_calls[0][3] == ATTEMPT_B

def test_ack_assigned_but_never_dispatched_clears_attempt_and_header():
    """Karte ist mir zugewiesen, aber nicht aktiv dispatcht
    (dispatch_attempt_id=None): PATCH ohne Attempt-Header (das Backend
    akzeptiert fehlenden Header, wenn der Task keinen hat), Context-File
    mit LEERER Attempt-ID — kein stale Wert fuer Folge-Calls."""
    _write_ctx(TASK_A, ATTEMPT_A)
    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id=ATTEMPT_A,
    )
    _Client.shared = calls = []
    client = _Client(cfg, attempt_by_task={TASK_B: None})
    assert _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B)) == 0
    patch_calls = [c for c in calls if c[0] == "PATCH"]
    assert patch_calls[0][3] is None
    ctx = _read_ctx()
    assert ctx["TASK_ID"] == TASK_B
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == ""


def test_ack_idempotent_in_progress_still_advances_context():
    """400 'In Progress -> In Progress' bleibt Idempotent-Success — und
    schreibt den Kontext TROTZDEM fort (sonst bleibt genau der W5-E-Zustand
    bestehen, wenn poll.sh schon geclaimt hat)."""
    _write_ctx(TASK_A, ATTEMPT_A)

    class _AlreadyInProgress(_Client):
        def request(self, method, path, body=None, **kw):
            self.calls.append((method, path, body, self.cfg.dispatch_attempt_id))
            if path.endswith("/detail"):
                tid = path.split("/tasks/")[1].split("/")[0]
                return {
                    "id": tid,
                    "board_id": BOARD_1,
                    "dispatch_attempt_id": self.attempt_by_task.get(tid),
                }
            if method == "PATCH":
                raise RuntimeError(
                    "400: Ungültiger Status-Übergang: In Progress → In Progress"
                )
            return {"ok": True}

    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id=ATTEMPT_A,
    )
    client = _AlreadyInProgress(cfg, attempt_by_task={TASK_B: ATTEMPT_B})
    assert _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B)) == 0
    ctx = _read_ctx()
    assert ctx["TASK_ID"] == TASK_B
    assert ctx["X_DISPATCH_ATTEMPT_ID"] == ATTEMPT_B


def test_context_write_failure_is_loud(monkeypatch):
    """Schreibfehler darf nicht still unterlaufen werden — sonst arbeitet
    der naechste mc-Call auf dem alten Kontext (Kern des W5-E-Bugs)."""
    _write_ctx(TASK_A, ATTEMPT_A)
    cfg = Config(
        api_url="http://test:8000",
        agent_token="tok",
        task_id=TASK_A,
        board_id=BOARD_1,
        dispatch_attempt_id=ATTEMPT_A,
    )
    client = _Client(cfg, attempt_by_task={TASK_B: ATTEMPT_B})

    import builtins

    real_open = builtins.open

    def _boom(path, *a, **kw):
        if path == CTX_PATH:
            raise OSError(13, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", _boom)
    with pytest.raises(UsageError, match="mc-context.env"):
        _cmd_ack(_Args(), client, cfg.with_task_id(TASK_B))
