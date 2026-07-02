"""Tests for mc remember CLI command."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))

from mc_cli.commands import _cmd_remember, _add_remember_args


class FakeArgs:
    """Minimal args namespace for testing."""
    def __init__(self, text, content=None, type="lesson", tags=None, related=None, task_id=None):
        self.text = text
        self.content = content
        self.type = type
        self.tags = tags
        self.related = related
        self.task_id = task_id


class TestMcRemember:
    def test_minimal_call_sends_correct_body(self):
        """mc remember 'some text' -> POST with title from text, type=lesson."""
        client = MagicMock()
        client.request.return_value = {"ok": True, "envelope": "test.md"}
        cfg = {}

        args = FakeArgs(text="Rate limiting bei xAI ist 10 RPM")
        result = _cmd_remember(args, client, cfg)

        assert result == 0
        call_args = client.request.call_args
        assert call_args[0] == ("POST", "/api/v1/agent/vault/note")
        body = call_args[1]["body"]
        assert body["content"] == "Rate limiting bei xAI ist 10 RPM"
        assert body["type"] == "lesson"
        assert body["title"] == "Rate limiting bei xAI ist 10 RPM"
        assert "idempotency_key" in body

    def test_explicit_content_uses_text_as_title(self):
        """mc remember 'Title' --content 'body' -> title=Title, content=body."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        args = FakeArgs(text="Auth Token Rotation", content="Tokens muessen alle 90 Tage rotiert werden")
        result = _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["title"] == "Auth Token Rotation"
        assert body["content"] == "Tokens muessen alle 90 Tage rotiert werden"

    def test_tags_parsed_correctly(self):
        """--tags 'docker,scopes' -> tags list."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        args = FakeArgs(text="Test", tags="docker,scopes,restart")
        result = _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["tags"] == ["docker", "scopes", "restart"]

    def test_auto_idempotency_key_from_content(self):
        """Idempotency key is deterministic hash of content."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        content = "Rate limiting bei xAI ist 10 RPM"
        expected_key = "remember-" + hashlib.sha256(content.encode()).hexdigest()[:16]

        args = FakeArgs(text=content)
        _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["idempotency_key"] == expected_key

    def test_task_id_from_env(self):
        """$TASK_ID env var auto-populates task_id."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        args = FakeArgs(text="Test")
        with patch.dict(os.environ, {"TASK_ID": "abc-123-def"}):
            _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["task_id"] == "abc-123-def"

    def test_task_id_arg_overrides_env(self):
        """Explicit --task-id overrides $TASK_ID."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        args = FakeArgs(text="Test", task_id="explicit-id")
        with patch.dict(os.environ, {"TASK_ID": "env-id"}):
            _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["task_id"] == "explicit-id"

    def test_long_title_truncated(self):
        """Title auto-generated from text is capped at 60 chars."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        long_text = "A" * 100
        args = FakeArgs(text=long_text)
        _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert len(body["title"]) <= 63  # 60 + "..."

    def test_empty_text_returns_error(self):
        """Empty text returns exit code 2."""
        client = MagicMock()
        cfg = {}

        args = FakeArgs(text="")
        result = _cmd_remember(args, client, cfg)
        assert result == 2
        client.request.assert_not_called()

    def test_related_notes_parsed(self):
        """--related '[[note-a]],[[note-b]]' -> related_notes list."""
        client = MagicMock()
        client.request.return_value = {"ok": True}
        cfg = {}

        args = FakeArgs(text="Test", related="[[note-a]],[[note-b]]")
        _cmd_remember(args, client, cfg)

        body = client.request.call_args[1]["body"]
        assert body["related_notes"] == ["[[note-a]]", "[[note-b]]"]
