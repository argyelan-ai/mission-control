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
- ``process_alive`` — a second, independent I/O probe (``docker exec ...
  pgrep -x <process_name>``) used by ``transcript_chat.resolve_aliveness`` to
  tell "the CLI process is provably gone" apart from "just quiet for a while"
  — a stale pane's tmux window can still be capturable after the process
  inside it died, so pane text alone can't answer this. Cached ~30s per
  (agent slug, process name); same Boss/host `None` scoping as
  ``capture_pane``. The process name comes from the harness adapter
  (``transcript_adapters``) — it is ``claude`` for Claude Code and ``omp``
  for Sparky.

``parse_pane_state`` here is the CLAUDE CODE probe. A foreign TUI has its own
(``omp_chat.parse_pane_state``); the adapter registry picks which one runs.
Feeding an omp pane to this one produced ``unknown`` for every state — which
is exactly why the send-readiness gate had to be switched off for foreign
harnesses before that adapter existed.

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
import time
from typing import Any

logger = logging.getLogger("mc.pane_state")

_PANE_TAIL_LINES = 40
# Siehe parse_pane_state: Fenster, in dem die Eingabezeile ueber dem Fuss stehen darf.
_PROMPT_WINDOW_LINES = 8
# Die Eingabezeile BEGINNT mit dem Marker — optional hinter Rahmenzeichen, weil
# manche CLI-Versionen den Prompt in eine Box zeichnen (``│ ❯   …   │``). Ein
# "> " mitten in einer Ausgabe (Zitat, Diff, Log) ist dagegen keine
# Eingabeaufforderung und darf den Ruhezustand nicht vortaeuschen.
_PROMPT_LINE_RE = re.compile(r"^[\s│┃|>]*(?:❯|> )")
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

# A prompt line carrying DRAFT/QUEUED text (the operator typed a follow-up
# while the agent was still working — "steering") — "❯ " immediately
# followed by a non-whitespace character. Live repro (wave-review): the
# CLI's own trailing status-bar chrome (model name + permission-mode line)
# can push this line below the last-3-non-empty-lines window rule 3 checks,
# so it's scanned across the WHOLE tail separately rather than by widening
# that window (which would risk a false "ready" read from something else
# further up the pane). Deliberately NOT anchored to line-start (`^`) —
# real captures sometimes carry leading indentation/box-drawing padding.
_QUEUED_DRAFT_PROMPT_RE = re.compile(r"❯ \S")

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
    3. An input-prompt marker (``❯ `` or ``> ``) at the START of one of the
       last ``_PROMPT_WINDOW_LINES`` non-empty
       non-empty lines, OR a prompt line carrying DRAFT/QUEUED text anywhere
       in the tail (``❯ `` immediately followed by non-whitespace — the
       operator "steered" a follow-up in while the agent was still working;
       scanned separately/wider than the last-3 window because the CLI's own
       trailing status-bar chrome can push it out of that window — live
       repro, wave-review), with neither 1 nor 2 matching -> ``working`` if
       the transcript is actively growing, else ``idle``. NEVER
       ``permission_prompt`` for either shape — rule 1 already ran and
       didn't match, so a prompt-marker line is unambiguously the input line
       itself, not an option.
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

    # Wie weit ueber der letzten Zeile die Eingabezeile stehen darf. Frueher 3 —
    # das reichte, solange unter dem Prompt nur der Rahmen stand. Claude Code
    # rendert dort inzwischen MEHRERE Fusszeilen (Trennlinie, Statuszeile mit
    # Modell/Kontext, Bypass-Hinweis), womit "❯" auf Platz 4+ rutschte und ein
    # voellig normaler Ruhezustand als "unknown" galt. Folge im Betrieb
    # (Operator-Befund 18.08.2026): das Readiness-Gate von send_text hielt JEDE
    # Nachricht an Container-Agenten mit Statuszeile fuer einen bootenden Agenten
    # und lehnte sie mit 409 agent_starting ab — im Chat kam nichts an.
    # Grosszuegig gewaehlt, weil zusaetzliche Fusszeilen jederzeit dazukommen
    # koennen; die Praezision liefert stattdessen der Zeilenanfang unten.
    non_empty = [line for line in tail if line.strip()]
    prompt_window = non_empty[-_PROMPT_WINDOW_LINES:]
    # Zeilenanfang statt "irgendwo enthalten": die Eingabezeile BEGINNT mit dem
    # Marker. Ein "> " mitten in einer Ausgabe (Zitat, Diff, Log) ist keine
    # Eingabeaufforderung — mit dem groesseren Fenster waere die alte
    # Enthaelt-Pruefung sonst deutlich falsch-positiver geworden.
    has_bare_prompt = any(_PROMPT_LINE_RE.match(line) for line in prompt_window)
    has_queued_draft_prompt = any(_QUEUED_DRAFT_PROMPT_RE.search(line) for line in tail)
    if has_bare_prompt or has_queued_draft_prompt:
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


_PROCESS_ALIVE_CACHE_TTL_SECONDS = 30
_process_alive_cache: dict[tuple[str, str], tuple[float, bool]] = {}


async def process_alive(agent, process_name: str = "claude") -> bool | None:
    """Cheap liveness probe independent of pane TEXT: is the agent's actual
    CLI process still running? ``docker exec ... pgrep -x <process_name>``
    against the agent's own container — a tmux window/pane can outlive the
    process it used to run (e.g. it crashed and dropped to a bare shell), so
    a successful ``capture_pane`` alone isn't proof of this; ``pgrep`` is.
    Cached ~30s per (agent slug, process name) — this is polled far more
    often than a process genuinely starts/dies, and each check is still a
    real docker-exec round trip.

    ``process_name`` (omp-Runde) kommt aus dem Harness-Adapter
    (``transcript_adapters.TranscriptAdapter.process_name``). Der frueher
    fest verdrahtete Wert ``claude`` machte jede omp-Sitzung „beendet":
    ``pgrep -x claude`` findet im omp-Container nichts, rc=1 heisst aber
    „nachweislich weg" — der Container faehrt ``omp`` (live geprueft:
    ``ps`` in ``mc-agent-sparky``).

    Returns:
    - ``True``: pgrep found a matching process (rc=0).
    - ``False``: pgrep ran cleanly and found nothing (rc=1) — the process
      is PROVABLY gone (container reachable, no claude running).
    - ``None``: no process channel for this runtime (Boss/host — mirrors
      ``capture_pane``'s own v1 scope), or the check itself failed/timed
      out (container gone, docker daemon hiccup, unexpected pgrep exit) —
      genuinely unknown, not a confident "dead"."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)

    if runtime != "cli-bridge" or not slug:
        return None

    now = time.time()
    cache_key = (slug, process_name)
    cached = _process_alive_cache.get(cache_key)
    if cached is not None and (now - cached[0]) < _PROCESS_ALIVE_CACHE_TTL_SECONDS:
        return cached[1]

    argv = ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", "pgrep", "-x", process_name]

    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, timeout=5
        )
    except Exception:
        logger.warning("pane_state: process_alive check failed for slug=%s", slug, exc_info=True)
        return None

    if result.returncode not in (0, 1):
        # pgrep's own failure modes (bad invocation, permission) — not a
        # confident answer either way; don't cache an uncertain result.
        logger.debug(
            "pane_state: pgrep unexpected rc=%s for slug=%s: %s",
            result.returncode, slug, result.stderr,
        )
        return None

    alive = result.returncode == 0
    _process_alive_cache[cache_key] = (now, alive)
    return alive
