"""The chat session says it is the chat (round-1 user test t1, MUST-1).

An omp agent's chat runs in its own `omp acp` child; the chat daemon starts
it with ``MC_SESSION_ROLE=chat`` (docker/omp-bridge/acp_chat.py). Every `mc`
call from there carries ``X-MC-Session-Role: chat``, and the server refuses
writes to the run state of the agent's own task from it
(backend app/services/chat_session_guard.py). A run session has no such
variable — its requests stay byte-identical to before.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.client import Client  # noqa: E402
from mc_cli.config import Config  # noqa: E402


def _headers(monkeypatch, role: str | None) -> dict:
    monkeypatch.setenv("MC_AGENT_TOKEN", "tok")
    if role is None:
        monkeypatch.delenv("MC_SESSION_ROLE", raising=False)
    else:
        monkeypatch.setenv("MC_SESSION_ROLE", role)
    return Client(Config.from_env())._headers()


def test_chat_session_sends_the_role_header(monkeypatch):
    assert _headers(monkeypatch, "chat")["X-MC-Session-Role"] == "chat"


def test_run_session_sends_no_role_header(monkeypatch):
    assert "X-MC-Session-Role" not in _headers(monkeypatch, None)


def test_an_unknown_role_is_not_sent(monkeypatch):
    """Only the one role the server knows goes out — a typo must not turn
    into a header the server reads as something else."""
    assert "X-MC-Session-Role" not in _headers(monkeypatch, "boss")
