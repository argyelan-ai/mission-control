"""W5 (2026-09-13): per-agent context env file via MC_CONTEXT_ENV_PATH.

Live incident: poll.sh (host mode), hermes-bridge and grok-bridge all wrote
the SAME /tmp/mc-context.env. Last writer wins set TASK_ID and
X_DISPATCH_ATTEMPT_ID for EVERY host agent — Hermes' card id (4c9bb492) sat
in the file, so another agent's `mc comment progress` landed on HERMES' card.
Nothing breaks, nothing reports: the audit trail is simply wrong.

Contract under test:
1. Config.from_env reads MC_CONTEXT_ENV_PATH when set — the kerntest is the
   TWO-AGENT constellation (different cards): each agent resolves its OWN
   TASK_ID. Without two agents the bug cannot occur at all (a single-agent
   probe passed on 13.09. even while the live bug was active).
2. Without the variable the CLI falls back to the legacy /tmp/mc-context.env
   path — running turns (containers, not-yet-rolled-out bridges) keep working.
3. _write_context_file honours the same resolution and creates per-agent
   files 0600 (attempt ids are not world material); the legacy /tmp path
   keeps its historical 644 (poll.sh chmod 644).
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.commands import _write_context_file  # noqa: E402
from mc_cli.config import DEFAULT_CONTEXT_ENV_PATH, Config, context_env_path  # noqa: E402
from mc_cli.errors import UsageError  # noqa: E402

TASK_A = "aaaaaaaa-1111-2222-3333-444444444444"
TASK_B = "bbbbbbbb-1111-2222-3333-444444444444"
BOARD_A = "board-a"
BOARD_B = "board-b"
ATTEMPT_A = "attempt-A"
ATTEMPT_B = "attempt-B"


def _write_ctx(path, task_id, board_id, attempt_id):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"TASK_ID={task_id}\n")
        f.write(f"BOARD_ID={board_id}\n")
        f.write(f"X_DISPATCH_ATTEMPT_ID={attempt_id}\n")


# ── 1. Kerntest: two host agents, different cards ─────────────────────────────
def test_two_host_agents_resolve_own_task_id(tmp_path, monkeypatch):
    """THE regression: two agents, two cards, two files — no cross-talk.

    Mirrors the live 13.09. constellation (hermes + a second host agent):
    each agent's context file carries its own TASK_ID and each Config.from_env
    must resolve exactly that card — never the other agent's.
    """
    path_a = str(tmp_path / "agents" / "alpha" / "mc-context.env")
    path_b = str(tmp_path / "agents" / "beta" / "mc-context.env")
    _write_ctx(path_a, TASK_A, BOARD_A, ATTEMPT_A)
    _write_ctx(path_b, TASK_B, BOARD_B, ATTEMPT_B)

    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", path_a)
    cfg_a = Config.from_env()
    assert cfg_a.task_id == TASK_A
    assert cfg_a.board_id == BOARD_A
    assert cfg_a.dispatch_attempt_id == ATTEMPT_A

    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", path_b)
    cfg_b = Config.from_env()
    assert cfg_b.task_id == TASK_B
    assert cfg_b.board_id == BOARD_B
    assert cfg_b.dispatch_attempt_id == ATTEMPT_B

    # Agent A re-reads after B wrote its file: still its OWN card (the live
    # bug was exactly this — a concurrent write repointed the shared file).
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", path_a)
    assert Config.from_env().task_id == TASK_A


def test_unset_var_shares_legacy_file_control(tmp_path, monkeypatch):
    """Control documenting the OLD failure mode: without MC_CONTEXT_ENV_PATH
    both agents read the same legacy file — this is what W5 removes."""
    legacy = tmp_path / "legacy.env"
    _write_ctx(str(legacy), TASK_B, BOARD_B, ATTEMPT_B)
    monkeypatch.setattr("mc_cli.config.DEFAULT_CONTEXT_ENV_PATH", str(legacy))
    monkeypatch.delenv("MC_CONTEXT_ENV_PATH", raising=False)
    assert Config.from_env().task_id == TASK_B


# ── 2. Fallback: unset variable → legacy /tmp path ────────────────────────────
def test_unset_var_falls_back_to_legacy_path(monkeypatch):
    monkeypatch.delenv("MC_CONTEXT_ENV_PATH", raising=False)
    assert context_env_path() == DEFAULT_CONTEXT_ENV_PATH == "/tmp/mc-context.env"


def test_set_var_wins_over_legacy(monkeypatch, tmp_path):
    custom = str(tmp_path / "custom.env")
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", custom)
    assert context_env_path() == custom


# ── 3. Writer honours the same resolution ─────────────────────────────────────
def test_write_context_file_custom_path_is_0600(tmp_path, monkeypatch):
    custom = str(tmp_path / "agents" / "grok" / "mc-context.env")
    os.makedirs(os.path.dirname(custom), exist_ok=True)
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", custom)
    returned = _write_context_file(
        task_id=TASK_A, board_id=BOARD_A, attempt_id=ATTEMPT_A
    )
    assert returned == custom
    with open(custom, encoding="utf-8") as f:
        assert f.read() == (
            f"TASK_ID={TASK_A}\n"
            f"BOARD_ID={BOARD_A}\n"
            f"X_DISPATCH_ATTEMPT_ID={ATTEMPT_A}\n"
        )
    assert stat.S_IMODE(os.stat(custom).st_mode) == 0o600


def test_write_context_file_missing_parent_dir_fails_loud(tmp_path, monkeypatch):
    """Path WITHOUT existing parent dir fails LOUD (UsageError) — silently
    working on the old context is exactly the W5-E bug class."""
    custom = str(tmp_path / "missing-dir" / "mc-context.env")
    monkeypatch.setenv("MC_CONTEXT_ENV_PATH", custom)
    with pytest.raises(UsageError):
        _write_context_file(task_id=TASK_A, board_id=BOARD_A, attempt_id=ATTEMPT_A)


def test_write_context_file_legacy_path_keeps_mode(tmp_path, monkeypatch):
    """Legacy path: no chmod surprise — poll.sh historically sets 644 there."""
    legacy = tmp_path / "legacy.env"
    legacy.write_text("")
    os.chmod(legacy, 0o644)
    monkeypatch.setattr("mc_cli.config.DEFAULT_CONTEXT_ENV_PATH", str(legacy))
    monkeypatch.delenv("MC_CONTEXT_ENV_PATH", raising=False)
    _write_context_file(task_id=TASK_A, board_id=BOARD_A, attempt_id=ATTEMPT_A)
    assert stat.S_IMODE(os.stat(legacy).st_mode) == 0o644
