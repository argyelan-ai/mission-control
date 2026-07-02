"""Expected-remote marker file gets written at workspace setup.

Companion to the pre-push hook (docker/mc-agent-base/lib/mc-pre-push.sh)
which reads `<workspace>/.mc-expected-remote` and aborts pushes that
would go to a different remote. If the marker is missing the hook is
silent, so this test guards the write path.
"""
import os

import pytest

from app.services.cli_bridge_runner import _write_expected_remote


def test_write_expected_remote_happy_path(tmp_path):
    _write_expected_remote(str(tmp_path), "https://github.com/test-owner/argyelan.ai.git")

    marker = tmp_path / ".mc-expected-remote"
    assert marker.exists()
    assert marker.read_text().strip() == "https://github.com/test-owner/argyelan.ai.git"


def test_write_expected_remote_strips_whitespace(tmp_path):
    _write_expected_remote(str(tmp_path), "  https://github.com/test-owner/argyelan.ai.git\n")
    content = (tmp_path / ".mc-expected-remote").read_text()
    assert content == "https://github.com/test-owner/argyelan.ai.git\n"


def test_write_expected_remote_overwrites(tmp_path):
    """Re-dispatch of the same task must replace an old marker."""
    _write_expected_remote(str(tmp_path), "https://github.com/test-owner/old.git")
    _write_expected_remote(str(tmp_path), "https://github.com/test-owner/new.git")
    assert (tmp_path / ".mc-expected-remote").read_text().strip() == "https://github.com/test-owner/new.git"


def test_write_expected_remote_handles_oserror(tmp_path, caplog):
    """If the workspace isn't writable, log loud but don't crash dispatch."""
    import logging
    bad_path = str(tmp_path / "does" / "not" / "exist")
    with caplog.at_level(logging.WARNING, logger="mc.cli_bridge"):
        _write_expected_remote(bad_path, "https://github.com/test-owner/x.git")
    # Either the directory-missing error or a permission one — both fine,
    # the point is the call doesn't raise.
    assert any("pre-push guard disabled" in r.message for r in caplog.records)
