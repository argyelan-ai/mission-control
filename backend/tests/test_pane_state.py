"""Task A6 — pane state probe (permission prompts, working/idle).

``parse_pane_state`` is pure (pane text in, state dict out) — every fixture
below is synthetic, neutral tmux pane text, no real session content. Two
classes: parser heuristic tests (no docker needed) and ``capture_pane``
tests (subprocess mocked — no real docker exec).
"""
from __future__ import annotations

import subprocess

import pytest

from app.services.pane_state import capture_pane, parse_pane_state

# ══════════════════════════════════════════════════════════════════════════
# Fixtures — synthetic tmux capture-pane snapshots
# ══════════════════════════════════════════════════════════════════════════

PERMISSION_PROMPT_EDIT = """\
╭──────────────────────────────────────────────────────╮
│ Edit demo.py                                          │
│                                                        │
│   10   def handler():                                 │
│   11 -     return None                                │
│   11 +     return "ok"                                │
│                                                        │
╰──────────────────────────────────────────────────────╯
Do you want to make this edit to demo.py?
❯ 1. Yes
  2. Yes, and don't ask again this session
  3. No, and tell Claude what to do differently (esc)
"""

PLAN_APPROVAL_PROMPT = """\
Here is the plan I've prepared.

Would you like to proceed?

❯ 1. Yes, and auto-accept edits
  2. Yes, manually approve edits
  3. No, keep planning
"""

WORKING_SPINNER = """\
✻ Thinking… (esc to interrupt)

  Reading demo.py
  Editing demo.py
"""

IDLE_PROMPT = """\
╭──────────────────────────────────────────────────────╮
│ ❯                                                      │
╰──────────────────────────────────────────────────────╯
  ? for shortcuts
"""

GARBAGE_OUTPUT = """\
some random log line
another line with no markers at all
totally unrelated tool output
"""


# ══════════════════════════════════════════════════════════════════════════
# parse_pane_state — heuristic priority order
# ══════════════════════════════════════════════════════════════════════════


def test_permission_prompt_edit_approval():
    result = parse_pane_state(PERMISSION_PROMPT_EDIT, transcript_active=True)

    assert result["status"] == "permission_prompt"
    assert result["prompt"]["question"] == "Do you want to make this edit to demo.py?"
    assert result["prompt"]["options"] == [
        {"key": "1", "label": "Yes"},
        {"key": "2", "label": "Yes, and don't ask again this session"},
        {"key": "3", "label": "No, and tell Claude what to do differently (esc)"},
    ]


def test_permission_prompt_plan_approval():
    result = parse_pane_state(PLAN_APPROVAL_PROMPT, transcript_active=True)

    assert result["status"] == "permission_prompt"
    assert result["prompt"]["question"] == "Would you like to proceed?"
    assert result["prompt"]["options"] == [
        {"key": "1", "label": "Yes, and auto-accept edits"},
        {"key": "2", "label": "Yes, manually approve edits"},
        {"key": "3", "label": "No, keep planning"},
    ]


def test_working_spinner_esc_to_interrupt():
    result = parse_pane_state(WORKING_SPINNER, transcript_active=True)

    assert result == {"status": "working", "prompt": None}


def test_working_spinner_wins_even_if_transcript_not_active():
    # "esc to interrupt" is authoritative — it means the CLI is mid-turn
    # regardless of whether the transcript file has grown recently (e.g. the
    # model is thinking but hasn't flushed a new line yet).
    result = parse_pane_state(WORKING_SPINNER, transcript_active=False)

    assert result == {"status": "working", "prompt": None}


def test_idle_prompt_when_transcript_not_active():
    result = parse_pane_state(IDLE_PROMPT, transcript_active=False)

    assert result == {"status": "idle", "prompt": None}


def test_input_prompt_marker_is_working_when_transcript_active():
    result = parse_pane_state(IDLE_PROMPT, transcript_active=True)

    assert result == {"status": "working", "prompt": None}


def test_garbage_output_is_unknown():
    result = parse_pane_state(GARBAGE_OUTPUT, transcript_active=True)

    assert result == {"status": "unknown", "prompt": None}


def test_empty_pane_text_is_unknown():
    result = parse_pane_state("", transcript_active=False)

    assert result == {"status": "unknown", "prompt": None}


def test_single_numbered_line_is_not_a_permission_prompt():
    # Only one option line — must not false-positive on ordinary numbered
    # text (e.g. a single markdown list item echoed back).
    pane = "Do you want to continue?\n❯ 1. Yes\nsome other unrelated line\n"
    result = parse_pane_state(pane, transcript_active=True)

    assert result["status"] != "permission_prompt"


def test_diff_line_number_gutters_do_not_match_option_regex():
    # Diff gutters like "10   def handler():" have no period after the
    # digit and must never be mistaken for enumerated options.
    pane = (
        "  10   def handler():\n"
        "  11       return None\n"
        "some trailing line\n"
    )
    result = parse_pane_state(pane, transcript_active=True)

    assert result["status"] != "permission_prompt"


def test_only_last_40_lines_considered():
    # A permission prompt scrolled out of the visible pane (>40 lines back)
    # must not be detected — only what's currently on screen counts.
    old_prompt = PERMISSION_PROMPT_EDIT.strip("\n").splitlines()
    filler = [f"old line {i}" for i in range(50)]
    pane = "\n".join(old_prompt + filler)

    result = parse_pane_state(pane, transcript_active=True)

    assert result["status"] != "permission_prompt"


def test_esc_to_interrupt_scrolled_out_of_view_does_not_match():
    filler = "\n".join(f"old line {i}" for i in range(50))
    pane = WORKING_SPINNER + filler
    result = parse_pane_state(pane, transcript_active=True)

    assert result["status"] != "working"


# ══════════════════════════════════════════════════════════════════════════
# capture_pane — I/O, subprocess mocked
# ══════════════════════════════════════════════════════════════════════════


class _StubAgent:
    def __init__(self, agent_runtime: str, slug: str | None = None):
        self.agent_runtime = agent_runtime
        self.slug = slug


@pytest.mark.asyncio
async def test_capture_pane_docker_argv_construction(monkeypatch):
    """Mirrors agent_chat_input's docker-exec argv construction exactly."""
    captured_argv: list[str] = []

    def _fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="❯ ", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await capture_pane(agent)

    assert captured_argv == [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent",
        "mc-agent-rex",
        "tmux", "capture-pane", "-p", "-t", "rex:0",
    ]
    assert result == "❯ "


@pytest.mark.asyncio
async def test_capture_pane_returns_none_for_host_runtime():
    agent = _StubAgent(agent_runtime="host", slug="boss")

    result = await capture_pane(agent)

    assert result is None


@pytest.mark.asyncio
async def test_capture_pane_returns_none_on_nonzero_exit(monkeypatch):
    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="no such window")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await capture_pane(agent)

    assert result is None


@pytest.mark.asyncio
async def test_capture_pane_returns_none_on_subprocess_exception(monkeypatch):
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await capture_pane(agent)

    assert result is None


@pytest.mark.asyncio
async def test_capture_pane_truncates_to_last_40_lines(monkeypatch):
    long_output = "\n".join(f"line {i}" for i in range(100))

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=0, stdout=long_output, stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await capture_pane(agent)

    lines = result.splitlines()
    assert len(lines) == 40
    assert lines[0] == "line 60"
    assert lines[-1] == "line 99"
