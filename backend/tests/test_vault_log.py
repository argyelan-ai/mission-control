"""Tests for vault_log.py — append-only activity log with rotation."""
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.vault_log import VaultLog, MAX_LINES


class TestVaultLog:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.vault_path = Path(self.tmpdir)
        self.log = VaultLog(self.vault_path)

    def test_append_creates_log_file(self):
        """First append creates log.md if it does not exist."""
        self.log.append("write", "Rate Limiting bei xAI", "researcher")
        log_path = self.vault_path / "log.md"
        assert log_path.exists()
        content = log_path.read_text()
        assert "write" in content
        assert "Rate Limiting bei xAI" in content
        assert "researcher" in content

    def test_append_format_matches_spec(self):
        """Each line matches: ## [YYYY-MM-DD HH:MM] action | title | agent"""
        self.log.append("promote", "Docker Pattern", "system")
        content = (self.vault_path / "log.md").read_text()
        lines = [l for l in content.strip().split("\n") if l.startswith("## [")]
        assert len(lines) == 1
        line = lines[0]
        assert "promote" in line
        assert "Docker Pattern" in line
        assert "system" in line
        # Verify timestamp format
        assert line.startswith("## [20")
        assert "] promote | " in line

    def test_multiple_appends_preserve_order(self):
        """Appends are chronological (newest last)."""
        self.log.append("write", "Note A", "boss")
        self.log.append("promote", "Note A", "system")
        self.log.append("decay", "Note B", "system")
        content = (self.vault_path / "log.md").read_text()
        lines = [l for l in content.strip().split("\n") if l.startswith("## [")]
        assert len(lines) == 3
        assert "write" in lines[0]
        assert "promote" in lines[1]
        assert "decay" in lines[2]

    def test_rotation_on_max_lines(self):
        """When log.md exceeds MAX_LINES, old content rotates to log-YYYY-MM.md."""
        # Write MAX_LINES + 10 entries
        for i in range(MAX_LINES + 10):
            self.log.append("write", f"Note {i}", "system")
        log_path = self.vault_path / "log.md"
        content = log_path.read_text()
        lines = [l for l in content.strip().split("\n") if l.startswith("## [")]
        # Current log should have only the overflow entries
        assert len(lines) <= MAX_LINES
        # An archive file should exist
        archive_files = list(self.vault_path.glob("log-*.md"))
        assert len(archive_files) >= 1

    def test_rotation_archive_filename(self):
        """Archive filename is log-YYYY-MM.md based on current month."""
        for i in range(MAX_LINES + 5):
            self.log.append("write", f"Note {i}", "system")
        now = datetime.now(timezone.utc)
        expected_name = f"log-{now.strftime('%Y-%m')}.md"
        assert (self.vault_path / expected_name).exists()

    def test_pipe_chars_in_title_escaped(self):
        """Pipe characters in title are replaced to avoid format corruption."""
        self.log.append("write", "Title | with | pipes", "boss")
        content = (self.vault_path / "log.md").read_text()
        # The title should not break the 3-field format
        lines = [l for l in content.strip().split("\n") if l.startswith("## [")]
        assert len(lines) == 1
        # Pipe in title replaced with dash
        assert "Title - with - pipes" in lines[0]

    def test_concurrent_safe_no_corruption(self):
        """Multiple rapid appends do not corrupt the file (sequential test)."""
        for i in range(50):
            self.log.append("write", f"Rapid {i}", "system")
        content = (self.vault_path / "log.md").read_text()
        lines = [l for l in content.strip().split("\n") if l.startswith("## [")]
        assert len(lines) == 50

    def test_empty_title_uses_placeholder(self):
        """Empty or whitespace-only title uses '(untitled)'."""
        self.log.append("lint", "", "system")
        content = (self.vault_path / "log.md").read_text()
        assert "(untitled)" in content

    def test_log_does_not_raise_on_permission_error(self):
        """Log operations fail silently (never crash callers)."""
        # Make vault_path read-only
        os.chmod(self.tmpdir, 0o444)
        try:
            # Should not raise
            self.log.append("write", "Should not crash", "system")
        finally:
            os.chmod(self.tmpdir, 0o755)
