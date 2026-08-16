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
_FOOTER_LOOKAHEAD_LINES = 6

# Option labels with a right-aligned description column (the live /model
# picker: "2. Sonnet ✔                  Sonnet 5 · Efficient for routine
# tasks") get truncated to the part before the column gap — a run of 2+
# spaces is always a column separator in Claude Code's box-drawn menus, never
# legitimate label text.
_LABEL_SPLIT_RE = re.compile(r"\s{2,}")

# A description line this long next to a short line directly above it reads
# as body text under a header, not the header itself (real /model picker:
# "Select model" / "Switch between Claude models. Your pick becomes the
# default...").
_LONG_LINE_THRESHOLD = 80

# ``❯ 1. Yes`` / ``  2. No, and tell Claude...`` — an optional leading
# pointer glyph, a digit, a literal period, then the option label. Requires
# the period (Claude Code's diff line-number gutters like "12    def foo():"
# have no period and must not match).
_OPTION_LINE_RE = re.compile(r"^\s*(?:❯\s*)?(\d)\.\s+(.*)$")

# Stripped from the front/back of a candidate question line — box-drawing
# chars Claude Code pads prompt boxes with, never part of the question text.
_BOX_DRAWING_STRIP_RE = re.compile(r"^[\s│╭╰─┃┆┊]+")

_ESC_TO_INTERRUPT = "esc to interrupt"

# Menu/picker footer hint (real /model picker: "Enter to set as default · s
# to use this session only · Esc to cancel") — these have no "?"/"Do you
# want" question line at all, just a plain header above a numbered option
# list, so rule 1 needs this as an alternate trigger (see
# ``_has_footer_hint_below``).
_FOOTER_HINT_RE = re.compile(r"enter to .*esc|esc to cancel", re.IGNORECASE)


def parse_pane_state(pane_text: str, transcript_active: bool) -> dict[str, Any]:
    """Classifies a captured pane snapshot into one of five states, applying
    the heuristics in priority order (first match wins) on the last
    ``_PANE_TAIL_LINES`` lines:

    1. A numbered option list (>=2 consecutive lines matching
       ``_OPTION_LINE_RE``) -> ``permission_prompt``, with the question +
       parsed options attached, when EITHER (a) a question line (ends in
       ``?`` or contains ``Do you want``) sits shortly above it, OR (b) no
       question line is found but a menu/picker footer hint (``Enter to
       ... Esc`` / ``esc to cancel`` — Claude Code's ``/model`` picker and
       similar menus have no question, just a plain header) appears shortly
       below it, in which case the question is the nearest non-blank line
       above the options (preferring a short header line over a long
       description line directly below it, see ``_find_question_fallback``).
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
    """Scans ``lines`` for a run of >=2 consecutive option lines validated by
    either an explicit question above (rule 1a) or a menu/picker footer hint
    below (rule 1b). Returns the first such match, or None."""
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
            options.append({"key": m.group(1), "label": _extract_label(m.group(2))})
            j += 1

        if len(options) >= 2:
            question = _find_question_above(lines, start)
            if question is None and _has_footer_hint_below(lines, j):
                question = _find_question_fallback(lines, start)
            if question is not None:
                return {"question": question, "options": options}

        i = j if j > i else i + 1

    return None


def _extract_label(raw: str) -> str:
    """Strips a right-aligned description column off an option's raw
    captured text (see ``_LABEL_SPLIT_RE``) — a no-op for plain
    single-space-separated labels, which is every pre-existing permission
    prompt fixture."""
    return _LABEL_SPLIT_RE.split(raw.strip(), maxsplit=1)[0].strip()


def _clean_line(line: str) -> str:
    """Strips box-drawing padding (Claude Code's bordered prompt boxes) off
    both ends of a candidate line, leaving just the text content."""
    cleaned = _BOX_DRAWING_STRIP_RE.sub("", line).rstrip()
    return cleaned.rstrip("│ ").strip()


def _find_question_above(lines: list[str], option_start: int) -> str | None:
    """Looks upward from just before ``option_start`` for the nearest
    non-blank line that reads as a question (ends in ``?`` or contains
    ``Do you want``), within ``_QUESTION_LOOKBACK_LINES`` lines."""
    floor = max(0, option_start - _QUESTION_LOOKBACK_LINES)
    for idx in range(option_start - 1, floor - 1, -1):
        candidate = _clean_line(lines[idx])
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


def _has_footer_hint_below(lines: list[str], options_end: int) -> bool:
    """Looks downward from just after the option block (``options_end`` is
    the first index past the last matched option line) for a menu/picker
    footer hint, within ``_FOOTER_LOOKAHEAD_LINES`` lines. Unlike
    ``_find_question_above``, this doesn't stop at the first non-matching
    line — real footers sit below other menu chrome (e.g. the live
    ``/model`` picker's "High effort (default)" reasoning-effort line)."""
    ceiling = min(len(lines), options_end + _FOOTER_LOOKAHEAD_LINES)
    for idx in range(options_end, ceiling):
        if _FOOTER_HINT_RE.search(lines[idx]):
            return True
    return False


def _find_question_fallback(lines: list[str], option_start: int) -> str | None:
    """Rule 1b's question text: the nearest non-blank line above the
    options, UNLESS it's a long description (over ``_LONG_LINE_THRESHOLD``
    chars) with an even shorter line directly above it — that pattern is a
    header sitting above its own description (real ``/model`` picker:
    "Select model" header, then a long "Switch between Claude models..."
    description, then a blank line, then the options), and the header is the
    more useful question text."""
    floor = max(0, option_start - _QUESTION_LOOKBACK_LINES)
    nearest_idx: int | None = None
    nearest_text: str | None = None
    for idx in range(option_start - 1, floor - 1, -1):
        candidate = _clean_line(lines[idx])
        if candidate:
            nearest_idx = idx
            nearest_text = candidate
            break

    if nearest_text is None or nearest_idx is None:
        return None

    if len(nearest_text) > _LONG_LINE_THRESHOLD:
        header_idx = nearest_idx - 1
        if header_idx >= floor:
            header_text = _clean_line(lines[header_idx])
            if header_text and len(header_text) <= _LONG_LINE_THRESHOLD:
                return header_text

    return nearest_text


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
