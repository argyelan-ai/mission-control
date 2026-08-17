"""Chat input delivery (A5) — types text/keys into an agent's live session.

Third adapter building block per the chat-CLI-adapter contract (see
``mc-chat-cli-adapter`` skill): session-resolution (A2) and the parser (A1/A3)
read a session; this module is the write side.

Two delivery channels, mirroring the two PTY-bridge paths already used by the
live terminal (``routers/cli_terminal.py``):

- **cli-bridge (Docker) agents**: one-shot ``docker exec ... tmux send-keys``
  calls against the agent's own tmux window (``{slug}:0``). The ``-u agent``
  user and ``-e LANG=C.UTF-8`` env mirror the docker-exec construction in
  ``cli_terminal.py`` (agent_terminal_ws) — without the correct UTF-8 locale
  tmux mangles multi-byte characters typed through it. No ``-it`` flags here:
  unlike the persistent PTY attach, a single send-keys invocation needs no
  controlling terminal of its own. Every ``-l`` call is followed by a ``--``
  separator *before* the literal text/key (``-l -- <text>``, never ``-- -l``
  — order matters): tmux scans the full argv for flags even after ``-l``, so
  text starting with ``-`` (``-h``, ``- bullet point``) would otherwise be
  parsed as a tmux flag and silently swallowed (fix round 1, reproduced live
  on tmux 3.6a).
- **Boss (host, slug ``boss``/``boss-host``)**: a short-lived WebSocket
  connection to the host-pty-bridge, same upstream URL construction as
  ``_build_host_upstream_url``'s Boss branch in ``cli_terminal.py`` — raw
  bytes written straight into the bridge's pty, no tmux involved.

Every other host-runtime agent (Hermes, Jarvis, ...) has no input channel at
all — ``InputNotSupportedError`` for the router to turn into 409
``{"reason": "input_not_supported"}``, mirroring A2's hard privacy/capability
rule that only cli-bridge agents and Boss get a live session surface.

``send_text`` additionally touches the cli-bridge agent-recycler's idle
marker (``/home/agent/.claude/last-task.marker``) — the fleet's recycler
kills idle claude sessions every ~5-8 minutes based on that file's mtime, and
chat activity was otherwise invisible to it, killing chat conversations with
idle agents mid-conversation (live-gate finding, fix round 3).

``set_effort`` (effort-level switch, v1 docker-only) sends ``/effort
<level>`` as a direct CLI argument rather than driving the ``/model``
picker's Left/Right/``s`` sequence — Phase-0 discovery (empirically, on a
throwaway tmux window, Claude Code 2.1.233) found BOTH paths persist the new
effort level to the agent's ``settings.json`` (``effortLevel``) identically
for the four LOWER levels; the picker's "s = session only" option genuinely
scopes the MODEL choice to the session but does NOT extend that scoping to
effort, despite its own label. Since the picker buys no session-only
guarantee it appeared to promise, the direct-argument form is strictly
simpler and equally side-effecting — see the Phase-0 discovery notes in the
A5 report for the full empirical trail (settings.json mtime/content diffs
before/after each path).

``ALLOWED_EFFORT_LEVELS`` (single source of truth, also driving the
``GET /chat/history`` capabilities payload via ``effort_capabilities``) is
the 6 levels discovered via the CLI's OWN validation error (feeding it an
invalid argument, zero persistence risk): ``low, medium, high, xhigh, max,
ultracode`` — plus a 7th CLI-accepted value, ``auto``, deliberately
EXCLUDED: it clears the persisted override entirely rather than setting one
("Effort level set to auto", no "(saved.../this session only)" suffix, no
stable displayed state for a chip to show as "current"), so it doesn't fit
this endpoint's "pick one of N levels" contract. Also discovered: ``max``
and ``ultracode`` are session-only BY CLI DESIGN ("this session only" in
their own confirmation text, ``settings.json`` genuinely untouched) — unlike
the other 4, which persist. Because of this split, verification does NOT
rely on the compact status-line badge (``"<level> · /effort"``) that only
renders for a PERSISTED level — it polls for the CLI's own inline
confirmation line instead (``"effort level to <level>"``, present in both
the persisting and session-only phrasings alike), which is a strictly more
reliable, level-independent signal.

``set_effort`` refuses to touch a busy pane at all — a preflight via
``pane_state.parse_pane_state`` 409s with ``AgentBusyError``
(``{"reason": "agent_busy"}``) if the agent is mid-turn or showing an open
permission prompt, since ``Escape`` is this app's INTERRUPT key, not a
neutral cleanup keystroke (wave-review finding I-1: sending ``/effort`` into
a working turn only queues it, and an Escape "cleanup" on a busy pane
silently aborts real work or dismisses a live permission prompt). Past the
preflight, it verifies the switch actually landed by polling
``pane_state.capture_pane`` for that confirmation line before returning
success; on a verification timeout it re-checks busy-ness on a FRESH capture
and only sends ``Escape`` as a cleanup safety net if that fresh check is
clear, before raising ``EffortSwitchFailedError`` for the router to turn
into 409 ``{"reason": "effort_switch_failed"}``.

``slash_command_capabilities`` (composer command palette) merges a static
built-in list (``model``, ``effort``, ``clear``, ``compact``, ``context``,
``status``, ``help``, ``resume`` — Claude Code's well-known standard
commands; ``model``/``effort``'s descriptions are live-verified from Phase-0
discovery, the rest are generic) with this agent's installed skills, each
one becoming a slash-command entry (Claude Code invokes a skill the same
way a command is invoked — ``/<skill-name>``). Skills are discovered by
scanning ``<claude-config>/skills/*/SKILL.md`` (the SAME per-agent directory
``plugin_manager.sync_agent_skills_to_disk`` populates for BOTH plain custom
skills and resolved plugin-provided skill symlinks — one scan covers both
sources), reusing ``plugin_manager.list_skills_in_dir``'s frontmatter
parsing rather than re-implementing it. Docker/cli-bridge only (no
``claude-config`` mount to scan for any other runtime — builtins-only
there); fail-silent (a broken/missing skills dir never breaks the response,
just yields builtins alone) and cached ~60s per agent slug (real file I/O).
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path

import websockets as ws_client

from app.config import settings
from app.services.pane_state import capture_pane, parse_pane_state
from app.services.plugin_manager import list_skills_in_dir
from app.services.token_harvester import _host_home
from app.services.transcript_chat import resolve_context_window

logger = logging.getLogger("mc.agent_chat_input")

# Named keys tmux recognizes by name via send-keys (no -l needed) — the
# byte sequences below are for the Boss WS path, which writes straight into
# a pty and has no tmux key-name translation of its own.
ALLOWED_KEYS: dict[str, str] = {
    "Escape": "\x1b",
    "Enter": "\r",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9",
    "y": "y", "n": "n",
}

_TMUX_NAMED_KEYS = frozenset({"Escape", "Enter", "Up", "Down"})

# Host-runtime slugs that resolve to the Boss's own tmux/pty session — every
# other host agent has no input channel (Hermes, Jarvis, ...).
_BOSS_SLUGS = ("boss", "boss-host")

_BOSS_WS_URL = "ws://host.docker.internal:7682/"

_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"

# agent-recycler idle-detection marker (services/agent_recycler.py) — touched
# on every send_text() to a cli-bridge agent so chat activity counts as
# activity, not just task dispatch.
_RECYCLER_MARKER_PATH = "/home/agent/.claude/last-task.marker"

# Gap between the text frame and the Enter frame on the Boss WS path. Sending
# text + "\r" as ONE frame (or as two frames back-to-back with no gap) makes
# the Claude TUI's paste detection swallow the Enter as part of the pasted
# text instead of submitting it — the message sits in the input box forever
# (fix round 2, reproduced live: text landed but never submitted for hours).
_BOSS_ENTER_DELAY_SECONDS = 0.15

# Effort switching (v1, docker-only). All 6 discrete levels the CLI's own
# /effort argument validator accepts (see module docstring for the discovery
# evidence and why the 7th accepted value, "auto", is deliberately excluded).
# Single source of truth — reused by set_effort's validation AND
# effort_capabilities' GET /chat/history payload.
ALLOWED_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultracode")

# Polling budget for _verify_effort_applied: a docker-exec capture-pane round
# trip is fast (<100ms typically) but not instant, and the CLI needs a brief
# moment to render its confirmation line after processing the /effort command.
_EFFORT_VERIFY_ATTEMPTS = 5
_EFFORT_VERIFY_DELAY_SECONDS = 0.2


class InputNotSupportedError(Exception):
    """Raised when the agent's runtime has no live input channel."""


class EffortSwitchFailedError(Exception):
    """Raised when the effort level could not be verified as applied (a
    verification timeout — no explicit rejection or confirmation seen)."""


class EffortSwitchRejectedError(Exception):
    """Raised when the CLI EXPLICITLY declined the switch (its own inline
    message says so) rather than the verification just timing out.

    Root cause investigated live on a throwaway window on mc-agent-davinci
    (2026-08-18): a real operator's `/effort low` was answered "Kept effort
    level as auto" instead of "Set effort level to low" — the switch
    genuinely did not apply. Reproducing the exact same command sequence
    (fresh throwaway session starting from the same unset/"auto" baseline,
    same CLI version 2.1.234, same model) worked correctly every time;
    repeating the SAME already-current level also always said "Set effort
    level to X", never "Kept". No reproducible root cause was found despite
    genuine attempts (checked: duplicate/racing commands — none; same-value
    no-op — ruled out; model/provider mismatch — none, same Sonnet 5 both
    times) — this may be a transient CLI-side race specific to a real,
    longer-lived session's exact internal state at that moment. Given no
    fix was reproducible, this is the documented fallback the wave-review
    itself specified: detect the CLI's own rejection wording and surface it
    honestly instead of the switch silently failing or a generic timeout."""

    def __init__(self, cli_message: str):
        super().__init__(cli_message)
        self.cli_message = cli_message


class AgentBusyError(Exception):
    """Raised when the pane shows a working turn or an open permission
    prompt — an effort switch preflight refuses to touch a busy session."""


# Pane states an effort switch must never touch (see set_effort's preflight
# and its verify-timeout cleanup — wave-review finding I-1).
_BUSY_PANE_STATUSES = frozenset({"working", "permission_prompt"})

# The CLI's own explicit-rejection wording (live-verified, Davinci
# 2026-08-18: "Kept effort level as auto") — distinct from its
# apply-confirmation wording ("Set effort level to <level>"). Captures the
# CLI's whole response line so the operator sees exactly what it said.
_EFFORT_REJECTED_RE = re.compile(r"Kept effort level as \S+")


def _docker_argv(slug: str, *tail: str) -> list[str]:
    return [
        "docker", "exec", "-e", "LANG=C.UTF-8", "-u", "agent",
        f"mc-agent-{slug}",
        "tmux", "send-keys", "-t", f"{slug}:0",
        *tail,
    ]


async def _run_docker_exec(argv: list[str]) -> None:
    """Runs a docker-exec argv list off the event loop. Never raises —
    delivery failures (agent container gone, tmux window missing) are logged
    and swallowed, matching the fire-and-forget nature of typing into a
    live terminal (there is no request/response to fail). ``timeout=5``
    mirrors ``pane_state.capture_pane`` — without it a wedged ``docker exec``
    (daemon stall, container in uninterruptible state) pins a thread from
    the default executor forever, and ``send_keys`` can fire up to 16 of
    these per request against the same shared pool the tailer's own
    ``to_thread`` calls use (review finding I-2)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        logger.warning("chat input: docker exec timed out: %s", argv)
        return
    if result.returncode != 0:
        logger.warning(
            "chat input: docker exec failed (rc=%s): %s",
            result.returncode, result.stderr.decode(errors="replace"),
        )


async def _touch_recycler_marker(slug: str) -> None:
    """Refreshes the agent-recycler's idle-detection marker for a cli-bridge
    agent. The recycler kills idle claude sessions every ~5-8 minutes based
    on this file's mtime; chat activity was otherwise invisible to it, so an
    idle agent could get recycled mid chat-conversation (live-gate finding).
    Fire-and-forget via ``_run_docker_exec`` — a failed touch (agent
    container gone, path missing) must never block the actual keystroke."""
    await _run_docker_exec(
        ["docker", "exec", "-u", "agent", f"mc-agent-{slug}", "touch", _RECYCLER_MARKER_PATH]
    )


async def _send_boss_bytes(*payloads: bytes, delay_before_last: float = 0.0) -> None:
    """Opens a short-lived WS connection to the host-pty-bridge, writes each
    payload in order as its OWN frame, then closes. If ``delay_before_last``
    is set, waits that long before sending the final payload — needed when
    the last payload is a submitting ``Enter``, since sending it back-to-back
    with the preceding text (or worse, concatenated into one frame) makes the
    Claude TUI treat the whole thing as a paste and never submit (fix round 2).
    Never raises for the same reason as ``_run_docker_exec`` — a dead bridge
    just means the keystroke is lost, not a request the caller can retry
    meaningfully."""
    try:
        async with ws_client.connect(
            _BOSS_WS_URL, open_timeout=5, ping_interval=None,
        ) as ws:
            last_index = len(payloads) - 1
            for i, payload in enumerate(payloads):
                if i == last_index and delay_before_last:
                    await asyncio.sleep(delay_before_last)
                await ws.send(payload)
    except Exception:
        logger.warning("chat input: boss WS delivery failed", exc_info=True)


def _target_kind(agent) -> str:
    """Classifies the agent into a delivery channel, or raises
    ``InputNotSupportedError`` if it has none. Duck-typed on ``agent.slug`` /
    ``agent.agent_runtime`` like ``transcript_chat.resolve_transcript_dir``,
    so tests can pass a plain stub."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)

    if runtime == "cli-bridge" and slug:
        return "docker"
    if runtime == "host" and slug in _BOSS_SLUGS:
        return "boss"
    raise InputNotSupportedError()


async def send_text(agent, text: str) -> None:
    """Types ``text`` into the agent's live session. Single-line text is sent
    as one literal ``tmux send-keys -l`` call; multi-line text is wrapped in
    a bracketed-paste sequence (so the target CLI treats it as one paste
    instead of one line per Enter-triggered send-keys call). BOTH cases are
    followed by a separate ``Enter`` call to submit — a literal ``-l`` send
    only types the text into the TUI's input box, it never submits on its
    own (fix round 4: the single-line path was missing this Enter entirely,
    root cause of messages sitting unsubmitted; the multi-line path already
    had it). For cli-bridge agents, also refreshes the agent-recycler's idle
    marker (see ``_touch_recycler_marker``) — chat input is real activity and
    must not let an idle agent get recycled."""
    kind = _target_kind(agent)
    slug = agent.slug

    if kind == "docker":
        if "\n" in text:
            pasted = f"{_BRACKETED_PASTE_START}{text}{_BRACKETED_PASTE_END}"
            await _run_docker_exec(_docker_argv(slug, "-l", "--", pasted))
        else:
            await _run_docker_exec(_docker_argv(slug, "-l", "--", text))
        await _run_docker_exec(_docker_argv(slug, "Enter"))
        await _touch_recycler_marker(slug)
        return

    # kind == "boss" — text and its submitting Enter MUST be separate frames
    # with a gap between them (see _send_boss_bytes docstring / fix round 2).
    await _send_boss_bytes(
        text.encode(), b"\r", delay_before_last=_BOSS_ENTER_DELAY_SECONDS
    )


async def send_keys(agent, keys: list[str]) -> None:
    """Sends a sequence of allowlisted control keys. Validates every key
    against ``ALLOWED_KEYS`` before delivering any of them (raises
    ``ValueError`` on the first non-allowlisted key) — a partially-applied
    keystroke sequence would be worse than rejecting the whole request."""
    for key in keys:
        if key not in ALLOWED_KEYS:
            raise ValueError(f"key not allowlisted: {key!r}")

    kind = _target_kind(agent)
    slug = agent.slug

    if kind == "docker":
        for key in keys:
            if key in _TMUX_NAMED_KEYS:
                await _run_docker_exec(_docker_argv(slug, key))
            else:
                await _run_docker_exec(_docker_argv(slug, "-l", "--", ALLOWED_KEYS[key]))
        return

    # kind == "boss"
    await _send_boss_bytes(*(ALLOWED_KEYS[key].encode() for key in keys))


async def set_effort(agent, level: str) -> None:
    """Switches a cli-bridge agent's effort level via ``/effort <level>``
    (direct CLI argument — see module docstring for why the ``/model``
    picker's Left/Right/``s`` sequence was NOT used despite that being
    Phase-0's assumed path). Docker/cli-bridge only in v1: Boss and every
    other host agent raise ``InputNotSupportedError`` (no pane probe exists
    for them — mirrors ``pane_state.capture_pane``'s own v1 scope).

    Validates ``level`` against ``ALLOWED_EFFORT_LEVELS`` before doing
    anything (raises ``ValueError``, matching ``send_keys``'s allowlist-first
    convention), then a PREFLIGHT: refuses to send anything at all into a
    pane that's mid-turn (``working``) or showing an open
    ``permission_prompt`` — raises ``AgentBusyError`` untouched (wave-review
    I-1). ``Escape`` is this app's INTERRUPT key, not a neutral cleanup
    keystroke: sending ``/effort`` into a working turn only queues it (it
    fires later as a garbage prompt once the turn finishes), and an Escape
    "cleanup" against a working pane would silently abort real work in
    progress or dismiss a live permission prompt instead of tidying up a
    stray autocomplete.

    Once past the preflight, sends the command as one literal ``-l --`` call
    plus a separate submitting ``Enter`` (same two-call shape as
    ``send_text``'s single-line path). Unlike ``send_text``/``send_keys``,
    delivery here is NOT fire-and-forget: the command is polled for its own
    inline confirmation before this returns success. Two distinct failure
    modes:
    - The CLI EXPLICITLY declined the switch (``"Kept effort level as
      <X>"``, live-verified on Davinci — see ``EffortSwitchRejectedError``'s
      docstring for the investigation) -> that error, immediately, carrying
      the CLI's own message. No Escape cleanup — the CLI already answered
      and left the pane in a normal ready state.
    - Verification simply times out (no confirmation, no explicit
      rejection, seen) -> a FRESH pane capture decides whether ``Escape``
      cleanup is safe — sent only if that fresh capture is NOT
      ``working``/``permission_prompt`` (same I-1 reasoning: the pane may
      have started a real turn in the gap since the preflight passed) —
      before raising ``EffortSwitchFailedError``. When no pane can be
      captured at all (container/window gone), Escape is sent regardless —
      there's no live process left to interrupt, matching
      ``_run_docker_exec``'s own fail-silent contract for a target that no
      longer exists."""
    if level not in ALLOWED_EFFORT_LEVELS:
        raise ValueError(f"effort level not allowlisted: {level!r}")

    kind = _target_kind(agent)
    if kind != "docker":
        raise InputNotSupportedError()
    slug = agent.slug

    if await _pane_is_busy(agent):
        raise AgentBusyError()

    await _run_docker_exec(_docker_argv(slug, "-l", "--", f"/effort {level}"))
    await _run_docker_exec(_docker_argv(slug, "Enter"))

    if not await _verify_effort_applied(agent, level):
        if not await _pane_is_busy(agent):
            await _run_docker_exec(_docker_argv(slug, "Escape"))
        raise EffortSwitchFailedError()


async def _pane_is_busy(agent) -> bool:
    """True if the agent's pane shows a working turn or an open permission
    prompt.

    ``transcript_active=False`` is passed deliberately, NOT as a "trust the
    pane" shortcut but because it's the choice that keeps the check honest:
    ``parse_pane_state``'s spinner rule (``"esc to interrupt"`` anywhere in
    the pane -> ``working``) is the reliable working-detector for a docker
    agent — it fires independently of ``transcript_active`` and is checked
    BEFORE the ambiguous rule this parameter affects. That ambiguous rule
    (a plain input-prompt marker with NO spinner) only gets reached when the
    spinner rule already didn't match, i.e. there is no visible sign of an
    active turn — the CLI shows ``esc to interrupt`` whenever it's actually
    working. Forcing ``transcript_active=True`` here would make that
    fallback rule always resolve to ``"working"``, since a genuinely idle
    Claude Code pane's input line is ALSO just a plain ``❯ `` prompt with no
    spinner — that would make this check permanently reject idle agents too,
    defeating its own purpose. Returns ``False`` when no pane can be
    captured at all — nothing to protect from interrupting if there's no
    reachable pane."""
    pane = await capture_pane(agent)
    if pane is None:
        return False
    return parse_pane_state(pane, transcript_active=False)["status"] in _BUSY_PANE_STATUSES


async def _verify_effort_applied(agent, level: str) -> bool:
    """Polls the pane for the CLI's own inline confirmation line — it always
    echoes ``"Set effort level to <level> (...)"`` into the transcript pane
    right after ``/effort`` applies, live-verified across all 6 allowed
    levels. This is deliberately NOT the compact status-line badge
    (``"<level> · /effort"``) the earlier implementation polled for: that
    badge only renders for a level that becomes the PERSISTED default
    (low/medium/high/xhigh) — ``max``/``ultracode`` are session-only by CLI
    design and never show it at all, which would make verification always
    time out for them. The confirmation-line substring
    (``"effort level to <level>"``) is present in both the persisting
    ("saved as your default for new sessions") and session-only ("this
    session only") phrasings alike, so one check covers every allowed level.
    Fire-and-forget delivery (``_run_docker_exec``) gives no confirmation on
    its own that the command actually landed; this is the one call site in
    the module that needs a real success/failure signal instead of the
    usual "log a warning and move on" contract."""
    marker = f"effort level to {level}"
    for _ in range(_EFFORT_VERIFY_ATTEMPTS):
        await asyncio.sleep(_EFFORT_VERIFY_DELAY_SECONDS)
        pane = await capture_pane(agent)
        if not pane:
            continue
        if marker in pane:
            return True
        rejected = _EFFORT_REJECTED_RE.search(pane)
        if rejected is not None:
            # The CLI answered definitively (just not with what was asked
            # for) — stop polling immediately rather than burning the rest
            # of the attempt budget waiting for a confirmation that will
            # never come. See EffortSwitchRejectedError's docstring for the
            # live investigation behind this (Davinci, 2026-08-18).
            raise EffortSwitchRejectedError(rejected.group(0))
    return False


def effort_capabilities(agent) -> dict[str, object]:
    """Effort-switching capability for the composer chip:
    ``{"effortLevels": [...], "canSwitchEffort": bool}`` — consumed by
    ``routers/agent_chat.get_chat_history`` to let the frontend build the
    chip dynamically from what the agent's actual harness supports, instead
    of hardcoding a level list. Docker/cli-bridge agents get
    ``ALLOWED_EFFORT_LEVELS`` verbatim (the single source of truth ``
    set_effort`` validates against too); every other runtime (Boss, any
    other host agent) gets an empty list and ``canSwitchEffort=False`` — no
    pane probe exists for them, the same v1 boundary ``set_effort`` itself
    enforces via ``InputNotSupportedError``. Never raises: an unsupported
    runtime is a normal, expected answer here (unlike ``set_effort``, where
    it's a request the caller made in error), so it's handled as data, not
    an exception."""
    try:
        kind = _target_kind(agent)
    except InputNotSupportedError:
        kind = None

    if kind == "docker":
        return {"effortLevels": list(ALLOWED_EFFORT_LEVELS), "canSwitchEffort": True}
    return {"effortLevels": [], "canSwitchEffort": False}


# Built-in slash commands — static, not discovered (no CLI-side enumeration
# API exists). "model"/"effort" descriptions are live-verified (Phase-0
# discovery, exact CLI autocomplete text); the rest are Claude Code's
# well-known standard commands, described generically since they weren't
# individually probed live.
_BUILTIN_SLASH_COMMANDS: tuple[dict[str, str], ...] = (
    {"name": "model", "description": "Set the AI model for Claude Code"},
    {"name": "effort", "description": "Set effort level for model usage"},
    {"name": "clear", "description": "Clear the conversation history"},
    {"name": "compact", "description": "Compact the conversation to free up context"},
    {"name": "context", "description": "Show context window usage"},
    {"name": "status", "description": "Show session status"},
    {"name": "help", "description": "Show available commands"},
    {"name": "resume", "description": "Resume a previous session"},
)

_SLASH_COMMANDS_CACHE_TTL_SECONDS = 60
_slash_commands_cache: dict[str, tuple[float, list[dict[str, str | None]]]] = {}


def _agent_skills_dir(slug: str) -> Path:
    return _host_home() / ".mc" / "agents" / slug / "claude-config" / "skills"


async def _discover_skill_commands(slug: str) -> list[dict[str, str | None]]:
    """Skills portion of ``slash_command_capabilities``, cached ~60s per
    slug (the actual disk scan) — see the module docstring for the
    directory/reuse rationale. Fail-silent: any exception during discovery
    (permission error, race on a symlink resolving mid-scan, ...) logs and
    yields an empty list rather than breaking the whole capabilities
    response over one broken skill."""
    now = time.time()
    cached = _slash_commands_cache.get(slug)
    if cached is not None and (now - cached[0]) < _SLASH_COMMANDS_CACHE_TTL_SECONDS:
        return cached[1]

    try:
        skills = await asyncio.to_thread(list_skills_in_dir, _agent_skills_dir(slug))
    except Exception:
        logger.warning(
            "slash commands: skill discovery failed for slug=%s", slug, exc_info=True
        )
        return []

    discovered: list[dict[str, str | None]] = [
        {"name": s.name, "description": s.description or None} for s in skills
    ]
    _slash_commands_cache[slug] = (now, discovered)
    return discovered


async def slash_command_capabilities(agent) -> dict[str, object]:
    """``{"slashCommands": [{"name": str, "description": str|None}, ...]}``
    — builtins merged with this agent's installed skills. Docker/cli-bridge
    only: every other runtime gets builtins alone (no ``claude-config``
    mount to scan for skills)."""
    commands: list[dict[str, str | None]] = list(_BUILTIN_SLASH_COMMANDS)

    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    if runtime == "cli-bridge" and slug:
        commands = commands + await _discover_skill_commands(slug)

    return {"slashCommands": commands}


def model_options_capabilities() -> dict[str, object]:
    """``{"modelOptions": [{"command": str, "label": str,
    "contextWindow": int|None}, ...]}`` — the composer's model-switcher
    dropdown, built from ``settings.model_aliases`` (config-driven, single
    source — "default" is just another alias there, not special-cased) and
    ``transcript_chat.resolve_context_window`` (the SAME model->window
    resolution usage events already use, via ``settings.context_windows``)
    so the frontend never needs its own hardcoded model/window map. Not
    gated by agent runtime or cached — this is a static informational list
    (which models exist and their context windows), not a per-agent live
    capability check.

    NOTE (harness-catalog follow-up round): ``model_aliases`` is a fallback
    seed, not the long-term source of truth — a future per-agent CLI
    discovery (the actual ``/model`` picker rows, cached per
    ``cli_version``) is meant to take over as the primary source, with this
    static map demoted to "what to show when the catalog is empty"."""
    options = [
        {
            "command": command,
            "label": command.capitalize(),
            "contextWindow": resolve_context_window(model_id),
        }
        for command, model_id in settings.model_aliases.items()
    ]
    return {"modelOptions": options}
