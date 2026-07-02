import json
import os
import tempfile
from pathlib import Path
from app.services.vault_cleanup_state import VaultCleanupState, _default_root


def test_default_root_prefers_home_host(monkeypatch):
    """HOME_HOST (the host-mounted path) must win over the container's
    expanduser('~'). Backend runs as mcuser (HOME=/home/mcuser) but the vault
    data is bind-mounted at HOME_HOST=/Users/testuser — see feedback_home_host_pattern."""
    monkeypatch.setenv("HOME_HOST", "/host-home")
    assert _default_root() == Path("/host-home/.mc/vault.cleanup.state")


def test_default_root_falls_back_to_expanduser_without_home_host(monkeypatch):
    """No HOME_HOST (e.g. host-side execution) → expanduser('~') is correct."""
    monkeypatch.delenv("HOME_HOST", raising=False)
    assert _default_root() == Path(os.path.expanduser("~")) / ".mc" / "vault.cleanup.state"


def test_state_dir_is_created_idempotently(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    state.ensure()  # second call is a no-op
    assert (tmp_path / "run.log").exists()


def test_run_id_is_stable_within_run(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    first = state.run_id
    second = state.run_id
    assert first == second


def test_checkpoint_set_and_get(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    state.set_checkpoint("title-backfill", "memory/notes/abc.md")
    assert state.get_checkpoint("title-backfill") == "memory/notes/abc.md"


def test_load_whitelist_returns_empty_when_missing(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    assert state.whitelist() == set()


def test_load_whitelist_strips_comments_and_blanks(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    (tmp_path / "whitelist.txt").write_text(
        "# comment line\n"
        "  memory/notes/keep-me.md  \n"
        "\n"
        "memory/notes/also-keep.md\n"
    )
    assert state.whitelist() == {
        "memory/notes/keep-me.md",
        "memory/notes/also-keep.md",
    }


def test_log_appends_iso_timestamped_lines(tmp_path):
    state = VaultCleanupState(root=tmp_path)
    state.ensure()
    state.log("INFO", "started")
    state.log("WARN", "almost done")
    content = (tmp_path / "run.log").read_text()
    assert "INFO  started" in content
    assert "WARN  almost done" in content
