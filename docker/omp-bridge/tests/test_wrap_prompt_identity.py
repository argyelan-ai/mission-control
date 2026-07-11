#!/usr/bin/env python3
"""Context-economy Stage 2 — wrap_prompt's CARD.md identity prepend.

Unlike the claude/openclaude harnesses (persistent process, SOUL/CARD
injected once via --append-system-prompt at start, see
docker/mc-agent-base/start-claude.sh), the omp native TUI is relaunched per
task and driven purely through injected prompts — bridge.py has no
--append-system-prompt equivalent. wrap_prompt therefore prepends CARD.md
itself when the context-economy Stage 2 opt-in (agent.use_operating_card)
wrote one.

Deliberately CARD-ONLY, no SOUL.md fallback (review fix, CRITICAL): omp is
opt-IN. Without CARD.md the prompt must be byte-identical to the pre-feature
behaviour — no SOUL.md ever gets read here, even if it happens to sit on
disk (docker_agent_sync writes it for every agent regardless of runtime).
Injecting the full ~29KB SOUL.md into every dispatched prompt for every
non-piloted omp agent would be a context explosion, and it would make
flipping the pilot's flag back OFF a regression instead of a clean rollback.
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


def test_wrap_prompt_injects_card_when_present(tmp_path):
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT"})
    result = wrap_prompt("do the task", home_dir=home)
    assert "CARD CONTENT" in result
    assert result.endswith(COMPLETION_INSTRUCTIONS)
    assert "do the task" in result


def test_wrap_prompt_never_falls_back_to_soul(tmp_path):
    """No CARD.md -> prompt unchanged, even when SOUL.md exists on disk.

    docker_agent_sync writes SOUL.md unconditionally for every agent
    (fallback/UI reference) regardless of the use_operating_card flag — its
    mere presence must never make bridge.py start injecting it.
    """
    home = _home_with(tmp_path, **{"SOUL.md": "SOUL CONTENT ONLY"})
    result = wrap_prompt("do the task", home_dir=home)
    assert "SOUL CONTENT ONLY" not in result
    assert result == "do the task" + COMPLETION_INSTRUCTIONS


def test_wrap_prompt_works_with_neither_file_present(tmp_path):
    home = _home_with(tmp_path)
    result = wrap_prompt("do the task", home_dir=home)
    assert result == "do the task" + COMPLETION_INSTRUCTIONS


def test_wrap_prompt_still_appends_completion_instructions_with_card(tmp_path):
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT"})
    result = wrap_prompt("do the task", home_dir=home)
    assert result.endswith(COMPLETION_INSTRUCTIONS)


def test_wrap_prompt_include_identity_false_omits_card_even_when_present(tmp_path):
    """Nudge path (continue_once): the live omp session already received the
    card on its first-dispatch prompt — a nudge must not re-prepend it."""
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT"})
    result = wrap_prompt("keep going", home_dir=home, include_identity=False)
    assert "CARD CONTENT" not in result
    assert result == "keep going" + COMPLETION_INSTRUCTIONS


def test_wrap_prompt_include_identity_true_is_the_default(tmp_path):
    home = _home_with(tmp_path, **{"CARD.md": "CARD CONTENT"})
    default_result = wrap_prompt("do the task", home_dir=home)
    explicit_result = wrap_prompt("do the task", home_dir=home, include_identity=True)
    assert default_result == explicit_result
    assert "CARD CONTENT" in default_result


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
