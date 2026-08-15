"""Pane state probe (A6) — infers whether a live agent session is working,
idle, waiting for input, or blocked on a permission/plan-approval prompt by
reading its tmux pane text.

Two building blocks, mirroring the module split used throughout the chat
adapter (parser stays pure, capture is the only I/O):

- ``parse_pane_state`` — pure function, pane text in, state dict out. No
  subprocess/docker/tmux knowledge at all, so it needs zero mocking to test.
- ``capture_pane`` — the only I/O here: shells out to ``docker exec ... tmux
  capture-pane`` for cli-bridge (Docker) agents, mirroring the argv
  construction in ``agent_chat_input.py``. Host agents other than Boss never
  reach this module (no transcript dir at all — see
  ``transcript_chat.resolve_transcript_dir``); Boss/host capture is out of
  scope for v1 and always returns ``None``, matching a plain `host` runtime.

Ready-signal glyphs mirrored from ``docker_agent_sync._wait_for_window_ready``
(``╭─`` / ``❯`` / ``> `` / ``$ ``) — that function already established what a
"ready" Claude Code pane looks like; this module builds on top of the same
vocabulary to distinguish working/idle rather than just ready/not-ready.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
from typing import Any

logger = logging.getLogger("mc.pane_state")

_PANE_TAIL_LINES = 40
_QUESTION_LOOKBACK_LINES = 6

# ``❯ 1. Yes`` / ``  2. No, and tell Claude...`` — an optional leading
# pointer glyph, a digit, a literal period, then the option label. Requires
# the period (Claude Code's diff line-number gutters like "12    def foo():"
# have no period and must not match).
_OPTION_LINE_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.*)$")

# Stripped from the front of a candidate question line — box-drawing chars
# Claude Code pads prompt boxes with, never part of the question text.
_BOX_DRAWING_STRIP_RE = re.compile(r"^[\s│╭╰─┃┆┊]+")

_ESC_TO_INTERRUPT = "esc to interrupt"


def parse_pane_state(pane_text: str, transcript_active: bool) -> dict[str, Any]:
    """Classifies a captured pane snapshot into one of five states, applying
    the heuristics in priority order (first match wins) on the last
    ``_PANE_TAIL_LINES`` lines:

    1. A numbered option list (>=2 consecutive lines matching
       ``_OPTION_LINE_RE``) with a question line (ends in ``?`` or contains
       ``Do you want``) shortly above it -> ``permission_prompt``, with the
       question + parsed options attached.
    2. The Claude Code spinner footer (``esc to interrupt``) anywhere in the
       captured text -> ``working``.
    3. An input-prompt marker (``❯ `` or ``> ``) on one of the last 3
       non-empty lines, with neither 1 nor 2 matching -> ``working`` if the
       transcript is actively growing, else ``idle``.
    4. Otherwise -> ``unknown``.
    """
    lines = pane_text.splitlines()
    tail = lines[-_PANE_TAIL_LINES:]
    tail_text = "\n".join(tail)

    prompt = _detect_permission_prompt(tail)
    if prompt is not None:
        return {"status": "permission_prompt", "prompt": prompt}

    if _ESC_TO_INTERRUPT in tail_text:
        return {"status": "working", "prompt": None}

    non_empty = [line for line in tail if line.strip()]
    last_three = non_empty[-3:]
    if any(("❯" in line) or ("> " in line) for line in last_three):
        return {"status": "working" if transcript_active else "idle", "prompt": None}

    return {"status": "unknown", "prompt": None}


def _detect_permission_prompt(lines: list[str]) -> dict[str, Any] | None:
    """Scans ``lines`` for a run of >=2 consecutive option lines preceded
    (within ``_QUESTION_LOOKBACK_LINES``) by a question line. Returns the
    first such match, or None."""
    n = len(lines)
    i = 0
    while i < n:
        match = _OPTION_LINE_RE.match(lines[i])
        if match is None:
            i += 1
            continue

        start = i
        options: list[dict[str, str]] = []
        j = i
        while j < n:
            m = _OPTION_LINE_RE.match(lines[j])
            if m is None:
                break
            options.append({"key": m.group(1), "label": m.group(2).strip()})
            j += 1

        if len(options) >= 2:
            question = _find_question_above(lines, start)
            if question is not None:
                return {"question": question, "options": options}

        i = j if j > i else i + 1

    return None


def _find_question_above(lines: list[str], option_start: int) -> str | None:
    """Looks upward from just before ``option_start`` for the nearest
    non-blank line that reads as a question (ends in ``?`` or contains
    ``Do you want``), within ``_QUESTION_LOOKBACK_LINES`` lines."""
    floor = max(0, option_start - _QUESTION_LOOKBACK_LINES)
    for idx in range(option_start - 1, floor - 1, -1):
        candidate = _BOX_DRAWING_STRIP_RE.sub("", lines[idx]).rstrip()
        # Trailing box-drawing padding (the closing "│" on the right of a
        # bordered prompt box) is stripped too.
        candidate = candidate.rstrip("│ ").strip()
        if not candidate:
            continue
        if candidate.endswith("?") or "Do you want" in candidate:
            return candidate
        # First non-blank line above the options wasn't a question — Claude
        # Code prompt boxes never interleave unrelated content between the
        # question and its options, so stop looking rather than risk
        # matching an unrelated earlier "?" line.
        return None
    return None


# ── Capture (I/O — shells out to docker exec, not pure) ─────────────────────

_BOSS_SLUGS = ("boss", "boss-host")


async def capture_pane(agent) -> str | None:
    """Captures the last ``_PANE_TAIL_LINES`` lines of an agent's live tmux
    pane, or None if this agent has no capturable pane.

    cli-bridge (Docker) agents: ``docker exec -e LANG=C.UTF-8 -u agent
    mc-agent-{slug} tmux capture-pane -p -t {slug}:0``, mirroring the argv
    construction in ``agent_chat_input._docker_argv`` (same env/user flags
    for the same reason — a wrong locale mangles multi-byte glyphs).

    Boss/host agents: always None in v1 (per the design brief) — the caller
    falls back to a transcript-mtime-only heuristic instead of pane text.

    Never raises — a delivery failure (container gone, tmux window missing)
    is logged and treated the same as "nothing captured", matching every
    other subprocess helper in this adapter."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)

    if runtime != "cli-bridge" or not slug:
        return None

    argv = [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent",
        f"mc-agent-{slug}",
        "tmux", "capture-pane", "-p", "-t", f"{slug}:0",
    ]

    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=5
        )
    except Exception:
        logger.warning("pane_state: capture-pane failed for slug=%s", slug, exc_info=True)
        return None

    if result.returncode != 0:
        logger.debug(
            "pane_state: capture-pane non-zero rc=%s for slug=%s: %s",
            result.returncode, slug, result.stderr,
        )
        return None

    lines = (result.stdout or "").splitlines()
    return "\n".join(lines[-_PANE_TAIL_LINES:])
