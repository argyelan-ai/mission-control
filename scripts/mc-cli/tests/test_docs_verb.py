"""Tests for `mc docs` — the first purely local mc-cli verb (no network,
no client/config context needed). Context-economy Stage 1.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from mc_cli.__main__ import main  # noqa: E402
from mc_cli.commands import REGISTRY  # noqa: E402


def _write_doc(tmp_path, topic: str, content: str):
    (tmp_path / f"{topic}.md").write_text(content, encoding="utf-8")


def test_docs_is_registered():
    assert "docs" in REGISTRY
    assert REGISTRY["docs"].endpoints == ()


def test_docs_reads_topic_file(tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n\nSend reports here.\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs", "telegram"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "# Telegram Reports" in out
    assert "Send reports here." in out


def test_docs_without_arg_prints_index(tmp_path, monkeypatch, capsys):
    (tmp_path / "INDEX.md").write_text("# Reference Docs Index\n\n| Topic |\n", encoding="utf-8")
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Reference Docs Index" in out


def test_docs_without_arg_falls_back_to_topic_list_when_no_index(tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    _write_doc(tmp_path, "vault", "# Vault\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "mc docs telegram" in out
    assert "mc docs vault" in out


def test_docs_missing_topic_is_friendly_error(tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs", "does-not-exist"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "does-not-exist" in err
    assert "telegram" in err  # available topics listed as a hint


def test_docs_no_dir_at_all_is_friendly_error(tmp_path, monkeypatch, capsys):
    empty_dir = tmp_path / "nope"
    monkeypatch.setenv("MC_DOCS_DIR", str(empty_dir))

    rc = main(["docs"])

    assert rc == 1
    err = capsys.readouterr().err
    assert "Keine Reference Docs gefunden" in err


def test_docs_does_not_require_token_or_task_context(tmp_path, monkeypatch, capsys):
    """`mc docs` must work with zero env — no MC_AGENT_TOKEN, no TASK_ID."""
    _write_doc(tmp_path, "memory", "# Memory-First Protocol\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))
    monkeypatch.delenv("MC_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("TASK_ID", raising=False)
    monkeypatch.delenv("BOARD_ID", raising=False)

    rc = main(["docs", "memory"])

    assert rc == 0
    assert "Memory-First Protocol" in capsys.readouterr().out


# ── Resolution order: MC_DOCS_DIR > $CLAUDE_CONFIG_DIR/docs > ~/.claude/docs ─
#
# Host agents (Boss/Hermes/Jarvis — Boss is the main consumer of the
# delegation doc!) run with CLAUDE_CONFIG_DIR=<agent_dir>/claude-config and
# HOME=/Users/Henry (the operator's real home, not the agent's). A `_docs_dir()`
# hardcoded to `Path.home()/".claude"/"docs"` looks in the wrong place for
# them entirely — docker-bridge agents don't set CLAUDE_CONFIG_DIR and keep
# using ~/.claude/docs (their real container home), but host agents do and
# must be routed to it.

def test_docs_uses_claude_config_dir_when_set(tmp_path, monkeypatch, capsys):
    """Without MC_DOCS_DIR, a host agent's CLAUDE_CONFIG_DIR/docs must be used."""
    config_dir = tmp_path / "claude-config"
    docs_dir = config_dir / "docs"
    docs_dir.mkdir(parents=True)
    _write_doc(docs_dir, "delegation", "# Delegation Pattern\n\nOrchestrator-only.\n")

    monkeypatch.delenv("MC_DOCS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

    rc = main(["docs", "delegation"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "# Delegation Pattern" in out
    assert "Orchestrator-only." in out


def test_docs_mc_docs_dir_wins_over_claude_config_dir(tmp_path, monkeypatch, capsys):
    """MC_DOCS_DIR (explicit override, e.g. for tests) beats CLAUDE_CONFIG_DIR."""
    config_dir = tmp_path / "claude-config"
    (config_dir / "docs").mkdir(parents=True)
    _write_doc(config_dir / "docs", "telegram", "# WRONG — from CLAUDE_CONFIG_DIR\n")

    override_dir = tmp_path / "override"
    override_dir.mkdir()
    _write_doc(override_dir, "telegram", "# RIGHT — from MC_DOCS_DIR\n")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MC_DOCS_DIR", str(override_dir))

    rc = main(["docs", "telegram"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "RIGHT — from MC_DOCS_DIR" in out
    assert "WRONG" not in out


def test_docs_falls_back_to_home_claude_docs_without_claude_config_dir(tmp_path, monkeypatch, capsys):
    """Docker cli-bridge agents don't set CLAUDE_CONFIG_DIR — fall back to
    ~/.claude/docs (their real container home)."""
    fake_home_docs = tmp_path / "home" / ".claude" / "docs"
    fake_home_docs.mkdir(parents=True)
    _write_doc(fake_home_docs, "vault", "# Vault Writing Discipline\n")

    monkeypatch.delenv("MC_DOCS_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    rc = main(["docs", "vault"])

    assert rc == 0
    assert "# Vault Writing Discipline" in capsys.readouterr().out


# ── Topic validation (no path traversal / absolute paths) ────────────────

def test_docs_rejects_path_traversal_topic(tmp_path, monkeypatch, capsys):
    """`mc docs ../../../etc/passwd`-style topics must be rejected before
    ever touching the filesystem — not silently resolved outside docs_dir."""
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    secret = tmp_path.parent / "secret.md"
    secret.write_text("TOP SECRET\n", encoding="utf-8")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs", "../secret"])

    assert rc == 1
    out_err = capsys.readouterr()
    assert "TOP SECRET" not in out_err.out
    assert "TOP SECRET" not in out_err.err
    assert "telegram" in out_err.err  # available topics still listed as a hint


def test_docs_rejects_absolute_path_topic(tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    outside = tmp_path.parent / "outside.md"
    outside.write_text("OUTSIDE CONTENT\n", encoding="utf-8")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs", str(outside.with_suffix(""))])

    assert rc == 1
    out_err = capsys.readouterr()
    assert "OUTSIDE CONTENT" not in out_err.out
    assert "OUTSIDE CONTENT" not in out_err.err


def test_docs_rejects_topic_with_uppercase_or_symbols(tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "telegram", "# Telegram Reports\n")
    monkeypatch.setenv("MC_DOCS_DIR", str(tmp_path))

    rc = main(["docs", "Telegram;rm"])

    assert rc == 1


def test_docs_ignores_claude_config_dir_without_docs_subdir(tmp_path, monkeypatch, capsys):
    """CLAUDE_CONFIG_DIR set but its docs/ subdir doesn't exist (e.g. agent
    not yet synced) — falls through to ~/.claude/docs rather than a
    permanent dead end."""
    config_dir = tmp_path / "claude-config-empty"
    config_dir.mkdir()  # no docs/ subdir inside

    fake_home_docs = tmp_path / "home" / ".claude" / "docs"
    fake_home_docs.mkdir(parents=True)
    _write_doc(fake_home_docs, "memory", "# Memory-First Protocol\n")

    monkeypatch.delenv("MC_DOCS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    rc = main(["docs", "memory"])

    assert rc == 0
    assert "# Memory-First Protocol" in capsys.readouterr().out
