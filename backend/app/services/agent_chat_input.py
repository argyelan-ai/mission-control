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
idle agents mid-conversation (live-gate finding, fix round 3). It ALSO gates
on pane readiness before typing anything (docker path only) — live
measurement found a send landing in a session file created 27s after a
recycler respawn, and an earlier ping lost entirely into a still-booting
CLI. ``_wait_for_send_readiness`` polls fresh ``capture_pane`` reads for up
to ~20s; a pane that never becomes readable raises ``AgentStartingError``
(-> router 409 ``{"reason":"agent_starting"}``) WITHOUT ever typing. The
recycler-marker touch runs BEFORE this gate, not after — a ~20s poll must
not let the recycler kill the very session it's waiting to become ready.

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

**openclaude (19.08.2026).** Ein Claude-Code-Fork — der Eingabe-Kanal traegt
unveraendert, die Capabilities nicht. Drei Stellen mussten aufmachen, und
jede aus einem eigenen, live erhobenen Grund:

- ``_EFFORT_LEVELS_BY_HARNESS``: openclaude kennt ``ultracode`` NICHT
  (``/effort zzz`` -> "Valid options are: low, medium, high, max, xhigh,
  auto"). Die Claude-Liste weiterzureichen haette einen Regler mit einer
  Stufe gebaut, die diese CLI zurueckweist.
- ``effort_capabilities``: **Effort haengt am MODELL**, nicht am Harness.
  Derselbe Agent meldet fuer sein Spark-Modell "Effort not supported", fuer
  ``gpt-5.2-codex`` "Medium effort (default)". Mechanisch WUERDE ``/effort
  low`` trotzdem greifen (live geprueft: die Stufe landete in der
  settings.json) — nur ignoriert das Modell sie. Darum kein Regler, dafuer
  ein ``effortReason``, den das UI vorlesen kann.
- ``_wait_for_send_readiness``: die Pane-Marker sind identisch (am echten
  Pane eines openclaude-Agenten gegengeprueft, nur gelesen) — das Gate gilt hier also
  mit, statt ausgerechnet dem Harness zu fehlen, der es erfuellen kann.

``slash_command_capabilities`` (composer command palette) merges a static
built-in list PER HARNESS (Claude Code: ``model``, ``effort``, ``clear``,
``compact``, ``context``, ``status``, ``help``, ``resume``; openclaude: eine
eigene, deutlich groessere Liste, live durch den Kommando-Picker
geblaettert) with this agent's installed skills, each
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
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import websockets as ws_client

from app.config import settings
from app.redis_client import RedisKeys, get_redis
from app.services.harness_catalog import (
    discover_effort_support,
    discover_model_catalog,
    get_observed_model_windows,
    resolve_cli_version,
)
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
    # Ctrl+U leert die Eingabezeile. Gebraucht zum Zurueckziehen einer
    # eingereihten Nachricht: "Up" holt die Warteschlange der CLI in die
    # Eingabe zurueck (Claude Code: "Press up to edit queued messages"),
    # Ctrl+U wirft den Text weg. Live geprueft 03.09.2026 (2.1.259).
    "C-u": "\x15",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9",
    "y": "y", "n": "n",
}

_TMUX_NAMED_KEYS = frozenset({"Escape", "Enter", "Up", "Down", "C-u"})

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

# Effort switching (v1, docker-only). Die Stufen, die der Argument-Validator
# der jeweiligen CLI selbst akzeptiert — erhoben mit dem persistenzfreien
# Trick aus dem Adapter-Skill (ein ungueltiges Argument fuettern und die
# Fehlermeldung lesen; sie listet die gueltigen Werte auf, ohne etwas zu
# schreiben).
#
# claude (2.1.234): low, medium, high, xhigh, max, ultracode
# openclaude (0.7.0): ``/effort zzz`` -> "Invalid argument: zzz. Valid
#   options are: low, medium, high, max, xhigh, auto" (live 19.08.2026) —
#   also OHNE ``ultracode``. Wer die Claude-Liste weiterreicht, baut einen
#   Regler mit einer Stufe, die diese CLI zurueckweist.
#
# ``auto`` fehlt in beiden Listen mit Absicht: es LOESCHT den Override, statt
# eine Stufe zu setzen (siehe Modul-Docstring) — es passt nicht in den
# "waehle eine von N"-Vertrag dieses Endpunkts.
#
# ⚠️ Die REIHENFOLGE ist eine Leiter (sparsam -> gruendlich), keine Kopie der
# Fehlermeldung: openclaude listet dort "max, xhigh", was semantisch keine
# Rangfolge sein duerfte (Claude Code, von dem openclaude abzweigt,
# dokumentiert xhigh unterhalb von max). Der Regler zeigt Ordnung — darum
# hier bewusst die Claude-Ordnung uebernommen. Das ist die eine ANNAHME in
# dieser Tabelle; alles andere ist gemessen.
_EFFORT_LEVELS_BY_HARNESS: dict[str, tuple[str, ...]] = {
    "claude": ("low", "medium", "high", "xhigh", "max", "ultracode"),
    "openclaude": ("low", "medium", "high", "xhigh", "max"),
}

# Rueckwaertskompatibler Name (Claude Code) — bestehende Aufrufer/Tests.
ALLOWED_EFFORT_LEVELS = _EFFORT_LEVELS_BY_HARNESS["claude"]

# omp (seit 02.09.2026): das Effort-Pendant heisst dort "thinking level" und
# wird NICHT ueber ein Slash-Kommando gesetzt — die TUI kennt kein
# ``/thinking`` (live geprueft am omp-Pane eines Agenten, omp v16.4.6: der Text ging als
# normale Nachricht ans Modell). Der einzige Kanal ohne Neustart ist
# Shift+Tab (tmux ``BTab``, "Cycle thinking level"), das den Ring
#   off -> auto -> minimal -> low -> medium -> high -> xhigh -> off
# dreht; die Statuszeile zeigt die Stufe sofort (``omp_chat.
# status_line_thinking_level``). ``auto`` steht bewusst NICHT in der Leiter:
# es ist keine Stufe, sondern omps Klassifizierer, der pro Zug selbst waehlt —
# auf dem Regler waere das ein Wert ohne Rang. ``max`` fehlt, weil das
# Flottenmodell (GLM-5.3-Flash) es nicht anbietet; der Ring ueberspringt es.
# Bewusst eine EIGENE Tabelle statt Eintrag in ``_EFFORT_LEVELS_BY_HARNESS``:
# die dient ``model_catalog`` als "kennt Claude-Aliasse"-Tor, und ``/model
# sonnet`` ist in omp Kauderwelsch.
_OMP_THINKING_LEVELS: tuple[str, ...] = ("off", "minimal", "low", "medium", "high", "xhigh")
# Ein voller Ring hat 7 Stationen (inkl. auto); nach 8 Druecken ohne Treffer
# reagiert die TUI nicht (Modell ohne Reasoning, Pane eingefroren) — Schluss.
_OMP_THINKING_RING_MAX_PRESSES = 8
# omps Settings-Datei im Container (agentDir bei OMP_PROFILE=mc-agent). Nur
# ueber docker exec erreichbar — auf dem Host sind nur omp-sessions gemountet.
_OMP_SETTINGS_PATH = "/home/agent/.omp/profiles/mc-agent/agent/settings.json"


def effort_levels_for(harness: str | None) -> tuple[str, ...]:
    """Die Stufenleiter DIESES Harness — leer fuer jede CLI, die kein
    Effort-Pendant hat (kimi). Leer heisst: nichts anbieten, nichts tippen."""
    if harness == "omp":
        return _OMP_THINKING_LEVELS
    return _EFFORT_LEVELS_BY_HARNESS.get(harness or "", ())


# Polling budget for _verify_effort_applied: a docker-exec capture-pane round
# trip is fast (<100ms typically) but not instant, and the CLI needs a brief
# moment to render its confirmation line after processing the /effort command.
# 12 x 0.5s = 6s Budget (Boss-Transkriptpfad: x2 = 12s). Die erste Fassung
# (5 x 0.2s = 1s!) gab regelmaessig auf, BEVOR die CLI ihre Bestaetigung
# gerendert hatte — Operator-Befund 19.08.2026 am Tester: Wechsel angewendet
# (Pane zeigte "Set effort level to medium"), Toast meldete trotzdem "Nicht
# bestaetigt" — und der Timeout-Pfad tippte obendrein ein aufraeumendes
# Escape in eine voellig gesunde TUI. Ein Pane-Capture ist billig; lieber
# 6s geduldig pollen als einen gelungenen Wechsel als Fehlschlag melden.
_EFFORT_VERIFY_ATTEMPTS = 12
_EFFORT_VERIFY_DELAY_SECONDS = 0.5


class InputNotSupportedError(Exception):
    """Raised when the agent's runtime has no live input channel."""


class EffortSwitchFailedError(Exception):
    """Raised when the effort level could not be verified as applied (a
    verification timeout — no explicit rejection or confirmation seen)."""


class EffortSwitchRejectedError(Exception):
    """Raised when the CLI EXPLICITLY declined the switch (its own inline
    message says so) rather than the verification just timing out.

    Root cause investigated live on a throwaway window on mc-agent-alpha
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


class AgentStartingError(Exception):
    """Raised when send_text's readiness gate never saw the pane become
    ready within its poll budget — the CLI is still booting/loading plugins,
    or a recycler respawn is mid-flight. See ``_wait_for_send_readiness``."""


# Pane states an effort switch must never touch (see set_effort's preflight
# and its verify-timeout cleanup — wave-review finding I-1).
_BUSY_PANE_STATUSES = frozenset({"working", "permission_prompt"})

# send_text's readiness gate (docker path): live measurement (wave-review)
# found a send landing in a session file created 27s AFTER a recycler
# respawn's previous write, and an earlier ping lost entirely into a
# still-booting CLI. ~20s at 1s steps mirrors the fleet's own boot/respawn
# timing (docker_agent_sync's own window-ready wait is in the same range).
# Harnesses mit einer eigenen Pane-Sonde (``transcript_adapters``). Nur fuer
# sie darf das Bereitschafts-Tor unten laufen — ein Harness ohne Sonde liefert
# fuer JEDEN Pane ``unknown`` und wuerde damit jede Nachricht ablehnen.

_SEND_READINESS_POLL_ATTEMPTS = 20
_SEND_READINESS_POLL_INTERVAL_SECONDS = 1.0

# The CLI's own explicit-rejection wording (live-verified, ein Container-Agent
# 2026-08-18: "Kept effort level as auto") — distinct from its
# apply-confirmation wording ("Set effort level to <level>"). Captures the
# CLI's whole response line so the operator sees exactly what it said.
_EFFORT_REJECTED_RE = re.compile(r"Kept effort level as \S+")
# Bestaetigungsdialog bei Sessions mit gecachtem Verlauf (s. _verify_effort_applied).
_EFFORT_CONFIRM_DIALOG_MARKER = "Change effort level?"


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


def can_receive_input(agent) -> bool:
    """Kann dieser Agent ueberhaupt Chat-Text empfangen? Oeffentliche,
    ausnahmefreie Form von ``_target_kind`` — der Anhang-Endpunkt fragt das
    vorab, weil eine Datei fuer einen Agenten, der nie eine Nachricht
    bekommt, nur Platte kostet und ein leeres Versprechen ist."""
    try:
        _target_kind(agent)
        return True
    except InputNotSupportedError:
        return False


async def _wait_for_send_readiness(agent) -> None:
    """Readiness gate before typing into a docker agent's pane. Live
    measurement (wave-review) found a real send landing in a session file
    created 27s AFTER a recycler respawn's previous write — the message was
    typed into a pane that was mid-boot, not the running session it looked
    like from the outside. An earlier ping was lost entirely the same way.

    Polls ``capture_pane`` fresh (never cached — this is exactly the kind
    of transient state a cache would hide) up to
    ``_SEND_READINESS_POLL_ATTEMPTS`` times, ``_SEND_READINESS_POLL_INTERVAL_SECONDS``
    apart: returns immediately once ``parse_pane_state`` recognizes ANYTHING
    in the pane — ``"working"`` (queueing a message into a busy turn is
    legit, this gate does NOT block on it — see the queued-draft-prompt fix
    above for why a mid-steer pane still classifies correctly), ``"idle"``,
    or ``"permission_prompt"`` all mean the CLI is responsive and rendering
    something recognizable, i.e. NOT booting. Only ``"unknown"`` (or no pane
    captured at all — container/window not even up yet) blocks: that shape
    is what a booting CLI, a loading-plugins splash, or a tmux window
    mid-respawn all share, and there's no cheap way to tell those apart
    from pane text alone — so this gate treats them identically and keeps
    waiting. Raises ``AgentStartingError`` if the pane never becomes
    readable within the poll budget — the caller must NOT type anything
    into a pane this gate never confirmed as ready.

    ``transcript_active=False`` is passed to ``parse_pane_state`` for the
    same reason ``_pane_is_busy`` uses it (see that function's docstring):
    it only affects whether an ambiguous bare-prompt/queued-draft line
    resolves to ``"working"`` vs ``"idle"`` — and this gate treats both
    identically anyway, so the choice is moot here beyond staying
    consistent with the rest of the module."""
    from app.services.transcript_adapters import adapter_for

    # Die Pane-Regeln des jeweiligen Harness — Claude-Glyphen erkennen die
    # omp-TUI nicht (und umgekehrt).
    read_state = adapter_for(agent).parse_pane_state

    for _ in range(_SEND_READINESS_POLL_ATTEMPTS):
        pane = await capture_pane(agent)
        if pane is not None:
            status = read_state(pane, False)["status"]
            if status != "unknown":
                return
        await asyncio.sleep(_SEND_READINESS_POLL_INTERVAL_SECONDS)
    raise AgentStartingError()


async def send_text(agent, text: str) -> None:
    """Types ``text`` into the agent's live session. Single-line text is sent
    as one literal ``tmux send-keys -l`` call; multi-line text is wrapped in
    a bracketed-paste sequence (so the target CLI treats it as one paste
    instead of one line per Enter-triggered send-keys call). BOTH cases are
    followed by a separate ``Enter`` call to submit — a literal ``-l`` send
    only types the text into the TUI's input box, it never submits on its
    own (fix round 4: the single-line path was missing this Enter entirely,
    root cause of messages sitting unsubmitted; the multi-line path already
    had it).

    Docker path, in order: (1) refreshes the agent-recycler's idle marker
    FIRST — see ``_touch_recycler_marker`` — deliberately BEFORE the
    readiness gate below, not after typing like the fire-and-forget keys
    themselves: a gate poll can run for up to ~20s, and without an early
    marker touch the recycler could decide the session is idle and kill it
    WHILE this function is still waiting for it to finish booting, racing
    the very gate meant to protect the send. (2) the readiness gate itself
    (``_wait_for_send_readiness``, raises ``AgentStartingError`` on
    timeout — the router turns that into 409
    ``{"reason":"agent_starting"}``) — never type into a half-booted TUI or
    a plain shell prompt left behind by a mid-respawn container. (3) only
    then the actual keystrokes."""
    kind = _target_kind(agent)
    slug = agent.slug

    if kind == "docker":
        from app.services.transcript_adapters import PANE_PROBED_HARNESSES

        await _touch_recycler_marker(slug)
        if getattr(agent, "harness", None) in PANE_PROBED_HARNESSES:
            # Das Tor liest den Pane mit den Regeln DIESES Harness. Solange es
            # nur Claude-Regeln gab, erfuellte eine fremde TUI sie nie — der omp-Agent
            # war dadurch dauerhaft unerreichbar (jeder Send: 409
            # agent_starting; Operator-Befund 19.08.2026).
            #
            # Wer das Tor bekommt, steht NICHT hier, sondern faellt mit der
            # Adapter-Registrierung: ein Harness mit Adapter hat eine Sonde,
            # also kann das Tor eine Aussage treffen. Zwei Runden hatten dafuer
            # unabhaengig je eine eigene Liste gebaut (eine aus den
            # Effort-Stufen abgeleitet — eine ANDERE Frage, eine handgepflegt);
            # beim Zusammenfuehren liefen sie prompt auseinander.
            #
            # openclaude erfuellt die Claude-Marker nachweislich: am ECHTEN
            # Pane eines openclaude-Agenten gegengeprueft (nur gelesen, nichts
            # getippt) — ``parse_pane_state`` liefert dort ``idle``, der Fuss
            # zeigt ``esc to interrupt`` wie bei Claude Code.
            #
            # Harnesses OHNE Adapter (kimi) bleiben aussen vor: keine Aussage
            # ueber Bereitschaft ist besser als eine falsche Ablehnung.
            await _wait_for_send_readiness(agent)
        if "\n" in text:
            pasted = f"{_BRACKETED_PASTE_START}{text}{_BRACKETED_PASTE_END}"
            await _run_docker_exec(_docker_argv(slug, "-l", "--", pasted))
        else:
            await _run_docker_exec(_docker_argv(slug, "-l", "--", text))
        await _run_docker_exec(_docker_argv(slug, "Enter"))
        note_sent(str(getattr(agent, "id", "") or slug), text)
        return

    # kind == "boss" — text and its submitting Enter MUST be separate frames
    # with a gap between them (see _send_boss_bytes docstring / fix round 2).
    await _send_boss_bytes(
        text.encode(), b"\r", delay_before_last=_BOSS_ENTER_DELAY_SECONDS
    )
    note_sent(str(getattr(agent, "id", "") or slug), text)


# Was zuletzt je Agent eingetippt wurde — fuer die Live-Vorschau des
# Sessions-Chats. Sie zeigt nur, was NACH dem Auftrag auf dem Bildschirm
# steht; bis der Auftrag im Transkript auftaucht (bei omp erst am Ende des
# Zugs), ist der eingetippte Text der einzige Anker. In-Process reicht: das
# Backend laeuft mit einem Worker, Sende-Router und Tailer teilen sich den
# Prozess. Live 03.09.2026: ohne Anker zeigte die Vorschau 2,5 KB alte
# Historie samt Echo des Auftrags.
_LAST_SENT: dict[str, str] = {}


def note_sent(agent_id: str, text: str) -> None:
    _LAST_SENT[agent_id] = text


def pop_last_sent(agent_id: str) -> str | None:
    return _LAST_SENT.pop(agent_id, None)


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

    # kind == "boss" — delay_before_last wirkt auch bei EINEM Frame: connect,
    # kurz setzen lassen, dann schreiben. Ohne den Settle-Moment verpufft ein
    # einzelnes Byte im Attach-Handshake der Bridge (live gesehen 19.08.2026:
    # Enter auf den Effort-Bestaetigungsdialog kam nie an, waehrend Text+Enter
    # mit Frame-Abstand immer funktionierte).
    await _send_boss_bytes(
        *(ALLOWED_KEYS[key].encode() for key in keys),
        delay_before_last=_BOSS_ENTER_DELAY_SECONDS,
    )


async def set_effort(agent, level: str) -> None:
    """Switches a cli-bridge agent's effort level via ``/effort <level>``
    (direct CLI argument — see module docstring for why the ``/model``
    picker's Left/Right/``s`` sequence was NOT used despite that being
    Phase-0's assumed path). Docker/cli-bridge only in v1: Boss and every
    other host agent raise ``InputNotSupportedError`` (no pane probe exists
    for them — mirrors ``pane_state.capture_pane``'s own v1 scope).

    Validates ``level`` against ``ALLOWED_EFFORT_LEVELS`` before doing
    anything (raises ``ValueError``, matching ``send_keys``'s allowlist-first
    convention), dann BEIDE Tore aus ``effort_capabilities`` gespiegelt —
    kennt der Harness ``/effort``, und kennt das MODELL des Agenten Stufen?
    Beide Male ``InputNotSupportedError`` (409), damit ein direkter
    POST nicht tippen kann, was die Oberflaeche gar nicht anbietet. Dann ein
    PREFLIGHT: refuses to send anything at all into a
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
      <X>"``, live-verified on ein Container-Agent — see ``EffortSwitchRejectedError``'s
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
    harness = getattr(agent, "harness", None)
    # Gegen die Leiter DIESES Harness, nicht pauschal gegen die von Claude
    # Code: ``ultracode`` gibt es bei openclaude nicht, und die CLI wuerde es
    # mit "Invalid argument" zurueckweisen — dann tippen wir es gar nicht erst
    # in ihre Eingabe. Bei unbekanntem Harness bleibt die breiteste bekannte
    # Liste stehen, damit ein unsinniger Wert weiterhin als FEHLERHAFTE
    # ANFRAGE (422) auffaellt und nicht erst am Harness-Gate (409) haengt —
    # die Reihenfolge ist die von vorher.
    if level not in (effort_levels_for(harness) or ALLOWED_EFFORT_LEVELS):
        raise ValueError(f"effort level not allowlisted: {level!r}")

    kind = _target_kind(agent)
    if not effort_levels_for(harness):
        # Spiegel des Capabilities-Gates: selbst wenn ein Client den Endpoint
        # direkt trifft, tippen wir kein /effort in eine fremde CLI.
        raise InputNotSupportedError()

    if kind == "boss":
        await _set_effort_boss(agent, level)
        return

    if harness == "omp":
        await _set_effort_omp(agent, level)
        return

    slug = agent.slug

    # Zweiter Spiegel, seit 20.08.2026: das MODELL-Tor aus
    # ``effort_capabilities``. Ohne ihn war der Docstring-Anspruch "Spiegel
    # des Capabilities-Gates" nur halb wahr — ein direkter POST
    # /chat/effort {"level":"high"} kam bei einem Agenten auf einem Modell
    # ohne Effort-Stufen durch beide bisherigen Pruefungen und tippte
    # ``/effort high`` in den Live-Pane. Die Stufe landet dann in der
    # settings.json und das Modell ignoriert sie — genau die luegende
    # Anzeige, gegen die das Tor gebaut wurde. Gleiche Quelle, gleicher
    # Cache-Schluessel (Modell inklusive) wie im Capabilities-Pfad, also
    # kein zusaetzliches Wegwerf-Fenster.
    #
    # ``supported is None`` (kalter Cache, Lock, Probe gescheitert) bleibt
    # BEWUSST erlaubt — identisch zum Capabilities-Gate: "noch nicht
    # ermittelt" ist kein Grund, einen funktionierenden Wechsel abzulehnen.
    model = await asyncio.to_thread(_persisted_model, slug)
    support = await discover_effort_support(agent, model)
    if support.get("supported") is False:
        raise InputNotSupportedError()

    if await _pane_is_busy(agent):
        raise AgentBusyError()

    await _run_docker_exec(_docker_argv(slug, "-l", "--", f"/effort {level}"))
    await _run_docker_exec(_docker_argv(slug, "Enter"))

    if not await _verify_effort_applied(agent, level):
        if not await _pane_is_busy(agent):
            await _run_docker_exec(_docker_argv(slug, "Escape"))
        raise EffortSwitchFailedError()


async def _set_effort_omp(agent, level: str) -> None:
    """omp-Variante von ``set_effort`` (02.09.2026): drueckt Shift+Tab
    (``BTab``), bis die Statuszeile die gewuenschte Stufe zeigt.

    - EIN Capture fuer Preflight UND Ist-Stufe: ``⟦esc⟧`` im Pane heisst
      arbeitender Zug -> ``AgentBusyError`` (Shift+Tab waehrend eines Zugs
      wuerde die Stufe zwar wechseln, aber wir wollen keine Tasten in einen
      arbeitenden Pane senden — gleiche I-1-Regel wie bei Claude).
    - Kein Pane / keine Statuszeile -> ``InputNotSupportedError`` (die TUI
      bootet oder das Fenster fehlt; ohne Anzeige ist nichts verifizierbar).
    - Schon dort -> kein Tastendruck.
    - Sonst je Druck: ``BTab``, kurz warten, Capture, Statuszeile lesen.
      Treffer -> fertig. Nach ``_OMP_THINKING_RING_MAX_PRESSES`` ohne Treffer
      -> ``EffortSwitchFailedError``. KEIN Escape-Cleanup: es liegt kein
      halbes Kommando im Eingabefeld, Escape waere hier omps INTERRUPT.
    - Persistenz: der Wechsel per Shift+Tab gilt nur fuer die laufende
      Session — jeder Task-Relaunch (bridge.py respawnt das Fenster) startet
      wieder mit ``defaultThinkingLevel`` aus omps settings.json. Darum wird
      die Stufe zusaetzlich dort eingetragen (auch bei "schon dort": die
      Statuszeile sagt nichts ueber die Datei). Ausnahme ``off``: omps
      eigener Settings-Enum kennt es nicht als Standard (die CLI persistiert
      es selbst nie) — wie max/ultracode bei Claude session-only.
    """
    from app.services.omp_chat import status_line_thinking_level

    slug = agent.slug
    pane = await capture_pane(agent)
    if pane is not None and _omp_pane_busy(agent, pane):
        raise AgentBusyError()
    current = status_line_thinking_level(pane)
    if current is None:
        raise InputNotSupportedError()

    if current != level:
        for _ in range(_OMP_THINKING_RING_MAX_PRESSES):
            await _run_docker_exec(_docker_argv(slug, "BTab"))
            await asyncio.sleep(_EFFORT_VERIFY_DELAY_SECONDS)
            current = status_line_thinking_level(await capture_pane(agent))
            if current == level:
                break
        else:
            raise EffortSwitchFailedError()

    if level != "off":
        await _persist_omp_thinking_level(slug, level)


def _omp_pane_busy(agent, pane: str) -> bool:
    from app.services.transcript_adapters import adapter_for

    return adapter_for(agent).parse_pane_state(pane, False)["status"] in _BUSY_PANE_STATUSES


async def _persist_omp_thinking_level(slug: str, level: str) -> None:
    """Schreibt ``defaultThinkingLevel`` in omps settings.json IM Container
    (JSON-Merge, andere Schluessel bleiben). Fail-silent wie
    ``_run_docker_exec``: der Session-Wechsel ist schon passiert und
    verifiziert; eine fehlende Persistenz kostet nur den Standard beim
    naechsten Relaunch, keinen falschen Erfolg."""
    script = (
        "import json, os, pathlib\n"
        f"p = pathlib.Path({_OMP_SETTINGS_PATH!r})\n"
        "p.parent.mkdir(parents=True, exist_ok=True)\n"
        "try:\n"
        "    data = json.loads(p.read_text())\n"
        "except Exception:\n"
        "    data = {}\n"
        f"data['defaultThinkingLevel'] = {level!r}\n"
        "tmp = p.with_suffix('.tmp')\n"
        "tmp.write_text(json.dumps(data, indent=2) + '\\n')\n"
        "os.replace(tmp, p)\n"
    )
    argv = [
        "docker", "exec", "-u", "agent", f"mc-agent-{slug}",
        "python3", "-c", script,
    ]
    try:
        result = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=5)
        if result.returncode != 0:
            logger.warning(
                "omp thinking level persist failed for %s: %s",
                slug, result.stderr.decode(errors="replace").strip()[:200],
            )
    except Exception as exc:  # noqa: BLE001 — fail-silent, s. Docstring
        logger.warning("omp thinking level persist failed for %s: %s", slug, exc)


def _read_since(path: Path, offset: int | None) -> tuple[int, str]:
    """Liest den ZUWACHS einer Transkript-Datei ab ``offset`` und gibt
    ``(neuer_offset, text)`` zurueck. Blockierend — Aufrufer wrappt in
    ``asyncio.to_thread``.

    ``offset is None`` heisst "diese Datei zum ersten Mal gesehen, und wir
    wissen nicht, wie gross sie beim Absenden des Kommandos war". Dann wird
    der AKTUELLE Stand als Nullpunkt genommen und nichts gelesen: alles davor
    koennte aus einem frueheren Wechsel stammen, und eine alte
    "Set effort level to <X>"- oder "Kept effort level as <X>"-Zeile als
    eigenes Ergebnis zu lesen ist der teurere Fehler (falsches 204 bzw.
    falsche Ablehnung) als eine verpasste Bestaetigung — die faellt auf
    409 zurueck, also auf die ehrliche Seite. Betrifft genau zwei Faelle:
    Rollover mitten im Wechsel und den stat-Race beim Baseline-Read.
    """
    size = path.stat().st_size
    if offset is None or offset > size:  # None = Erstsichtung; > = Datei gekuerzt
        return size, ""
    if size == offset:
        return offset, ""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    return offset + len(data), data.decode("utf-8", errors="replace")


async def _set_effort_boss(agent, level: str) -> None:
    """Boss-Variante von ``set_effort`` (19.08.2026, Operator: Chip bei Boss
    ohne Funktion). Kein Pane-Kanal — darum:

    - PREFLIGHT ueber das Transkript statt den Pane: mtime frisch UND der Zug
      laeuft noch (letzte inhaltliche Zeile ist keine reine Antwort) -> busy.
      In eine arbeitende TUI getippt wuerde /effort nur als Muell-Prompt
      queuen — exakt die Docker-Preflight-Begruendung.
    - TIPPEN ueber die WS-Bridge (gleiche Frame-Trennung wie send_text).
    - VERIFY ueber das TRANSKRIPT: /effort schreibt seine Bestaetigung als
      local-command-stdout-Zeile in die Session-Datei (R12b, gleiche CLI).
      Gelesen wird NUR was nach der vorab notierten Dateigroesse dazukommt —
      dieselbe Stale-Zeilen-Lektion wie beim Pane-Verify (18.08.).
    - KEIN Escape bei Timeout: ohne Pane ist "safe to Escape" nicht
      feststellbar, und ein Escape in Marks arbeitenden Boss wuerde echte
      Arbeit abbrechen. Ein evtl. gequeutes /effort ist das kleinere Uebel.
    """
    from app.services.transcript_chat import (  # lazy: Import-Zyklus vermeiden
        ChatTailerManager, find_active_session, resolve_transcript_dir,
    )

    tdir = resolve_transcript_dir(agent)
    if tdir is None:
        raise InputNotSupportedError()
    active = await asyncio.to_thread(find_active_session, tdir)
    baseline_path: Path | None = None
    # None heisst "Groesse unbekannt", NICHT 0 (Review 20.08.2026): frueher
    # setzte der OSError-Zweig nur die Groesse auf 0 und liess den Pfad stehen —
    # die Verify-Schleife las die Datei dann ab Byte 0 und hielt eine
    # "Set effort level to <X>"-Zeile von vor einer Stunde fuer die eigene
    # Bestaetigung (204 ohne Wirkung); umgekehrt loeste ein altes "Kept effort
    # level as <X>" eine Ablehnung fuer einen Wechsel aus, der geklappt hat.
    baseline_size: int | None = None
    if active is not None:
        baseline_path = active[0]
        try:
            stat = await asyncio.to_thread(baseline_path.stat)
            baseline_size = stat.st_size
            fresh = (time.time() - stat.st_mtime) < 20
            ended = await asyncio.to_thread(
                ChatTailerManager._transcript_suggests_turn_ended, baseline_path
            )
            if fresh and not ended:
                raise AgentBusyError()
        except OSError:
            baseline_size = None

    await _send_boss_bytes(
        f"/effort {level}".encode(), b"\r",
        delay_before_last=_BOSS_ENTER_DELAY_SECONDS,
    )

    marker = f"effort level to {level}"
    confirmed_dialog = False
    # Pro Datei der Byte-Stand, bis zu dem schon gelesen wurde. Gelesen wird
    # ausschliesslich der ZUWACHS und der wird aufsummiert — nie die ganze
    # Datei (Review 20.08.2026): Boss' echtes Transkript-Verzeichnis hat 53
    # Dateien / 981 MB, die groesste 124,9 MB. Ohne bekannten Baseline-Pfad
    # lief frueher in JEDER der 24 Runden ein voller read_text, also ~3 GB
    # Lesen und 24 aufeinanderfolgende ~125-MB-Allokationen in EINEM
    # POST /chat/effort — auf einer Docker-VM mit 5 GB Deckel.
    # Aufsummiert statt pro Runde geprueft, damit die Bestaetigungszeile auch
    # dann gefunden wird, wenn sie zwischen zwei Polls zerschnitten wurde.
    offsets: dict[Path, int] = {}
    if baseline_path is not None and baseline_size is not None:
        offsets[baseline_path] = baseline_size
    fresh_parts: list[str] = []
    for attempt in range(_EFFORT_VERIFY_ATTEMPTS * 2):  # Transkript ist traeger als der Pane
        await asyncio.sleep(_EFFORT_VERIFY_DELAY_SECONDS)
        if attempt == 2 and not confirmed_dialog:
            # Ohne Pane ist der Bestaetigungsdialog (s. _verify_effort_applied)
            # nicht sichtbar — nach ~1.5s ohne Transkript-Bestaetigung wird er
            # EINMAL blind mit Enter beantwortet (Option "Yes" ist vorgewaehlt).
            # Ist kein Dialog da, ist das Enter auf leerem Eingabefeld ein
            # No-Op. Restrisiko, dokumentiert: ein im Terminal halb getippter
            # Entwurf wuerde abgeschickt — akzeptiert, weil der Operator den
            # Wechsel gerade selbst ausgeloest hat und das Eingabefeld dafuer
            # ohnehin frei sein muss.
            confirmed_dialog = True
            await _send_boss_bytes(b"\r", delay_before_last=_BOSS_ENTER_DELAY_SECONDS)
            continue
        try:
            current = await asyncio.to_thread(find_active_session, tdir)
        except OSError:
            continue
        if current is None:
            continue
        path = current[0]
        try:
            offsets[path], zuwachs = await asyncio.to_thread(
                _read_since, path, offsets.get(path)
            )
        except OSError:
            continue
        if zuwachs:
            fresh_parts.append(zuwachs)
        fresh_text = "".join(fresh_parts)
        if marker in fresh_text:
            return
        rejected = _EFFORT_REJECTED_RE.search(fresh_text)
        if rejected is not None:
            raise EffortSwitchRejectedError(rejected.group(0))
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
    command_echo = f"/effort {level}"
    confirmed_dialog = False
    for _ in range(_EFFORT_VERIFY_ATTEMPTS):
        await asyncio.sleep(_EFFORT_VERIFY_DELAY_SECONDS)
        pane = await capture_pane(agent)
        if not pane:
            continue
        # Sessions MIT gecachtem Verlauf fragen zurueck ("Change effort
        # level? … 1. Yes, switch … 2. No, go back", Option 1 vorgewaehlt) —
        # auf frischen Wegwerf-Sessions erschien der Dialog nie, weshalb die
        # R12b-Reproduktion scheiterte und "Kept effort level as X" raetselhaft
        # blieb: das IST die Antwort der CLI, wenn der Dialog verneint wird
        # (gefunden 19.08.2026 am Boss). Der Operator hat den Wechsel bereits
        # im Regler entschieden — der Dialog wird EINMAL bestaetigt, dann
        # normal weiter verifiziert.
        # NUR hinter dem Echo DIESES Kommandos lesen (Operator-Live-Bug
        # 18.08.2026 abends): der Pane zeigt Verlauf. Eine "Kept effort
        # level as <X>"-Zeile eines FRUEHEREN Versuchs blieb sichtbar und
        # liess jeden neuen Versuch sofort als abgelehnt enden — jeder
        # "Erneut versuchen"-Klick scheiterte identisch, bis die Zeile
        # zufaellig aus dem Fenster scrollte. rfind nimmt das LETZTE Echo:
        # das ist unseres, auch wenn ein aelterer Versuch derselben Stufe
        # weiter oben steht. Solange das Echo noch nicht gerendert ist,
        # gibt es nichts auszuwerten -> weiter pollen.
        #
        # Der Zuschnitt steht seit dem Review 20.08.2026 VOR dem Dialog-Zweig,
        # nicht mehr danach: der suchte den GANZEN Pane und fiel damit auf
        # exakt denselben Alt-Scrollback herein. Beim zweiten Wechsel
        # innerhalb eines Scrollbacks feuerte er beim ERSTEN Poll auf die
        # "Change effort level?"-Zeile des vorigen Wechsels — ein Streu-Enter
        # ins LIVE-Fenster des Agenten (schickt ab, was der Operator gerade
        # tippt), ``confirmed_dialog`` verbraucht, der ECHTE Dialog nie
        # beantwortet, 409 nach Budget-Ablauf.
        echo_idx = pane.rfind(command_echo)
        if echo_idx < 0:
            continue
        tail = pane[echo_idx + len(command_echo):]
        if marker in tail:
            return True
        if not confirmed_dialog and _EFFORT_CONFIRM_DIALOG_MARKER in tail:
            confirmed_dialog = True
            await _run_docker_exec(_docker_argv(agent.slug, "Enter"))
            continue
        rejected = _EFFORT_REJECTED_RE.search(tail)
        if rejected is not None:
            # The CLI answered definitively (just not with what was asked
            # for) — stop polling immediately rather than burning the rest
            # of the attempt budget waiting for a confirmation that will
            # never come. See EffortSwitchRejectedError's docstring for the
            # live investigation behind this (ein Container-Agent, 2026-08-18).
            raise EffortSwitchRejectedError(rejected.group(0))
    return False


# ALLOWED_EFFORT_LEVELS was empirically verified (Phase-0 discovery +
# fix-round live reproduction attempts on ein Container-Agent) against this exact CLI
# build. Deliberately NOT auto-re-probed on a version mismatch: /effort
# argument commands persist to the agent's settings.json (see the module
# docstring), so an unattended reprobe on every new CLI version would
# silently change a real agent's default effort level — far worse than a
# possibly-stale level list. A drift just gets logged, once per cli_version
# fleet-wide (Redis SET NX EX dedup, same TTL as the model catalog), as a
# signal that a manual Phase-0 re-verification pass is due.
_EFFORT_LEVELS_VERIFIED_CLI_VERSION = {"claude": "2.1.234", "openclaude": "0.7.0", "omp": "16.4.6"}
_EFFORT_DRIFT_LOG_DEDUP_TTL_SECONDS = 24 * 3600  # matches the model catalog's own TTL


async def _check_effort_levels_version_drift(agent) -> None:
    """Fire-and-forget observability check — see
    ``_EFFORT_LEVELS_VERIFIED_CLI_VERSION`` above for why this only logs,
    never reprobes. Never raises; a Redis hiccup logs anyway (better a
    possible duplicate log line than a check that silently does nothing)."""
    try:
        cli_version = await resolve_cli_version(agent)
    except Exception:
        return
    harness = getattr(agent, "harness", None) or ""
    verified = _EFFORT_LEVELS_VERIFIED_CLI_VERSION.get(harness)
    if not cli_version or verified is None or cli_version == verified:
        return

    try:
        redis = await get_redis()
        claimed = await redis.set(
            RedisKeys.effort_levels_drift_logged(cli_version),
            "1", nx=True, ex=_EFFORT_DRIFT_LOG_DEDUP_TTL_SECONDS,
        )
    except Exception:
        claimed = True

    if not claimed:
        return
    logger.warning(
        "agent_chat_input: die Effort-Stufen fuer harness=%s wurden gegen %s "
        "geprueft, slug=%s faehrt aber %s — Liste/Bestaetigungstext koennen "
        "veraltet sein und brauchen einen manuellen Phase-0-Nachlauf "
        "(nie automatisch nachgeprobt: /effort persistiert in die "
        "settings.json des Agenten).",
        harness, verified, getattr(agent, "slug", None), cli_version,
    )


async def effort_capabilities(agent) -> dict[str, object]:
    """Effort-switching capability for the composer chip: ``{"effortLevels":
    [...], "canSwitchEffort": bool, "effortReason": str|None}`` — consumed by
    ``routers/agent_chat.get_chat_history`` to let the frontend build the
    chip dynamically from what the agent's actual harness supports, instead
    of hardcoding a level list. Docker/cli-bridge agents get die Stufenleiter
    IHRES Harness (``effort_levels_for`` — die gleiche, gegen die
    ``set_effort`` validiert) und loesen die Versions-Drift-Pruefung oben
    aus; jede andere Runtime (Boss, sonstige Host-Agenten) bekommt eine leere
    Liste und ``canSwitchEffort=False`` — es gibt dort keine Pane-Sonde,
    dieselbe v1-Grenze, die ``set_effort`` selbst via
    ``InputNotSupportedError`` zieht.

    ZWEI Bedingungen, seit 19.08.2026: der Harness muss ``/effort`` kennen
    UND das MODELL des Agenten muss Stufen haben. Die zweite ist neu und war
    der eigentliche Befund dieser Runde — sie kommt aus dem ``/model``-Picker
    selbst (``harness_catalog.discover_effort_support``), nicht aus einer
    Tabelle. Faellt eine der beiden aus, steht das WARUM in ``effortReason``
    (Codes siehe ``_no_effort``): das UI erklaert es am Chip, statt das
    Bedienelement wortlos verschwinden zu lassen.

    Never raises: an unsupported runtime is a normal, expected answer here
    (unlike ``set_effort``, where it's a request the caller made in error),
    so it's handled as data, not an exception."""
    try:
        kind = _target_kind(agent)
    except InputNotSupportedError:
        kind = None

    harness = getattr(agent, "harness", None)
    levels = effort_levels_for(harness)

    if kind == "docker" and not levels:
        # Fremde CLI im Container (kimi): /effort ist ein Claude-Kommando.
        # Weder Leiter noch Schaltrecht behaupten — sonst tippt ein Klick
        # Kauderwelsch in eine TUI, die es nicht versteht (kritischer
        # Test-Durchgang 18.08.2026).
        return _no_effort("foreign_harness")

    if kind == "docker" and harness == "omp":
        # Quelle ist der Pane, nicht eine Datei: die Statuszeile zeigt die
        # Stufe der LAUFENDEN Session (Shift+Tab-Wechsel landen nie in
        # settings.json). Ohne Statuszeile (TUI bootet, Fenster weg) gibt es
        # nichts zu schalten — und nichts, woran ein Wechsel zu pruefen waere.
        from app.services.omp_chat import status_line_thinking_level

        await _check_effort_levels_version_drift(agent)
        current = status_line_thinking_level(await capture_pane(agent))
        if current is None:
            caps = _no_effort("no_pane")
            caps["effortLevels"] = list(levels)
            return caps
        return {
            "effortLevels": list(levels),
            "canSwitchEffort": True,
            # ``auto`` ist keine Stufe der Leiter -> None: der Chip zeigt
            # dann ehrlich "keine feste Stufe" statt einer falschen Fuellung.
            "effort": current if current in levels else None,
            "effortShared": False,
            "effortReason": None,
            # Die Statuszeile ist JUENGER als jedes usage-Ereignis: ein
            # Shift+Tab-Wechsel steht sofort dort, waehrend das letzte usage
            # noch die Stufe des vorigen Zugs traegt (Live-Befund 03.09.2026:
            # Chip zeigte nach Neuladen "high", Pane sagte "low"). Das
            # Frontend laesst darum bei ``effortLive`` diesen Wert ueber
            # ``usage.effort`` gewinnen — bei Claude Code bleibt es beim
            # settings.json-Standard, der nur fuer NEUE Sessions gilt.
            "effortLive": True,
        }

    if kind == "docker":
        await _check_effort_levels_version_drift(agent)
        slug = getattr(agent, "slug", None) or ""
        effort = (
            await asyncio.to_thread(_persisted_effort_level, slug, levels) if slug else None
        )
        # Effort haengt am MODELL, nicht nur am Harness (Befund 19.08.2026):
        # derselbe openclaude-Agent meldet fuer sein Spark-Modell
        # "Effort not supported for qwen38-27b-unsloth-nvfp4", fuer
        # gpt-5.2-codex dagegen "Medium effort (default)". Die ehrliche
        # Quelle ist der Picker selbst — sie faellt beim Katalog-Lauf ab,
        # ohne zweites Wegwerf-Fenster.
        #
        # Das eingestellte Modell geht MIT in den Cache-Schluessel: die
        # Antwort gilt nur fuer dieses Modell, und ohne es servierte der
        # 24h-Cache nach einem Wechsel weiter die Aussage des alten
        # (Details in ``RedisKeys.model_catalog``).
        model = await asyncio.to_thread(_persisted_model, slug) if slug else None
        support = await discover_effort_support(agent, model)
        if support.get("supported") is False:
            # Mechanisch WUERDE ``/effort low`` hier sogar greifen (live
            # geprueft: die Stufe landete in der settings.json, obwohl der
            # Picker "not supported" sagt) — aber das Modell ignoriert sie.
            # Ein Regler, der eine Einstellung schreibt, die nichts bewirkt,
            # ist eine luegende Anzeige. Stattdessen: kein Regler, dafuer ein
            # GRUND, den das UI vorlesen kann.
            return {
                "effortLevels": list(levels),
                "canSwitchEffort": False,
                # KEINE Stufe zurueckgeben, obwohl eine in der settings.json
                # steht: das Frontend fuellt daraus die Saeule des Chips, und
                # eine gefuellte Saeule fuer eine Stufe, die dieses Modell
                # ignoriert, ist genau die luegende Anzeige, gegen die das Tor
                # gebaut wurde (ein Agent mit "xhigh" in der Datei zeigte 100%).
                # Leer heisst hier ehrlich "keine wirksame Stufe".
                "effort": None,
                "effortShared": False,
                "effortReason": "model_no_effort",
                # Das Modell, ueber das die CLI diese Aussage gemacht hat —
                # aus der Picker-Zeile selbst. Das UI setzt es in den
                # Erklaertext ein, statt das gerade angezeigte Modell zu
                # nehmen: nur so kann der Satz nicht ueber ein anderes Modell
                # sprechen als die Messung.
                "effortModel": support.get("model"),
            }
        return {
            "effortLevels": list(levels),
            "canSwitchEffort": True,
            # Startwert fuer den Chip, solange die Session noch kein usage-Ereignis
            # geschrieben hat. Ein spaeteres usage gewinnt immer (es kennt auch die
            # session-only-Stufen max/ultracode, die nie in der Datei landen).
            "effort": effort,
            # Container-Agenten haben ihre EIGENE settings.json — ein
            # persistierender Wechsel bleibt beim Agenten.
            "effortShared": False,
            # ``supported is None`` (kalter Cache, Lock, Probe gescheitert)
            # bleibt bewusst schaltbar: das war der Zustand vor dieser Runde
            # fuer die ganze Flotte, und "noch nicht ermittelt" ist kein
            # Grund, ein funktionierendes Bedienelement wegzunehmen.
            "effortReason": None,
        }

    # Host-Agenten mit Claude-Code-Harness (Boss): seit 19.08.2026 SCHALTBAR
    # (Operator: "klicke drauf, passiert nichts"). Kanal: die WS-Bridge tippt,
    # verifiziert wird ueber das TRANSKRIPT statt ueber den Pane (den es hier
    # nicht gibt) — /effort schreibt seine Bestaetigung als local-command-
    # stdout-Zeile in die Session-Datei (R12b-Befund, gleiche CLI).
    #
    # effortShared=True traegt die WICHTIGE Eigenheit dieses Setups: Boss
    # unsetzt CLAUDE_CONFIG_DIR und nutzt Marks eigene ~/.claude/settings.json.
    # Eine persistierende Stufe (low..xhigh) aendert damit auch den Standard
    # von Marks EIGENEN Claude-Sessions — das UI muss das am Wert sagen,
    # nicht verschweigen.
    # Beide Bedingungen zusammen — jede fuer sich hat eine echte Luecke:
    #
    # ``kind == "boss"`` statt bloss ``harness == "claude"`` (Review
    # 20.08.2026): ``_target_kind`` liefert "boss" nur fuer host + einen Slug
    # aus _BOSS_SLUGS, alles andere wirft InputNotSupportedError -> kind None.
    # Migration 0163 setzt harness='claude' aber auf JEDEN Host-Agenten mit
    # Anthropic-Runtime. Am Harness allein haette so ein Agent
    # canSwitchEffort=true gemeldet, die PERSOENLICHE ~/.claude/settings.json
    # des Operators fuer den Chip gelesen — und jeder Zug am Regler haette
    # 409 input_not_supported gegeben: genau der "klicke drauf, passiert
    # nichts"-Fehler, den diese Runde beheben sollte.
    #
    # ``levels`` zusaetzlich (openclaude-Runde): die Stufenleiter haengt am
    # Harness. Ohne Stufen gibt es keinen Regler, den man anbieten koennte —
    # und ``list(levels)`` unten waere leer, also ein Chip ohne Inhalt.
    if kind == "boss" and levels:
        effort = await asyncio.to_thread(_persisted_effort_level_at,
                                         _host_home() / ".claude" / "settings.json",
                                         levels)
        return {
            "effortLevels": list(levels),
            "canSwitchEffort": True,
            "effort": effort,
            "effortShared": True,
            "effortReason": None,
        }
    return _no_effort("no_pane" if kind is None else "foreign_harness")


def _no_effort(reason: str) -> dict[str, object]:
    """Die "kein Effort"-Antwort MIT Begruendung. Der Grund ist der ganze
    Unterschied zwischen "das Bedienelement fehlt" und "das Bedienelement
    fehlt, WEIL …" — das UI kann es erklaeren statt nur auszublenden.

    Codes (maschinenlesbar, der Text gehoert ins Frontend):
    - ``foreign_harness``: diese CLI hat kein Effort-Pendant (kimi).
    - ``no_pane``: die Runtime hat keinen steuerbaren Kanal (Hermes, Jarvis).
    - ``model_no_effort``: Harness kann es, das MODELL des Agenten nicht.

    ``effortModel`` ist hier immer ``None``: nur beim modellbedingten Fall
    hat die CLI ueberhaupt ein Modell genannt. Das Feld steht trotzdem in
    JEDER Antwort mit Grund, damit das Frontend eine feste Form hat."""
    return {
        "effortLevels": [], "canSwitchEffort": False, "effort": None,
        "effortShared": False, "effortReason": reason, "effortModel": None,
    }


# Built-in slash commands — static, not discovered (no CLI-side enumeration
# API exists). "model"/"effort" descriptions are live-verified (Phase-0
# discovery, exact CLI autocomplete text); the rest are Claude Code's
# well-known standard commands, described generically since they weren't
# individually probed live.
_BUILTIN_SLASH_COMMANDS: tuple[dict[str, str | None], ...] = (
    {"name": "model", "description": "Set the AI model for Claude Code"},
    {"name": "effort", "description": "Set effort level for model usage"},
    {"name": "clear", "description": "Clear the conversation history"},
    {"name": "compact", "description": "Compact the conversation to free up context"},
    {"name": "context", "description": "Show context window usage"},
    {"name": "status", "description": "Show session status"},
    {"name": "help", "description": "Show available commands"},
    {"name": "resume", "description": "Resume a previous session"},
)

# openclaude bringt eine EIGENE, deutlich groessere Builtin-Liste mit — live
# durch den Kommando-Picker geblaettert (19.08.2026). Der Picker mischt
# Builtins und installierte Skills; hier stehen NUR die Builtins, weil die
# Skills bereits ueber ``_discover_skill_commands`` kommen (sonst behaupten
# wir Skills, die dieser Agent gar nicht installiert hat).
#
# Beschreibungen: bewusst ``None``, ausser wo der Picker sie woertlich
# gezeigt hat (``/effort``, eigene Aufnahme). Erfundene Einzeiler waeren
# genau die Sorte plausibler Falschaussage, die spaeter niemand mehr
# nachprueft — ein leeres Feld ist ehrlicher.
_OPENCLAUDE_BUILTIN_SLASH_COMMANDS: tuple[dict[str, str | None], ...] = tuple(
    {"name": name, "description": "Set effort level for model usage" if name == "effort" else None}
    for name in (
        "add-dir", "agents", "branch", "btw", "buddy", "cache-probe", "cache-stats",
        "clear", "color", "compact", "config", "context", "copy", "cost", "debug",
        "diff", "doctor", "effort", "exit", "export", "feedback", "help", "hooks",
        "ide", "init", "insights", "knowledge", "loop", "mcp", "memory", "mobile",
        "model", "onboard-github", "permissions", "plan", "plugin", "pr-comments",
        "provider", "release-notes", "reload-plugins", "rename", "resume", "review",
        "rewind", "security-review", "skills", "stats", "status", "statusline",
        "stickers", "summarize", "tasks", "terminal-setup", "theme", "usage", "vim",
        "wiki",
    )
)

_BUILTIN_SLASH_COMMANDS_BY_HARNESS: dict[str, tuple[dict[str, str | None], ...]] = {
    "claude": _BUILTIN_SLASH_COMMANDS,
    "openclaude": _OPENCLAUDE_BUILTIN_SLASH_COMMANDS,
}

_SLASH_COMMANDS_CACHE_TTL_SECONDS = 60
_slash_commands_cache: dict[str, tuple[float, list[dict[str, str | None]]]] = {}


def _persisted_effort_level_at(
    path: Path, levels: tuple[str, ...] = ALLOWED_EFFORT_LEVELS
) -> str | None:
    """Wie ``_persisted_effort_level``, aber fuer einen expliziten Pfad —
    Boss' effektive Config ist ~/.claude/settings.json (geteilt mit dem
    Operator), nicht das mc-Agenten-Muster. Fail-silent -> None."""
    try:
        with open(path) as f:
            level = json.load(f).get("effortLevel")
    except Exception:
        return None
    return level if level in levels else None


def _persisted_effort_level(
    slug: str, levels: tuple[str, ...] = ALLOWED_EFFORT_LEVELS
) -> str | None:
    """Die im ``settings.json`` des Agenten hinterlegte Effort-Stufe — der
    Standard, mit dem JEDE neue Session startet.

    Warum das hier gebraucht wird (Operator-Befund 18.08.2026): Der Effort-Chip
    im Composer hing allein am ``usage``-Ereignis aus dem Transkript. Eine frisch
    gestartete Session hat noch keines — also fehlte das Bedienelement komplett,
    und der Effort war schlicht nicht schaltbar, bis der Agent zufaellig einmal
    gearbeitet hatte. Die Datei ist die ehrliche Zweitquelle: was in ihr steht,
    gilt bis eine Session es ueberschreibt (``max``/``ultracode`` tun das nur
    fuer sich selbst und stehen darum korrekt NICHT in der Datei).

    ``levels`` ist die Leiter des jeweiligen Harness: ein ``ultracode`` in der
    Datei eines openclaude-Agenten ist fuer DESSEN CLI kein gueltiger Wert
    und darf nicht als aktuelle Stufe durchgereicht werden.

    Fail-silent: fehlende Datei, kaputtes JSON oder ein unbekannter Wert geben
    ``None`` — das UI zeigt dann ``auto``, statt etwas zu behaupten."""
    try:
        path = _host_home() / ".mc" / "agents" / slug / "claude-config" / "settings.json"
        with open(path) as f:
            level = json.load(f).get("effortLevel")
    except Exception:
        return None
    return level if level in levels else None


def _persisted_model_at(path: Path) -> str | None:
    """Wie ``_persisted_model``, aber fuer einen expliziten Pfad — Boss'
    effektive Config ist ~/.claude/settings.json (geteilt mit dem Operator).
    Fail-silent -> None."""
    try:
        with open(path) as f:
            model = json.load(f).get("model")
    except Exception:
        return None
    return model if isinstance(model, str) and model.strip() else None


def _persisted_model(slug: str) -> str | None:
    """Das im ``settings.json`` des Agenten hinterlegte Modell — der Standard,
    mit dem jede neue Session startet. Gleiche Rolle wie
    ``_persisted_effort_level`` (Operator-Befund 18.08.2026 abends): das
    Modell-Label im Composer hing allein am usage-Ereignis, eine frische
    Session zeigte darum "—" statt des tatsaechlich eingestellten Modells.

    Der Wert kommt in ZWEI Gestalten vor, beide echt beobachtet in der Flotte:
    als Kurz-Alias ("sonnet", von ``/model sonnet`` geschrieben) oder als volle
    Modell-ID ("claude-sonnet-5", vom Config-Renderer). Beide werden verbatim
    durchgereicht — die Zuordnung zum Dropdown-Eintrag macht das Frontend
    (Alias == command; volle ID via settings.model_aliases hier NICHT
    aufloesen, sonst behaupten wir eine Zuordnung, die der Renderer nie
    getroffen hat). Fail-silent: fehlt/kaputt/leer -> None."""
    try:
        path = _host_home() / ".mc" / "agents" / slug / "claude-config" / "settings.json"
        with open(path) as f:
            model = json.load(f).get("model")
    except Exception:
        return None
    return model if isinstance(model, str) and model.strip() else None


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
    # Die Builtins sind CLI-Vokabular und pro Harness verschieden. Fuer eine
    # fremde CLI (kimi, omp) waeren sie falsche Versprechen — dort bleibt die
    # Liste leer, bis deren Harness eigene Kommandos meldet.
    builtins = _BUILTIN_SLASH_COMMANDS_BY_HARNESS.get(getattr(agent, "harness", None) or "")
    if builtins is None:
        return {"slashCommands": []}

    commands: list[dict[str, str | None]] = list(builtins)

    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    if runtime == "cli-bridge" and slug:
        commands = commands + await _discover_skill_commands(slug)

    return {"slashCommands": commands}


# Aliasse, die Claude Code annimmt, aber im /model-Picker VERSCHWEIGT, bis sie
# einmal gewaehlt wurden. Live 03.09.2026 (2.1.259, OAuth-Token im Container):
# der Picker listete Default/Sonnet/Opus/Haiku; ``/model fable`` wurde trotzdem
# angenommen, persistierte ``"model": "fable"``, die Statuszeile zeigte
# "Fable 5.1" — und erst danach stand die Zeile im Picker. Der Katalog spiegelt
# den Picker und verschwieg Fable darum jedem Claude-Agenten (Marks Befund).
# Nur fuer Harness "claude": openclaude persistiert auch ein falsches Token.
_PICKER_HIDDEN_CLAUDE_ALIASES = ("fable",)


async def model_options_capabilities(agent) -> dict[str, object]:
    """``{"modelOptions": [{"command": str, "label": str,
    "contextWindow": int|None}, ...]}`` — the composer's model-switcher
    dropdown.

    PRIMARY source (harness-catalog round): ``harness_catalog
    .discover_model_catalog(agent)`` — the agent's OWN ``/model`` picker
    rows, discovered live from a throwaway session and Redis-cached by
    ``(harness, cli_version)``. FALLBACK, used only when that catalog is
    empty (cold cache, discovery not finished yet, or genuinely no harness
    for this runtime): ``settings.model_aliases`` (config-driven —
    ``"default"`` is just another alias there, not special-cased). Either
    way, ``contextWindow`` comes from ``transcript_chat
    .resolve_context_window`` using the observed-map + config-seed tiers
    (no current-session statusline tier here — this isn't a specific
    session's usage event) — same resolution chain usage events use, so the
    frontend never needs its own hardcoded model/window map.

    Not gated by agent runtime in the sense of returning nothing for a host
    agent — ``discover_model_catalog`` already returns ``[]`` for those (no
    harness), which falls straight through to the SAME static
    ``model_aliases`` fallback every agent gets when its catalog is
    unavailable. Never raises — catalog discovery is fully fail-silent on
    its own (see ``harness_catalog``)."""
    harness = getattr(agent, "harness", None)
    if harness not in _EFFORT_LEVELS_BY_HARNESS:
        # Claude-Aliasse ("/model sonnet") in einer fremden CLI sind
        # Kauderwelsch — lieber gar keine Auswahl anbieten. Das persistierte
        # Modell wird unten trotzdem nur fuer docker+bekannten Harness gelesen.
        return {"modelOptions": [], "model": None}

    # Persistiertes Modell NUR fuer docker/cli-bridge lesen: nur dort IST
    # ~/.mc/agents/<slug>/claude-config die effektive Config. Boss (host)
    # unsetzt CLAUDE_CONFIG_DIR und liest ~/.claude/ — sein alter
    # ~/.mc/agents/boss-Ordner liegt seit April brach und lieferte beim
    # ersten Live-Test prompt ein Geister-Modell ("glm-5.1:cloud"), das Boss
    # nie faehrt. Lieber ehrliches None als eine falsche Behauptung.
    #
    # Steht VOR dem Katalog-Aufruf, weil das Modell in dessen Cache-Schluessel
    # gehoert — und weil es derselbe Wert sein muss wie in
    # ``effort_capabilities``, sonst zahlt ein Request zwei Erkennungslaeufe.
    try:
        kind = _target_kind(agent)
    except InputNotSupportedError:
        kind = None
    slug = getattr(agent, "slug", None) or ""
    if kind == "docker" and slug:
        model = await asyncio.to_thread(_persisted_model, slug)
    elif kind == "boss":
        # Boss liest ~/.claude/settings.json (CLAUDE_CONFIG_DIR unset) — NICHT
        # das mc-Agenten-Muster, aus dem frueher ein Geister-Modell kam.
        # Ohne diesen Zweig stand nach /clear ein "—" im Composer, bis die
        # erste Nachricht ein usage-Ereignis erzeugte (Operator-Befund
        # 19.08.2026, Screenshot).
        model = await asyncio.to_thread(
            _persisted_model_at, _host_home() / ".claude" / "settings.json"
        )
    else:
        model = None

    catalog = await discover_model_catalog(agent, model if kind == "docker" else None)
    observed = await get_observed_model_windows()

    if catalog:
        rows = catalog
    elif harness == "claude":
        rows = [
            {"command": command, "label": command.capitalize()}
            for command in settings.model_aliases
        ]
    else:
        # ``settings.model_aliases`` ist Claude-Code-Vokabular aus der
        # Konfiguration. Bei openclaude waere es geraten — und openclaude
        # VALIDIERT (``Model 'x' not found``), setzt aber sofort und dauerhaft,
        # was es annimmt. Ein leeres Dropdown, bis der Katalog steht, ist
        # billiger als ein Klick, der einen echten Agenten umschaltet.
        rows = []

    if harness == "claude":
        known = {row["command"] for row in rows}
        rows = list(rows) + [
            {"command": alias, "label": alias.capitalize()}
            for alias in _PICKER_HIDDEN_CLAUDE_ALIASES
            if alias not in known and alias in settings.model_aliases
        ]

    alias_to_model_id = settings.model_aliases
    options = []
    for row in rows:
        command = row["command"]
        model_id = alias_to_model_id.get(command, command)
        options.append({
            "command": command,
            "label": row["label"],
            "contextWindow": resolve_context_window(model_id, observed),
        })
    # Startwert fuers Modell-Label, solange die Session noch kein usage-Ereignis
    # geschrieben hat — ein spaeteres usage gewinnt immer (es kennt das Modell
    # des laufenden Zuges, nicht nur den persistierten Standard).
    return {"modelOptions": options, "model": model}
