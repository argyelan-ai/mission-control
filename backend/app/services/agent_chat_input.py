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
"""
from __future__ import annotations

import asyncio
import logging
import subprocess

import websockets as ws_client

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


class InputNotSupportedError(Exception):
    """Raised when the agent's runtime has no live input channel."""


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
    live terminal (there is no request/response to fail)."""
    result = await asyncio.to_thread(subprocess.run, argv, capture_output=True)
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
    instead of one line per Enter-triggered send-keys call) followed by a
    separate ``Enter`` to submit it. For cli-bridge agents, also refreshes
    the agent-recycler's idle marker (see ``_touch_recycler_marker``) — chat
    input is real activity and must not let an idle agent get recycled."""
    kind = _target_kind(agent)
    slug = agent.slug

    if kind == "docker":
        if "\n" in text:
            pasted = f"{_BRACKETED_PASTE_START}{text}{_BRACKETED_PASTE_END}"
            await _run_docker_exec(_docker_argv(slug, "-l", "--", pasted))
            await _run_docker_exec(_docker_argv(slug, "Enter"))
        else:
            await _run_docker_exec(_docker_argv(slug, "-l", "--", text))
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
