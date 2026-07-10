#!/usr/bin/env python3
"""Context-economy Stage 2 — wrap_prompt's CARD.md/SOUL.md identity prepend.

Unlike the claude/openclaude harnesses (persistent process, SOUL/CARD
injected once via --append-system-prompt at start, see
docker/mc-agent-base/start-claude.sh), the omp native TUI is relaunched per
task and driven purely through injected prompts — bridge.py has no
--append-system-prompt equivalent. wrap_prompt therefore prepends the
identity content itself: CARD.md when the context-economy Stage 2 opt-in
(agent.use_operating_card) wrote one, else SOUL.md — file existence is the
only branch, mirroring the shell-script consumers' `[ -f CARD.md ] ||
CARD_FILE=SOUL.md` pattern.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_DIR = os.path.dirname(HERE)
sys.path.insert(0, BRIDGE_DIR)

import bridge  # noqa: E402
from bridge import COMPLETION_INSTRUCTIONS, wrap_prompt  # noqa: E402


def _home_with(tmp_path, **files):
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (claude_dir / name).write_text(content, encoding="utf-8")
    return str(tmp_path)


def test_wrap_prompt_prefers_card_over_soul(tmp_path):
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT", "SOUL.md": "SOUL CONTENT"})
    result = wrap_prompt("do the task", home_dir=home)
    assert "CARD CONTENT" in result
    assert "SOUL CONTENT" not in result
    assert result.endswith(COMPLETION_INSTRUCTIONS)
    assert "do the task" in result


def test_wrap_prompt_falls_back_to_soul_when_no_card(tmp_path):
    home = _home_with(tmp_path, **{"SOUL.md": "SOUL CONTENT ONLY"})
    result = wrap_prompt("do the task", home_dir=home)
    assert "SOUL CONTENT ONLY" in result
    assert "do the task" in result


def test_wrap_prompt_works_with_neither_file_present(tmp_path):
    home = _home_with(tmp_path)
    result = wrap_prompt("do the task", home_dir=home)
    assert result == "do the task" + COMPLETION_INSTRUCTIONS


def test_wrap_prompt_still_appends_completion_instructions_with_card(tmp_path):
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT"})
    result = wrap_prompt("do the task", home_dir=home)
    assert result.endswith(COMPLETION_INSTRUCTIONS)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
