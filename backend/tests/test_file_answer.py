"""Tests for file-answer endpoint and CLI command."""
import hashlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MC_CLI_PATH = REPO_ROOT / "scripts" / "mc-cli"
if str(MC_CLI_PATH) not in sys.path:
    sys.path.insert(0, str(MC_CLI_PATH))

from app.services.vault_log import VaultLog


class TestFileAnswerCli:
    """Tests for mc file-answer CLI command."""

    def test_creates_correct_body(self):
        from mc_cli.commands import _cmd_file_answer

        client = MagicMock()
        client.request.return_value = {"ok": True, "expected_target": "agents/r/knowledge/x.md"}

        class Args:
            query = "How does rate limiting work at xAI?"
            answer = "xAI uses a token-bucket system with 10 RPM."
            sources = "uuid1,uuid2"
            type = "knowledge"
            tags = "xai,rate-limiting"

        result = _cmd_file_answer(Args(), client, {})
        assert result == 0
        call_args = client.request.call_args
        assert call_args[0] == ("POST", "/api/v1/agent/vault/file-answer")
        body = call_args[1]["body"]
        assert body["query"] == "How does rate limiting work at xAI?"
        assert body["answer"] == "xAI uses a token-bucket system with 10 RPM."
        assert body["source_note_ids"] == ["uuid1", "uuid2"]
        assert body["type"] == "knowledge"
        assert body["tags"] == ["xai", "rate-limiting"]

    def test_empty_query_returns_error(self):
        from mc_cli.commands import _cmd_file_answer

        client = MagicMock()

        class Args:
            query = ""
            answer = "some answer"
            sources = None
            type = "knowledge"
            tags = None

        result = _cmd_file_answer(Args(), client, {})
        assert result == 2
        client.request.assert_not_called()

    def test_empty_answer_returns_error(self):
        from mc_cli.commands import _cmd_file_answer

        client = MagicMock()

        class Args:
            query = "Some question"
            answer = ""
            sources = None
            type = "knowledge"
            tags = None

        result = _cmd_file_answer(Args(), client, {})
        assert result == 2
        client.request.assert_not_called()

    def test_no_sources_omits_field(self):
        from mc_cli.commands import _cmd_file_answer

        client = MagicMock()
        client.request.return_value = {"ok": True}

        class Args:
            query = "Question"
            answer = "Answer with enough content here."
            sources = None
            type = "knowledge"
            tags = None

        _cmd_file_answer(Args(), client, {})
        body = client.request.call_args[1]["body"]
        assert "source_note_ids" not in body
