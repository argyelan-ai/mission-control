"""Task A6 — pane state probe (permission prompts, working/idle).

``parse_pane_state`` is pure (pane text in, state dict out) — every fixture
below is synthetic, neutral tmux pane text, no real session content. Two
classes: parser heuristic tests (no docker needed) and ``capture_pane``
tests (subprocess mocked — no real docker exec).
"""
from __future__ import annotations

import subprocess

import pytest

from app.services import pane_state
from app.services.pane_state import capture_pane, parse_pane_state, process_alive

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

# Live repro (wave-review): the operator "steered" a follow-up message in
# while the agent was still working (queued/draft text sitting in the
# prompt line, not yet submitted). The CLI's own trailing status-bar chrome
# (model name + permission-mode line) pushes the "❯ <queued text>" line
# below the last-3-non-empty-lines window rule 3 originally checked —
# before the fix this fell through to "unknown" and the operator saw
# "Status unklar — Terminal prüfen" mid-steer.
QUEUED_DRAFT_PROMPT_NO_SPINNER = """\
────────────────────────────────────────────────────────────────────────────
❯ finish the report once you're done with this
────────────────────────────────────────────────────────────────────────────
  Sonnet 5
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

QUEUED_DRAFT_PROMPT_WITH_SPINNER = """\
✻ Thinking… (esc to interrupt)

❯ finish the report once you're done with this
────────────────────────────────────────────────────────────────────────────
  Sonnet 5
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

# Real captured pane from a live /model picker (verified on the "freecode"
# agent during A6's live-gate check) — a menu with no question line at all,
# just a plain header/description above the option list and a footer hint
# below it instead. Neutral UI chrome, no session content.
MODEL_PICKER_PROMPT = """\
   Select model
   Switch between Claude models. Your pick becomes the default for new sessions. For other/previous model names, specify with --model.

     1. Default (recommended)     Sonnet 5 · Efficient for routine tasks
   ❯ 2. Sonnet ✔                  Sonnet 5 · Efficient for routine tasks
     3. Opus                      Opus 5 · Best for everyday, complex tasks
     4. Haiku                     Haiku 4.5 · Fastest for quick answers
     5. Qwen/Qwen3.6-35B-A3B-FP8  Detected from Local OpenAI-compatible

   ● High effort (default) ←/→ to adjust

   Enter to set as default · s to use this session only · Esc to cancel
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


def test_permission_prompt_model_picker_menu_no_question_line():
    # Live-gate finding (fix round 1): a menu/picker has no "?"/"Do you
    # want" question — just a header + description above the options and a
    # footer hint ("Enter to ... Esc") below. Rule 1b must still classify
    # this as permission_prompt, using the short header ("Select model")
    # over the long description line directly below it as the question, and
    # truncating each option's right-aligned description column off the
    # label.
    result = parse_pane_state(MODEL_PICKER_PROMPT, transcript_active=True)

    assert result["status"] == "permission_prompt"
    assert result["prompt"]["question"] == "Select model"
    assert result["prompt"]["options"] == [
        {"key": "1", "label": "Default (recommended)"},
        {"key": "2", "label": "Sonnet ✔"},
        {"key": "3", "label": "Opus"},
        {"key": "4", "label": "Haiku"},
        {"key": "5", "label": "Qwen/Qwen3.6-35B-A3B-FP8"},
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


def test_queued_draft_prompt_no_spinner_is_idle_when_transcript_not_active():
    # The trailing status-bar chrome pushes the "❯ <queued text>" line out
    # of the last-3-non-empty-lines window — must still resolve via the
    # wider whole-tail scan, not fall through to "unknown".
    result = parse_pane_state(QUEUED_DRAFT_PROMPT_NO_SPINNER, transcript_active=False)

    assert result == {"status": "idle", "prompt": None}


def test_queued_draft_prompt_no_spinner_is_working_when_transcript_active():
    result = parse_pane_state(QUEUED_DRAFT_PROMPT_NO_SPINNER, transcript_active=True)

    assert result == {"status": "working", "prompt": None}


def test_queued_draft_prompt_with_spinner_is_working_regardless_of_transcript_active():
    # Spinner (rule 2) must still win over the draft-prompt shape (rule 3) —
    # priority order unchanged, just a new pattern added within rule 3.
    result = parse_pane_state(QUEUED_DRAFT_PROMPT_WITH_SPINNER, transcript_active=False)

    assert result == {"status": "working", "prompt": None}


def test_queued_draft_prompt_never_classified_as_permission_prompt():
    result = parse_pane_state(QUEUED_DRAFT_PROMPT_NO_SPINNER, transcript_active=True)

    assert result["status"] != "permission_prompt"


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


# ══════════════════════════════════════════════════════════════════════════
# process_alive — I/O, subprocess mocked. Each test clears the module-level
# cache first (keyed by slug, TTL-based) so results from other tests in this
# file (or a previous run within the same slug) can never leak in.
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_process_alive_argv_construction_and_true_when_found(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})
    captured_argv: list[str] = []

    def _fake_run(argv, **kwargs):
        captured_argv.extend(argv)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await process_alive(agent)

    assert captured_argv == [
        "docker", "exec", "-u", "agent", "mc-agent-rex", "pgrep", "-x", "claude",
    ]
    assert result is True


@pytest.mark.asyncio
async def test_process_alive_false_when_pgrep_finds_nothing(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await process_alive(agent)

    assert result is False


@pytest.mark.asyncio
async def test_process_alive_none_for_host_runtime():
    agent = _StubAgent(agent_runtime="host", slug="boss")

    result = await process_alive(agent)

    assert result is None


@pytest.mark.asyncio
async def test_process_alive_none_on_subprocess_exception(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})

    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await process_alive(agent)

    assert result is None


@pytest.mark.asyncio
async def test_process_alive_none_on_unexpected_returncode(monkeypatch):
    """Neither 0 (found) nor 1 (clean "nothing found") — pgrep's own
    failure modes (bad invocation, permission) aren't a confident answer."""
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})

    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=2, stdout="", stderr="usage error")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    result = await process_alive(agent)

    assert result is None


@pytest.mark.asyncio
async def test_process_alive_result_is_cached(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})
    call_count = {"n": 0}

    def _fake_run(argv, **kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    first = await process_alive(agent)
    second = await process_alive(agent)

    assert first is True
    assert second is True
    assert call_count["n"] == 1  # second call served from the ~30s cache


@pytest.mark.asyncio
async def test_process_alive_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setattr(pane_state, "_process_alive_cache", {})
    call_count = {"n": 0}

    def _fake_run(argv, **kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    fake_now = {"t": 1_800_000_000.0}
    monkeypatch.setattr(pane_state.time, "time", lambda: fake_now["t"])

    agent = _StubAgent(agent_runtime="cli-bridge", slug="rex")
    await process_alive(agent)
    fake_now["t"] += 31  # past the 30s TTL
    await process_alive(agent)

    assert call_count["n"] == 2
