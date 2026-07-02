"""Test: file-context überschreibt stale env-vars in mc CLI config.

Szenario das Davinci 2026-05-10 erwischt hat (cf319ff1):
- Tmux-Window hat TASK_ID/BOARD_ID aus dem letzten Dispatch im Env
- poll.sh schreibt /tmp/mc-context.env mit der NEUEN task_id beim neuen
  Dispatch, aber die alte Env-Var bleibt im Window-Scope gesetzt
- Vorheriger Code: env wins → mc CLI nutzt stale TASK_ID → 404/400
- Fix: file wins → CLI nutzt immer die per-dispatch frische ID
"""
import os
import sys
import tempfile
import textwrap

import pytest

# Skript-Pfad zum sys.path hinzufügen
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.config import Config  # noqa: E402


def _write_ctx_file(path: str, mapping: dict) -> None:
    with open(path, "w") as f:
        for k, v in mapping.items():
            f.write(f"{k}={v}\n")


CTX_PATH = "/tmp/mc-context.env"


def _cleanup():
    if os.path.exists(CTX_PATH):
        os.remove(CTX_PATH)


def test_file_context_overrides_stale_env(monkeypatch):
    """Stale env TASK_ID darf nicht über frisches /tmp/mc-context.env gewinnen."""
    _write_ctx_file(
        CTX_PATH,
        {
            "TASK_ID": "fresh-task-from-file",
            "BOARD_ID": "fresh-board-from-file",
            "X_DISPATCH_ATTEMPT_ID": "fresh-attempt",
        },
    )
    try:
        monkeypatch.setenv("TASK_ID", "stale-task-from-env")
        monkeypatch.setenv("BOARD_ID", "stale-board-from-env")
        monkeypatch.setenv("MC_AGENT_TOKEN", "tok")

        cfg = Config.from_env()
        assert cfg.task_id == "fresh-task-from-file"
        assert cfg.board_id == "fresh-board-from-file"
        assert cfg.dispatch_attempt_id == "fresh-attempt"
    finally:
        _cleanup()


def test_env_used_when_no_file(monkeypatch):
    """Wenn /tmp/mc-context.env nicht existiert: env ist Fallback."""
    _cleanup()
    monkeypatch.setenv("TASK_ID", "env-only-task")
    monkeypatch.setenv("BOARD_ID", "env-only-board")
    monkeypatch.setenv("MC_AGENT_TOKEN", "tok")

    cfg = Config.from_env()
    assert cfg.task_id == "env-only-task"
    assert cfg.board_id == "env-only-board"
