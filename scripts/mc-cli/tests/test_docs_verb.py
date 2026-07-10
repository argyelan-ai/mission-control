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
