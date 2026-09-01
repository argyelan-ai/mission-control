"""Transcript chat parser — normalizes Claude Code JSONL lines into chat events.

Pure functions only (no I/O, no DB, no framework deps) — consumed by A3/A4 to
build the per-session chat view and mirrored in the frontend's chatTypes.ts.

Normalized event shapes (plain dicts, JSON-serializable):
  {"kind":"message","uuid":str,"ts":str,"role":"user"|"assistant","text":str,
   "model":str|None,"sidechain":bool}
  {"kind":"tool","uuid":str,"ts":str,"name":str,"title":str,"detail":dict,
   "toolUseId":str|None,"result":str|None,"status":"done"|"error",
   "stats":{"additions":int,"deletions":int}|None,"sidechain":bool}
  {"kind":"thinking","uuid":str,"ts":str,"text":str,"sidechain":bool}
  {"kind":"command","uuid":str,"ts":str,"command":str,"result":str|None}
  {"kind":"usage","uuid":str,"ts":str,"inputTokens":int,"outputTokens":int,
   "model":str|None,"effort":str|None,"contextWindow":int|None,
   "components":{"input":int,"cacheRead":int,"cacheCreation":int,"output":int}}

``inputTokens`` is deliberately the SUM of the three input-side fields
(``input`` + ``cacheRead`` + ``cacheCreation``); ``components`` carries the same
numbers unsummed for the context breakdown view. ``_stamp_usage_source``
replaces ``components`` with the CLI statusline's ``current_usage`` when that is
fresh — the transcript line describes one turn, the statusline describes the
whole live context window, so it is the better answer to "where did the window
go" whenever it exists.

`parse_transcript_line` also emits an internal ``_tool_result`` event for
``tool_result`` content blocks (type=="user" lines) — ``{"kind":"_tool_result",
"tool_use_id":str,"content":Any,"is_error":bool}``. ``read_history`` merges
these onto their matching ``tool`` event by ``tool_use_id`` == ``toolUseId``
(needed to disambiguate parallel tool calls within one assistant turn);
they never reach the frontend on their own.

A slash command the operator runs IN-SESSION (not typed as ordinary chat
text) writes THREE separate, chained user entries instead of one — a
``<local-command-caveat>`` boilerplate line, a
``<command-name>/<command-message>/<command-args>`` line, and a
``<local-command-stdout>``/``<local-command-stderr>`` line (real payload +
full detail: see ``_parse_local_command_wrapper``'s docstring). The caveat
is suppressed entirely (no event). The command line becomes a ``command``
event. The stdout/stderr line becomes an internal ``_command_result`` event
(``{"kind":"_command_result","parent_uuid":str|None,"content":str,
"is_error":bool}``) merged onto its ``command`` event by ``parentUuid`` ==
that event's own ``uuid`` — same pattern as ``_tool_result``, just keyed by
parent chain instead of a tool-call id; never reaches the frontend on its
own.

``message.content`` has TWO shapes in real transcripts: the API's list-of-
blocks form, and a plain string — real interactively-typed user turns write
the latter (fix round 5, live-gate finding: string content silently produced
zero events, symptom "I don't see my own message"). Both ``_parse_user_entry``
and ``_parse_assistant_entry`` normalize a string ``content`` into a single
``{"type":"text","text":...}`` block up front so the rest of the block-loop
logic (slash-command rule included, for user entries) runs unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import sse
from app.services.harness_catalog import get_observed_model_windows, observe_model_window
from app.services.pane_state import capture_pane, process_alive
from app.redis_client import RedisKeys
from app.services.token_harvester import _host_home, _should_attribute_boss_path

logger = logging.getLogger("mc.transcript_chat")

_DETAIL_TRUNCATE_LEN = 2000
_TITLE_MAX_LEN = 80

# Session-scan limits (find_active_session / transcript_allowed)
_LIVE_WINDOW_SECONDS = 60
_BOSS_SCAN_LINES = 20

# resolve_aliveness's transcript-age fallback (host/boss agents, or a docker
# agent whose process_alive check itself came back unknown): a transcript
# this recent is plausibly still an ongoing session, one this stale is not.
_ALIVENESS_IDLE_MAX_AGE_SECONDS = 12 * 3600  # 12 hours

# Host-runtime slugs that resolve to the Boss's own ~/.claude session dir —
# every other host agent (Hermes, Jarvis) has no transcript at all.
_BOSS_SLUGS = ("boss", "boss-host")

# Tools whose title is built from a file_path basename, prefixed "Read".
_FILE_PATH_READ_TOOLS = {"Read", "NotebookEdit"}

# Local-command wrapper tags — the TUI writes these as their OWN dedicated
# user entries (string content) when the operator runs a slash command
# in-session, chained via parentUuid: caveat -> command -> stdout/stderr.
# Real captured payload (Davinci, 2026-08-17, redacted of nothing — none of
# these three lines carry personal data):
#   {"type":"user","message":{"content":"<local-command-caveat>Caveat: The
#    messages below were generated by the user while running local commands.
#    DO NOT respond to these messages or otherwise consider them in your
#    response unless the user explicitly asks you to.</local-command-caveat>"},
#    "isMeta":true, "uuid":"25b3bd6f-..."}
#   {"type":"user","message":{"content":"<command-name>/effort</command-name>\n
#    <command-message>effort</command-message>\n
#    <command-args>low</command-args>"}, "uuid":"3e19b93b-...",
#    "parentUuid":"25b3bd6f-..."}
#   {"type":"user","message":{"content":"<local-command-stdout>Kept effort
#    level as auto</local-command-stdout>"}, "uuid":"6e488fb4-...",
#    "parentUuid":"3e19b93b-..."}
# Before this fix these all fell through to the generic text-block path and
# rendered as raw user chat bubbles (the caveat's own instruction text, the
# literal XML tags, the raw stdout) — an operator-visible bug.
_LOCAL_COMMAND_CAVEAT_RE = re.compile(
    r"^<local-command-caveat>.*</local-command-caveat>$", re.DOTALL
)
_COMMAND_WRAPPER_RE = re.compile(
    r"^<command-name>(?P<name>[^<]*)</command-name>\s*"
    r"<command-message>[^<]*</command-message>\s*"
    r"(?:<command-args>(?P<args>[^<]*)</command-args>\s*)?$",
    re.DOTALL,
)
_LOCAL_COMMAND_STDOUT_RE = re.compile(
    r"^<local-command-stdout>(?P<content>.*)</local-command-stdout>$", re.DOTALL
)
_LOCAL_COMMAND_STDERR_RE = re.compile(
    r"^<local-command-stderr>(?P<content>.*)</local-command-stderr>$", re.DOTALL
)


# Teamkollegen-Nachricht (Operator-Befund 19.08.2026): Startet ein Agent
# Subagenten, schreibt Claude Code deren Rueckmeldungen als gewoehnliche
# USER-Turns ins Transkript. Der Chat zeigte sie darum als Nachrichten des
# Operators an — inklusive eines langen Sicherheits-Hinweises, der sich an das
# MODELL richtet und in jeder solchen Nachricht identisch ist.
#
# Bewusst ENG gefasst: der Text muss AB SEINEM ANFANG den Umschlag tragen
# (eine Einleitungszeile davor ist erlaubt, aber nicht noetig). Eine echte
# Nachricht, die zufaellig ueber Teamkollegen spricht, darf nicht still
# verschwinden.
#
# Warum der Anker am UMSCHLAG haengt und nicht an der Einleitungszeile: live
# nachgezaehlt (19.08.2026, 610 echte Turns) beginnen 5 Turns direkt mit
# ``<teammate-message …>``, ganz ohne Prosa-Vorspann — die landeten vorher als
# rechtsbuendige Blase, die der Operator nie getippt hat. Ausserdem ist die
# Prosa Text von Claude Code selbst und aendert sich ohne Ankuendigung.
_TEAMMATE_INTRO_RE = re.compile(r"^Another Claude session sent a message:[ \t]*\n?")
_TEAMMATE_OPEN_RE = re.compile(r"<teammate-message(?P<attrs>[^>]*)>")
_TEAMMATE_CLOSE_TAG = "</teammate-message>"
# Auf Wort-/Zeichengrenze verankert: ohne den Vorlauf fand ``search`` in
# ``from_teammate_id="spoof" teammate_id="real"`` zuerst ``spoof`` und schrieb
# die Kachel dem falschen Absender zu.
_TEAMMATE_ID_RE = re.compile(r'(?:^|\s)teammate_id="(?P<id>[^"]*)"')


def _parse_teammate_message(
    text: str, msg_uuid: str, ts: str, sidechain: bool
) -> list[dict[str, Any]] | None:
    """Erkennt eingespeiste Teamkollegen-Nachrichten und macht daraus Ereignisse
    mit eigener Rolle — EINES JE UMSCHLAG. ``None`` = keine solche Nachricht,
    der Aufrufer behandelt die Zeile normal weiter.

    Behalten wird, was der Operator wissen will: WER geschrieben hat und WAS.
    Der Boilerplate-Absatz dahinter faellt weg — er ist Anweisung an das Modell,
    kein Gespraechsinhalt, und in jeder Nachricht identisch.

    Zwei teuer bezahlte Details:

    * **Ein Ereignis je Block.** Claude Code buendelt mehrere Rueckmeldungen
      unter EINER Einleitungszeile — live 62 von 610 Turns, Spitzenwert 41
      Bloecke. Das fruehere lazy ``(?P<payload>.*?)`` stoppte am ersten
      Schluss-Tag und ersetzte den ganzen Turn durch ein Ereignis mit nur
      dieser ersten Nutzlast: alles danach war still geloescht.
    * **``find`` statt Rueckverfolgung.** Der Scan laeuft hier synchron auf dem
      asyncio-Loop (``ChatTailerManager._run``) und blockiert damit alle
      anderen Tailer mit. Ein Subagenten-Bericht ist gut und gern hunderte KB;
      gemessen kostete das lazy Muster dort 9,4 ms je Zeile, ``str.find`` plus
      Slicing 0,02 ms."""
    pos = 0
    intro = _TEAMMATE_INTRO_RE.match(text)
    if intro is not None:
        pos = intro.end()
    # Ab hier MUSS der Umschlag stehen — nur fuehrender Leerraum davor. Damit
    # bleibt die Erkennung eng: wer ueber die Zeichenfolge schreibt, statt sie
    # als Umschlag zu fuehren, bekommt keine Kachel.
    while pos < len(text) and text[pos].isspace():
        pos += 1

    events: list[dict[str, Any]] = []
    while True:
        open_m = _TEAMMATE_OPEN_RE.match(text, pos)
        if open_m is None:
            break
        attrs = open_m.group("attrs") or ""
        id_match = _TEAMMATE_ID_RE.search(attrs)
        if attrs.endswith("/"):
            # Selbstschliessend: kein Inhalt, aber sicher nicht der Operator.
            payload = ""
            pos = open_m.end()
        else:
            close = text.find(_TEAMMATE_CLOSE_TAG, open_m.end())
            if close == -1:
                # Abgeschnittene Zeile. Der Umschlag ist da, also gehoert der
                # Rest dem Teamkollegen — lieber vollstaendig als Kachel zeigen
                # als faelschlich als Nachricht des Operators.
                payload = text[open_m.end() :]
                pos = len(text)
            else:
                payload = text[open_m.end() : close]
                pos = close + len(_TEAMMATE_CLOSE_TAG)
        events.append(
            {
                "kind": "message",
                # Eigene, stabile uuid je Block: ``seen_uuids`` (Dedup) und der
                # React-Key im Verlauf wuerfen Geschwister mit gleicher uuid
                # sonst wieder weg. Der erste Block behaelt die Zeilen-uuid,
                # damit alles, was sie schon referenziert, unveraendert passt.
                "uuid": msg_uuid if not events else f"{msg_uuid}#tm{len(events)}",
                "ts": ts,
                "role": "teammate",
                "teammate": id_match.group("id") if id_match else None,
                "text": payload.strip(),
                "model": None,
                "sidechain": sidechain,
            }
        )
        while pos < len(text) and text[pos].isspace():
            pos += 1

    return events or None


def _parse_local_command_wrapper(
    text: str, msg_uuid: str, ts: str, parent_uuid: str | None
) -> list[dict[str, Any]] | None:
    """Recognizes the three local-command wrapper shapes in a string-content
    user entry. Returns ``None`` (not a wrapper — caller falls through to
    the normal text/slash-command handling) or the list of events this line
    produces:

    - caveat -> ``[]`` (suppressed entirely, no event at all — it's boilerplate
      instruction text for the model, not something an operator should see
      as a chat bubble).
    - command-name/message/args -> one ``command`` event (``command`` =
      ``"<name> <args>"``, or just ``<name>`` when there are no args),
      ``result`` defaulted to ``None`` pending a possible stdout/stderr merge.
    - stdout/stderr -> one internal ``_command_result`` event
      (``{"kind":"_command_result","parent_uuid":str|None,"content":str,
      "is_error":bool}``), matched onto its command event by ``parentUuid``
      == the command event's own ``uuid`` — same merge pattern as
      ``_tool_result`` onto ``tool`` events, just keyed by parent chain
      instead of a tool-call id, and never reaches the frontend on its own."""
    if _LOCAL_COMMAND_CAVEAT_RE.match(text):
        return []

    m = _COMMAND_WRAPPER_RE.match(text)
    if m is not None:
        name = m.group("name").strip()
        args = (m.group("args") or "").strip()
        command = f"{name} {args}" if args else name
        return [
            {
                "kind": "command",
                "uuid": msg_uuid,
                "ts": ts,
                "command": command,
                "result": None,
            }
        ]

    m = _LOCAL_COMMAND_STDOUT_RE.match(text)
    if m is not None:
        return [
            {
                "kind": "_command_result",
                "parent_uuid": parent_uuid,
                "content": m.group("content"),
                "is_error": False,
            }
        ]

    m = _LOCAL_COMMAND_STDERR_RE.match(text)
    if m is not None:
        return [
            {
                "kind": "_command_result",
                "parent_uuid": parent_uuid,
                "content": m.group("content"),
                "is_error": True,
            }
        ]

    return None


def resolve_context_window(
    model: str | None, observed: dict[str, int] | None = None
) -> int | None:
    """Resolves a model name to its context window size (tokens). Shared by
    both consumers of ``parse_transcript_line`` — ``read_history`` and the
    live tailer — since both funnel through this one call site in
    ``_parse_assistant_entry``.

    Matching order (harness-catalog round — the OBSERVED tier is new; the
    CURRENT-SESSION-statusline tier that outranks even this whole function
    is applied separately, AFTER parsing, by ``_stamp_usage_source``):
    1. ``observed`` (an EXACT match only) — a model->window map the caller
       fetched from ``harness_catalog.get_observed_model_windows()`` (every
       FRESH statusline-state read anywhere in the fleet feeds this Redis
       hash; ``None``/``{}`` here just skips this tier, keeping this
       function itself Redis-free and pure/synchronous — see
       ``harness_catalog``'s module docstring for why the dependency runs
       this direction and not the reverse).
    2. Exact match against a configured key in ``settings.context_windows``
       (the static, config-seeded fallback — demoted from primary to
       tertiary this round, not deleted: still what answers before any
       observation exists).
    3. The LONGEST configured key that is a prefix of ``model`` (handles
       dated/versioned model strings, e.g. a future
       "claude-sonnet-4-6-20261201" against the configured "claude-sonnet-4-6").
    4. ``model`` contains the literal substring ``"[1m]"`` (Anthropic's 1M-
       context beta suffix) -> 1,000,000.
    5. Otherwise ``None`` — an unknown model gets no number rather than a
       guessed one.
    """
    if not model:
        return None

    if observed and model in observed:
        return observed[model]

    windows = settings.context_windows
    if model in windows:
        return windows[model]

    prefix_matches = [key for key in windows if model.startswith(key)]
    if prefix_matches:
        return windows[max(prefix_matches, key=len)]

    if "[1m]" in model:
        return 1_000_000

    return None


def parse_transcript_line(
    line: str, observed_windows: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    """One raw JSONL line -> 0..n normalized chat events. Never raises.

    ``observed_windows`` (harness-catalog round) is passed straight through
    to ``resolve_context_window`` for a ``usage`` event's ``contextWindow``
    estimate — see that function's docstring for the full precedence chain.
    Optional and ``None`` by default so this stays call-compatible with
    every existing caller; only ``read_history`` and the tailer, which have
    a fetched observed-map available, pass a real dict."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        logger.debug("transcript_chat: malformed JSON line, skipping")
        return []

    if not isinstance(d, dict):
        logger.debug("transcript_chat: line is not a JSON object, skipping")
        return []

    entry_type = d.get("type")
    try:
        if entry_type == "user":
            return _parse_user_entry(d)
        if entry_type == "assistant":
            return _parse_assistant_entry(d, observed_windows)
    except Exception:
        logger.debug("transcript_chat: failed to parse %s entry", entry_type, exc_info=True)
        return []

    return []


def _parse_user_entry(d: dict[str, Any]) -> list[dict[str, Any]]:
    msg_uuid = d.get("uuid")
    ts = d.get("timestamp")
    message = d.get("message")
    if not msg_uuid or not ts or not message:
        return []

    content = message.get("content")
    if isinstance(content, str):
        wrapper_events = _parse_local_command_wrapper(
            content, msg_uuid, ts, d.get("parentUuid")
        )
        if wrapper_events is not None:
            return wrapper_events
        # Real interactive user turns write ``message.content`` as a plain
        # string, not the list-of-blocks shape tool-driven turns use —
        # verified live (fix round 5): {"type":"user","message":{"content":
        # "..."}}. Normalize to the one-text-block shape so the existing
        # block loop below (slash-command rule included) handles it unchanged.
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []

    sidechain = bool(d.get("isSidechain", False))
    events: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if text is None:
                continue
            notification_evs = _parse_task_notification(text, msg_uuid, ts)
            teammate_evs = (
                None if notification_evs is not None
                else _parse_teammate_message(text, msg_uuid, ts, sidechain)
            )
            if notification_evs is not None:
                events.extend(notification_evs)
            elif teammate_evs is not None:
                events.extend(teammate_evs)
            elif text.startswith("/") and "\n" not in text:
                events.append(
                    {
                        "kind": "command",
                        "uuid": msg_uuid,
                        "ts": ts,
                        "command": text,
                        "result": None,
                    }
                )
            else:
                events.append(
                    {
                        "kind": "message",
                        "uuid": msg_uuid,
                        "ts": ts,
                        "role": "user",
                        "text": text,
                        "model": None,
                        "sidechain": sidechain,
                    }
                )
        elif block_type == "tool_result":
            events.append(
                {
                    "kind": "_tool_result",
                    "tool_use_id": block.get("tool_use_id"),
                    "content": block.get("content"),
                    "is_error": bool(block.get("is_error", False)),
                }
            )

    return events


def _parse_assistant_entry(
    d: dict[str, Any], observed_windows: dict[str, int] | None = None
) -> list[dict[str, Any]]:
    msg_uuid = d.get("uuid")
    ts = d.get("timestamp")
    message = d.get("message")
    if not msg_uuid or not ts or not message:
        return []

    content = message.get("content")
    if isinstance(content, str):
        # Assistant entries are API-shaped (block arrays) in every real
        # transcript observed so far — but the user-entry string-content
        # discovery (fix round 5, see above) means the format isn't as
        # rigid as assumed. Tolerate it defensively the same way rather
        # than silently drop a whole assistant turn (including its usage
        # event) if it ever shows up here too.
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []

    sidechain = bool(d.get("isSidechain", False))
    model = message.get("model")
    if model == "<synthetic>":
        # Claude Codes Marker fuer intern erzeugte Nachrichten (z.B.
        # Fehler-Hinweise) — kein echtes Modell, kein API-Aufruf. Ungefiltert
        # landete er woertlich als Modell-Label im Composer (live gesehen am
        # Researcher, 18.08.2026: Chip zeigte "<synthetic>"). None laesst die
        # Anzeige ehrlich auf den persistierten Standard zurueckfallen.
        model = None
    events: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if text is None:
                continue
            events.append(
                {
                    "kind": "message",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "role": "assistant",
                    "text": text,
                    "model": model,
                    "sidechain": sidechain,
                }
            )
        elif block_type == "thinking":
            text = block.get("thinking")
            # Claude Code schreibt den Denkverlauf NICHT im Klartext: der Block
            # traegt ein leeres ``thinking`` und nur eine verschluesselte
            # ``signature`` (live gemessen 01.09.2026, 115 Bloecke in einer
            # Sitzung). Ein Ereignis daraus erzeugte eine Denk-Blase, die sich
            # aufklappen liess und leer war — eine Schublade, die Inhalt
            # verspricht, den die Quelle nicht hat. Erst wenn der Denkverlauf
            # aus dem Terminal-Strom kommt, gibt es wieder etwas zu zeigen.
            if text is None or not text.strip():
                continue
            events.append(
                {
                    "kind": "thinking",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "text": text,
                    "sidechain": sidechain,
                }
            )
        elif block_type == "tool_use":
            name = block.get("name")
            if not name:
                continue
            tool_input = block.get("input") or {}
            events.append(
                {
                    "kind": "tool",
                    "uuid": msg_uuid,
                    "ts": ts,
                    "name": name,
                    "title": build_tool_title(name, tool_input),
                    "detail": _truncate_detail(tool_input),
                    "toolUseId": block.get("id"),
                    "result": None,
                    "status": "done",
                    "stats": None,
                    "sidechain": sidechain,
                }
            )

    usage = message.get("usage")
    if usage:
        # `inputTokens` stays the SUM of the three input-side fields — every
        # existing consumer (the context ring's fallback estimate) depends on
        # that. `components` keeps them apart as well, so a breakdown view can
        # show where the window actually went without re-deriving anything.
        components = {
            "input": usage.get("input_tokens") or 0,
            "cacheRead": usage.get("cache_read_input_tokens") or 0,
            "cacheCreation": usage.get("cache_creation_input_tokens") or 0,
            "output": usage.get("output_tokens") or 0,
        }
        input_tokens = (
            components["input"] + components["cacheRead"] + components["cacheCreation"]
        )
        events.append(
            {
                "kind": "usage",
                "uuid": msg_uuid,
                "ts": ts,
                "inputTokens": input_tokens,
                "outputTokens": components["output"],
                "model": model,
                "effort": d.get("effort"),
                "contextWindow": resolve_context_window(model, observed_windows),
                "components": components,
            }
        )

    return events


def _truncate_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Truncates string values longer than 2000 chars, appending an ellipsis."""
    truncated: dict[str, Any] = {}
    for key, value in detail.items():
        if isinstance(value, str) and len(value) > _DETAIL_TRUNCATE_LEN:
            truncated[key] = value[:_DETAIL_TRUNCATE_LEN] + "…"
        else:
            truncated[key] = value
    return truncated


def build_tool_title(name: str, tool_input: dict[str, Any]) -> str:
    """Builds a short, human-readable title for a tool_use event.

    'Read' + {'file_path': '/x/y.py'} -> 'Read y.py' etc.
    """
    if name in _FILE_PATH_READ_TOOLS:
        title = f"Read {_basename(tool_input.get('file_path'))}"
    elif name == "Write":
        title = f"Write {_basename(tool_input.get('file_path'))}"
    elif name == "Edit":
        title = f"Edit {_basename(tool_input.get('file_path'))}"
    elif name == "Bash":
        title = f"$ {tool_input.get('command', '')}"
    elif name in ("Grep", "Glob"):
        title = f'Search "{tool_input.get("pattern", "")}"'
    elif name == "WebSearch":
        title = f'Web "{tool_input.get("query", "")}"'
    elif name == "WebFetch":
        title = f"Fetch {_domain(tool_input.get('url'))}"
    elif name in ("Task", "Agent"):
        title = f"Agent: {tool_input.get('description', '')}"
    else:
        title = name

    return _truncate_title(title)


def _basename(file_path: Any) -> str:
    if not file_path:
        return ""
    return str(file_path).rstrip("/").rsplit("/", 1)[-1]


def _domain(url: Any) -> str:
    if not url:
        return ""
    stripped = str(url).split("://", 1)[-1]
    return stripped.split("/", 1)[0]


def _truncate_title(title: str) -> str:
    if len(title) <= _TITLE_MAX_LEN:
        return title
    return title[: _TITLE_MAX_LEN - 1] + "…"


# ── Session resolution (I/O — reads transcript dirs, not pure) ──────────────
#
# The functions below are the only I/O-touching code in this module (the
# parser above stays pure). They locate an agent's live Claude Code session
# on disk and gate Boss/host transcripts against Mark's private ~/.claude
# sessions before anything from them reaches the frontend.


def encode_cwd(cwd: str) -> str:
    """Replicates Claude Code's own project-directory name encoding: every
    non-alphanumeric character (including path separators and dots) becomes
    a literal '-'. Verified against a real session dir name, see tests."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def resolve_transcript_dir(agent) -> Path | None:
    """Maps an Agent to the on-disk directory holding its Claude Code JSONL
    transcripts, or None if this agent/runtime has no transcript at all
    (Hermes, Jarvis, manual agents — anything not driven by the claude CLI).

    Duck-typed on ``agent.slug`` / ``agent.agent_runtime`` so tests can pass
    a plain stub instead of a DB-backed Agent row.
    """
    slug = getattr(agent, "slug", None)
    runtime = getattr(agent, "agent_runtime", None)
    if not slug:
        return None

    if runtime == "cli-bridge":
        return (
            _host_home()
            / ".mc"
            / "agents"
            / slug
            / "claude-config"
            / "projects"
            / encode_cwd("/home/agent")
        )

    if runtime == "host" and slug in _BOSS_SLUGS:
        checkout = str(_host_home() / ".mc" / "checkouts" / "mission-control")
        return _host_home() / ".claude" / "projects" / encode_cwd(checkout)

    return None


# Wie viele Zeilen einer Sitzungsdatei hoechstens gelesen werden, um sie als
# reine Kommando-Huelle zu erkennen. Echte Gespraeche zeigen ihren ersten
# Inhalt in den ersten paar Zeilen (live nachgesehen: Zeile 8); wer bis hier
# nichts gezeigt hat, gilt im Zweifel als echt — sichtbar bleiben ist die
# ungefaehrliche Richtung.
_PROBE_SCAN_MAX_LINES = 400
# Memo, damit dieselbe Datei nicht bei jedem Poll neu gelesen wird. Schluessel
# traegt mtime+size, ein Anwachsen der Datei verwirft den Eintrag also selbst.
_probe_scan_memo: dict[tuple[str, float, int], bool] = {}
_PROBE_SCAN_MEMO_MAX = 512


#: Befehle, die nur ein Mensch ausloest — nie die Katalog-Erkennung, die
#: ausschliesslich `/model` sondiert. Eine Sitzung mit einem dieser Befehle
#: ist eine echte Sitzung, auch wenn sonst nichts drinsteht.
_OPERATOR_COMMANDS = frozenset({"clear"})


def _is_operator_command(name: str | None) -> bool:
    return bool(name) and name.strip().lstrip("/").split()[0].lower() in _OPERATOR_COMMANDS


def is_command_only_session(path: Path) -> bool:
    """Hat diese Sitzungsdatei ueberhaupt ein Gespraech — oder nur
    Kommando-Huellen?

    ``True`` heisst: die Datei enthaelt mindestens eine
    ``<command-name>``/``<local-command-stdout>``-Zeile, aber KEINE
    Assistenten-Antwort und keine einzige echte Nutzer-Zeile. Genau die Form
    hat eine Sitzung, die nur eine Sonde geoeffnet hat (die
    ``/model``-Katalog-Erkennung tat das 41-mal ueber 8 Agenten hinweg) —
    und weil sie die NEUESTE Datei im Ordner war, zeigte der Chat sie statt
    des echten Gespraechs.

    Warum genau dieses Kriterium und kein einfacheres: eine FRISCHE, noch
    leere Sitzung sieht sonst identisch aus. Ihr fehlt aber die
    Kommando-Zeile — sie besteht nur aus Kopf-Zeilen (``mode``,
    ``attachment``, ``file-history-snapshot``, ``last-prompt``). Darum ist
    das Vorhandensein einer Kommando-Huelle Teil der Bedingung und nicht nur
    das Fehlen von Inhalt: eine frisch gestartete Sitzung bleibt sichtbar,
    eine reine Sonden-Sitzung nicht.

    Alles, was nicht sicher als Huelle erkennbar ist, gilt als echt
    (Lesefehler, kaputte Zeilen, sehr lange Dateien) — eine faelschlich
    versteckte Sitzung waere schlimmer als eine faelschlich gezeigte."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return False
    memo = _probe_scan_memo.get(key)
    if memo is not None:
        return memo

    command_seen = False
    content_seen = False
    truncated = False
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _PROBE_SCAN_MAX_LINES:
                    # Nicht zu Ende gelesen -> keine sichere Aussage. Eine
                    # echte Sonden-Sitzung ist winzig (rund 10 Zeilen); wer
                    # hier ankommt, ist keine.
                    truncated = True
                    break
                if content_seen:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("type")
                if kind == "assistant":
                    content_seen = True
                    break
                if kind != "user":
                    continue
                message = entry.get("message")
                text = (message or {}).get("content") if isinstance(message, dict) else None
                if not isinstance(text, str):
                    # Listen-Inhalt = Tool-Ergebnis o.ae. — echter Verlauf.
                    content_seen = True
                    break
                if _LOCAL_COMMAND_CAVEAT_RE.match(text):
                    continue  # Boilerplate, sagt weder Huelle noch Inhalt
                wrapper = _COMMAND_WRAPPER_RE.match(text)
                if wrapper and _is_operator_command(wrapper.group("name")):
                    # Ein Befehl des Operators ist keine Sonde: `/clear` wirft
                    # die Sitzung weg und erzeugt eine Datei, die genau wie
                    # eine Sonde aussieht — solange danach nichts geschrieben
                    # wird. Wird sie uebersprungen, meldet der Chat den
                    # Sitzungswechsel nie, und die Nachricht des Operators
                    # bleibt als "Nicht bestaetigt" stehen (Befund 31.08.2026).
                    content_seen = True
                    break
                if (
                    wrapper
                    or _LOCAL_COMMAND_STDOUT_RE.match(text)
                    or _LOCAL_COMMAND_STDERR_RE.match(text)
                ):
                    command_seen = True
                    continue
                content_seen = True
                break
    except OSError:
        return False

    result = command_seen and not content_seen and not truncated
    if len(_probe_scan_memo) >= _PROBE_SCAN_MEMO_MAX:
        _probe_scan_memo.clear()
    _probe_scan_memo[key] = result
    return result


#: Dateien im ``subagents``-Ordner, die KEIN delegierter Auftrag sind:
#: ``journal.jsonl`` ist das Protokoll eines Workflows, ``aside_question`` und
#: ``acompact`` sind CLI-interne Hilfsagenten. Der Operator hat sie nie
#: beauftragt und will sie nicht als Karte sehen.
_SUBAGENT_INTERNAL_PREFIXES = ("aside_question", "acompact")

#: Kopfzeilen, in denen der Startzeitpunkt gesucht wird. Bewusst klein: eine
#: gemessene Subagenten-Datei war 13,8 MB gross, der Zeitstempel steht in der
#: ersten Zeile.
_SUBAGENT_HEAD_LINES = 5


def subagent_runs(session_path: Path) -> list[dict[str, Any]]:
    """Die Subagenten-Laeufe DIESER Sitzung — je Lauf ein Steckbrief.

    Claude Code legt seit ~2.1.2xx neben dem Sitzungs-Transkript einen Ordner
    ``<sitzung>/subagents/`` an, darin je Subagent eine ``.jsonl`` mit seinem
    vollstaendigen Verlauf und eine gleichnamige ``.meta.json``. Im
    HAUPT-Transkript steht davon nichts mehr: live gemessen am 22.08.2026
    stehen dort 0 Zeilen mit ``isSidechain: true``, der Spawn erscheint nur als
    Werkzeugaufruf ``Agent``. Der Verlauf eines Subagenten ist aus dem
    Hauptstrom also NICHT rekonstruierbar — nur von hier.

    Rueckgabe je Lauf: ``runId`` (Datei-Stem ohne ``agent-``), ``name``,
    ``agentType``, ``description``, ``model``, ``color``, ``teamName``,
    ``startedAt``. Nach Startzeitpunkt sortiert.

    Von den Feldern ist keines garantiert: ueber 754 gemessene Steckbriefe war
    ``agentType`` immer da, ``name`` in 50 %, ``model`` in 57 %. Fehlt der
    Steckbrief ganz oder ist er kaputt, bleibt der Lauf trotzdem in der Liste —
    er hat einen Verlauf, den man zeigen kann, und ein fehlendes Feld ist
    ``None`` statt einer Erfindung.

    Wirft nie: ein Fehler beim Auflisten darf nicht den ganzen Verlauf
    mitreissen (gleiche Hausregel wie ``transcript_suggests_turn_ended``).
    """
    session_dir = session_path.parent / session_path.stem
    subdir = session_dir / "subagents"
    try:
        # Ein SYMLINK auf den Ordner (oder auf das Sitzungsverzeichnis)
        # verschoebe den ganzen Baum: ``glob`` folgt ihm, und die Laeufe eines
        # FREMDEN Agenten stuenden in dieser Liste. Ein Agent kann das
        # anlegen — sein Config-Verzeichnis ist schreibbar gemountet.
        # Echte Pfade der Flotte enthalten keine Symlink-Komponente
        # (nachgeprueft 22.08.2026), es geht hier also nichts Legitimes
        # verloren.
        if session_dir.is_symlink() or subdir.is_symlink():
            logger.warning(
                "transcript_chat: subagents dir is a symlink, refusing to follow it: %s",
                subdir,
            )
            return []
        if not subdir.is_dir():
            return []
        # Flach, nicht rekursiv: ``subagents/workflows/`` ist ein eigener
        # Baum (396 von 1245 gemessenen Dateien) mit praktisch leeren
        # Steckbriefen und gehoert zu einem anderen Thema.
        files = sorted(subdir.glob("agent-*.jsonl"))
    except OSError:
        logger.debug("transcript_chat: subagents dir unreadable for %s", session_path)
        return []

    runs: list[dict[str, Any]] = []
    for path in files:
        run_id = path.stem[len("agent-") :]
        if not run_id or run_id.startswith(_SUBAGENT_INTERNAL_PREFIXES):
            continue
        meta = _read_subagent_meta(path.with_suffix(".meta.json"))
        runs.append(
            {
                "runId": run_id,
                "name": meta.get("name"),
                "agentType": meta.get("agentType"),
                "description": meta.get("description"),
                "model": meta.get("model"),
                "color": meta.get("color"),
                "teamName": meta.get("teamName"),
                "startedAt": _subagent_started_at(path),
            }
        )

    # Startreihenfolge, nicht alphabetisch — sonst zeigte die Oberflaeche eine
    # Abfolge, die es nie gab. Laeufe ohne Zeitstempel ans Ende.
    runs.sort(key=lambda r: (r["startedAt"] is None, r["startedAt"] or ""))
    return runs


def _read_subagent_meta(path: Path) -> dict[str, Any]:
    """Der Steckbrief, oder ein leeres Dikt. Fail-silent mit Absicht: ein
    kaputter Steckbrief darf den Lauf nicht verschwinden lassen."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _subagent_started_at(path: Path) -> str | None:
    """Der Zeitstempel der ersten Zeile, die einen traegt. Nur die ersten
    Zeilen werden gelesen — siehe ``_SUBAGENT_HEAD_LINES``."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _ in range(_SUBAGENT_HEAD_LINES):
                line = f.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = entry.get("timestamp") if isinstance(entry, dict) else None
                if isinstance(ts, str) and ts:
                    return ts
    except OSError:
        return None
    return None


#: Die Hintergrund-Meldung der CLI. Sie traegt die Rolle ``user`` und stand
#: darum ungedeutet als NACHRICHT DES OPERATORS im Chat — eine Wand aus
#: Kennungen und Host-Pfaden, die niemand getippt hat.
#:
#: Bewusst streng verankert (``\A`` … ``\Z`` auf dem getrimmten Text): Eine
#: echte Nachricht, die ueber Hintergrund-Meldungen SPRICHT, darf nicht
#: verschluckt werden. Text des Operators verlieren ist der teurere Fehler.
_TASK_NOTIFICATION_RE = re.compile(
    r"\A<task-notification>(?P<body>.*?)</task-notification>\Z", re.S
)
_NOTIFICATION_FIELD_RE = re.compile(r"<(?P<tag>[a-z-]+)>(?P<value>.*?)</(?P=tag)>", re.S)

#: Was aus der Meldung ueberhaupt herausgereicht wird. ``output-file`` steht
#: mit Absicht NICHT hier: das ist ein Pfad auf der Maschine des Operators,
#: er hilft in der Oberflaeche niemandem, und dieses Repo ist oeffentlich.
_NOTIFICATION_KEEP = {"task-id": "taskId", "tool-use-id": "toolUseId",
                      "status": "status", "summary": "summary"}


def _parse_task_notification(
    text: str, msg_uuid: str, ts: str
) -> list[dict[str, Any]] | None:
    """``<task-notification>…</task-notification>`` -> ein ``notification``-
    Ereignis, oder ``None``, wenn der Text keine (vollstaendige) Meldung ist.

    Die Felder sind nicht garantiert: ueber 400 Transkripte gemessen traegt
    ``tool-use-id`` 66 von 77 Meldungen, ``status`` und ``summary``
    durchgaengig. Fehlendes wird ``None``, nicht erfunden.
    """
    match = _TASK_NOTIFICATION_RE.match(text.strip())
    if match is None:
        return None

    felder = {
        m.group("tag"): m.group("value").strip()
        for m in _NOTIFICATION_FIELD_RE.finditer(match.group("body"))
    }
    if not felder:
        return None

    ereignis: dict[str, Any] = {"kind": "notification", "uuid": msg_uuid, "ts": ts}
    for tag, name in _NOTIFICATION_KEEP.items():
        wert = felder.get(tag)
        ereignis[name] = wert or None
    return [ereignis]


#: Der EINZIGE Ordner, in dem je eine Wegwerf-Sitzung der Katalog-Erkennung
#: gelandet ist: der Projektordner von ``/home/agent`` im Container. Host-
#: Agenten liegen woanders (``~/.claude/projects/<pfad-des-checkouts>``), und
#: die Erkennung erreicht sie ohnehin nicht — sie faehrt ``docker exec``.
_DISCOVERY_LEGACY_PROJECT_DIR = "-home-agent"


# Eine einzelne Transkript-Zeile kann sehr gross sein (ein tool_result mit
# Datei-Inhalt oder Kommando-Ausgabe ist EINE Zeile — gemessen bis ~1,2 MB).
# Ein zu kleines Lese-Fenster landet mitten in so einer Zeile, findet keinen
# vollstaendigen Eintrag und faellt still auf den mtime zurueck — genau der
# Fehler, den diese Funktion beheben soll. Darum wird das Fenster verdoppelt,
# bis ein Eintrag gefunden ist oder die Datei ganz gelesen wurde.
_LAST_ENTRY_TAIL_STEPS = (65536, 524288, 4194304)
_LAST_ENTRY_CACHE: dict[str, tuple[int, float, float | None]] = {}


def last_entry_timestamp(path: Path) -> float | None:
    """Zeitstempel des LETZTEN Eintrags einer Transkript-Datei (Unix-Sekunden).

    Warum das noetig ist: Der Datei-mtime luegt. Eine alte Sitzungsdatei kann
    beruehrt werden (Metadaten-Schreiber, Editor, Backup, Rollover-Nachzuegler),
    ohne dass ein einziger Gespraechs-Eintrag dazukommt — sie sieht dann neuer
    aus als die Datei, in der tatsaechlich gerade gesprochen wird. Operator-
    Befund 31.08.2026 (Boss): MC zeigte einen 11 Tage alten Chat, weil dessen
    Datei einen frischeren mtime trug als die laufende Sitzung.

    Gelesen wird nur das Dateiende, rueckwaerts bis zur ersten Zeile mit
    brauchbarem ``timestamp``. Das Fenster waechst dabei (``_LAST_ENTRY_TAIL_STEPS``),
    weil eine EINZELNE Zeile sehr gross sein kann — ein ``tool_result`` mit
    Datei-Inhalt ist ein Eintrag, gemessen bis ~1,2 MB. Ergebnis wird pro Pfad
    gecacht (Groesse + mtime als Gueltigkeits-Marke) — bei unveraenderter Datei
    kostet der zweite Aufruf nichts. ``None``, wenn die Datei keinen lesbaren Zeitstempel hat
    (leer, kaputt, fremdes Format) — der Aufrufer faellt dann auf mtime zurueck.
    """
    try:
        st = path.stat()
    except OSError:
        return None

    cached = _LAST_ENTRY_CACHE.get(str(path))
    if cached is not None and cached[0] == st.st_size and cached[1] == st.st_mtime:
        return cached[2]

    ts: float | None = None
    for window in _LAST_ENTRY_TAIL_STEPS:
        try:
            with path.open("rb") as fh:
                if st.st_size > window:
                    fh.seek(-window, 2)
                    fh.readline()  # angeschnittene erste Zeile verwerfen
                chunk = fh.read()
        except OSError:
            break

        for raw in reversed(chunk.splitlines()):
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            value = entry.get("timestamp")
            if not isinstance(value, str) or not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            ts = parsed.timestamp()
            break

        if ts is not None or st.st_size <= window:
            break  # gefunden, oder die ganze Datei war schon im Fenster

    if ts is None:
        logger.debug(
            "last_entry_timestamp: kein Zeitstempel in %s (%d Bytes) — mtime gilt",
            path.name,
            st.st_size,
        )

    # Ein Eintrag pro Datei: der Live-Transkript-Pfad wuerde sonst bei jedem
    # Anhaengen einen neuen Schluessel erzeugen und den Cache aufblaehen.
    _LAST_ENTRY_CACHE[str(path)] = (st.st_size, st.st_mtime, ts)
    return ts


def find_active_session(tdir: Path) -> tuple[Path, dict[str, Any]] | None:
    """Finds the newest ``*.jsonl`` transcript directly under ``tdir`` (does
    NOT recurse into subdirectories — those hold sidechains/artifacts, not
    top-level sessions).

    Sitzungen ohne jeden Gespraechsinhalt werden UEBERSPRUNGEN
    (``is_command_only_session``) — aber NUR im Projektordner eines
    Container-Agenten (``-home-agent``). Grund: die ``/model``-Katalog-
    Erkennung hat monatelang Wegwerf-Sitzungen dort hinterlassen; als jeweils
    neueste Datei verdeckten sie das echte Gespraech (Operator-Befund
    19.08.2026: researcher zeigte 10 Zeilen / 0 Antworten statt seiner 218
    Zeilen / 65 Antworten). Die Erkennung legt dort inzwischen nichts mehr ab
    (sie schreibt seit 19.08. in den Projektordner von ``/workspace``); diese
    Schicht heilt zusaetzlich die ~41 Dateien, die bereits auf der Platte
    liegen — geloescht wird nichts. Bleibt danach nichts uebrig, gewinnt doch
    die neueste Datei: lieber eine magere Sitzung zeigen als gar keine.

    Warum die Einschraenkung auf diesen einen Ordner: Eine frische Sitzung,
    deren erster Zug ein Slash-Befehl ist, sieht GENAUSO aus wie eine Sonde —
    sie hat eine Kommando-Huelle und noch keine Antwort. Bei einem Host-Agenten
    wurde deshalb der Modellwechsel im frischen Chat verschluckt: der Chat
    sprang auf die vorige Sitzung zurueck, das Echo blieb als "Nicht
    bestaetigt" stehen und der Modell-Chip las das ALTE Modell (Operator-
    Befund 22.08.2026, Boss). Die Erkennung laeuft ausschliesslich per
    ``docker exec`` gegen cli-bridge-Agenten (``harness_catalog._tmux``) — in
    den Ordner eines Host-Agenten hat sie nie geschrieben und kann es nicht.
    Dort ist eine reine Kommando-Sitzung also immer die des Operators.

    Returns ``(path, meta)`` where ``meta`` is
    ``{"sessionId": <filename stem>, "mtime": <iso8601>, "live": <bool>}``
    — ``live`` is True when the file was written within the last
    ``_LIVE_WINDOW_SECONDS``. Returns None if the directory doesn't exist or
    has no top-level jsonl files.
    """
    if not tdir.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for candidate in tdir.glob("*.jsonl"):
        try:
            candidates.append((candidate.stat().st_mtime, candidate))
        except OSError:
            continue
    if not candidates:
        return None

    # Sortiert wird nach dem Zeitstempel des LETZTEN EINTRAGS, nicht nach mtime
    # (siehe last_entry_timestamp: der mtime luegt, wenn eine alte Datei nur
    # beruehrt wurde). Ohne lesbaren Eintrag faellt eine Datei auf ihren mtime
    # zurueck, damit fremde/kaputte Formate nicht unsichtbar werden.
    ranked = []
    for mtime, path in candidates:
        entry_ts = last_entry_timestamp(path)
        # Explizit: nur "nicht lesbar" faellt auf mtime zurueck. Ein echter
        # Zeitstempel 0.0 (1970) bliebe mit `or` faelschlich der mtime.
        ranked.append((mtime if entry_ts is None else entry_ts, mtime, path))
    # Gleichstand beim Inhalts-Zeitstempel (identische Fixtures, Sitzungs-
    # Rollover, Sekundengenauigkeit) entscheidet der mtime — sonst gewinnt
    # zufaellig der alphabetisch groessere Dateiname und ein frischer
    # Rollover bliebe unsichtbar.
    ranked.sort(key=lambda row: (row[0], row[1], str(row[2])), reverse=True)
    _, newest_mtime, newest_path = ranked[0]
    if tdir.name == _DISCOVERY_LEGACY_PROJECT_DIR:
        for _, mtime, candidate in ranked:
            # Der Reihe nach von neu nach alt — der erste Treffer mit Inhalt
            # gewinnt, und im Normalfall ist das gleich der erste geprueft.
            if not is_command_only_session(candidate):
                newest_mtime, newest_path = mtime, candidate
                break

    meta = {
        "sessionId": newest_path.stem,
        "mtime": datetime.fromtimestamp(newest_mtime, tz=timezone.utc).isoformat(),
        "live": (time.time() - newest_mtime) < _LIVE_WINDOW_SECONDS,
    }
    return newest_path, meta


async def resolve_aliveness(agent, session_path: Path, adapter: Any | None = None) -> str:
    """Classifies a session's liveness for the history/tailer meta —
    ``"active" | "idle" | "ended"``. Fixes the old live-only semantics
    (mtime<60s == the ONLY signal, so an idle-but-still-running CLI read as
    "beendet"/ended everywhere — an operator-visible bug, since a session
    with nothing new to say for a few minutes is completely normal, not
    dead). ``live`` stays on the history/find_active_session meta unchanged
    for backward compat (== ``aliveness == "active"``); this is the new,
    richer signal alongside it.

    Priority order:
    1. Written within ``_LIVE_WINDOW_SECONDS`` -> ``"active"``.
    2. A NEWER session file now exists in the same directory (this one was
       rolled over / superseded) -> ``"ended"``. Checked via
       ``find_active_session`` again — cheap (one directory glob).
    3. ``pane_state.process_alive`` (docker cli-bridge, cached ~30s): the
       CLI process is confirmed running -> ``"idle"``; confirmed gone ->
       ``"ended"``.
    4. Otherwise (Boss/host — no process channel at all — or the docker
       check itself came back unknown): a transcript-age fallback. Within
       ``_ALIVENESS_IDLE_MAX_AGE_SECONDS`` -> ``"idle"``; older -> ``"ended"``.

    ``adapter`` (omp-Runde): liefert Session-Suche und Prozessnamen des
    Harness. ``None`` = Claude Code. Ohne ihn suchte Schritt 3 immer nach
    einem ``claude``-Prozess — bei omp fand ``pgrep`` nichts und die
    laufende Sitzung galt faelschlich als beendet.
    """
    if adapter is None:
        from app.services.transcript_adapters import adapter_for

        adapter = adapter_for(agent)

    try:
        mtime = session_path.stat().st_mtime
    except OSError:
        mtime = None

    if mtime is not None and (time.time() - mtime) < _LIVE_WINDOW_SECONDS:
        return "active"

    # Rollover-Pruefung ueber der Transkript-WURZEL des Harness — bei omp
    # liegt die Session eine Ebene tiefer als die Wurzel.
    try:
        active = await asyncio.to_thread(
            adapter.find_active_session, adapter.session_scan_root(session_path)
        )
    except OSError:
        active = None
    if active is not None and active[0] != session_path:
        return "ended"

    alive = await process_alive(agent, adapter.process_name)
    if alive is True:
        return "idle"
    if alive is False:
        return "ended"

    if mtime is not None and (time.time() - mtime) < _ALIVENESS_IDLE_MAX_AGE_SECONDS:
        return "idle"
    return "ended"


def _extract_cwd_and_branch(path: Path) -> tuple[str, str | None]:
    """Scans the first ``_BOSS_SCAN_LINES`` lines of a transcript for the
    first line carrying a top-level ``cwd`` — Claude Code stamps ``cwd`` /
    ``gitBranch`` on every line, so line 1 normally suffices; the 20-line
    cap is a safety margin against odd/legacy transcripts, not an expected
    scan depth. Returns ("", None) if nothing usable was found."""
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i >= _BOSS_SCAN_LINES:
                break
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict) or "cwd" not in d:
                continue
            return d.get("cwd") or "", d.get("gitBranch")
    return "", None


def transcript_allowed(agent, path: Path) -> bool:
    """Privacy gate: cli-bridge agent transcripts are always MC's own
    workspace (Docker containers have nothing else to write) — always
    allowed. Host-runtime Boss transcripts live in Mark's own ~/.claude,
    shared with his private/personal sessions — only lines that look like MC
    work (mission-control cwd or a task/ branch) are allowed through, via
    the same heuristic token_harvester uses for cost attribution. Any read
    failure (missing/unreadable file) fails closed.

    Explicitly gated on ``slug in _BOSS_SLUGS`` (not just "any non-cli-bridge
    agent") — resolve_transcript_dir() already returns None for every other
    host agent (Hermes, Jarvis) so this branch should never be reached for
    them in practice, but a caller passing one directly with an arbitrary
    path must still fail closed rather than accidentally running the Boss
    heuristic against it (review finding, fix-round 1)."""
    runtime = getattr(agent, "agent_runtime", None)
    slug = getattr(agent, "slug", None)

    if runtime == "cli-bridge":
        return True
    if not (runtime == "host" and slug in _BOSS_SLUGS):
        return False

    try:
        cwd, git_branch = _extract_cwd_and_branch(path)
    except OSError:
        return False

    return _should_attribute_boss_path(cwd, git_branch)


# ── Statusline state (I/O — reads the CLI's own context-window truth) ───────
#
# Claude Code invokes settings.json's `statusLine` command on every prompt,
# piping it a JSON blob with the CLI's own live token accounting
# (context_window.used_percentage, context_window.current_usage.*).
# docker/shared/statusline-mc.sh (wired in via plugin_manager.render_agent_settings,
# claude-harness agents only) mirrors that blob to
# <claude-config>/statusline-state/<session_id>.json. Reading it back here
# gives the chat context meter ground truth instead of a guess from the
# static settings.context_windows model->size map (resolve_context_window
# above) — the estimate stays as the fallback for models/agents where no
# fresh statusline write exists yet (Boss, whose ~/.claude isn't managed by
# this codebase; agents that haven't sent a prompt since the feature shipped).

_STATUSLINE_FRESH_SECONDS = 120


def _claude_config_root(session_path: Path) -> Path:
    """Given a session's transcript file path, returns the claude-config (or
    Boss's ~/.claude) root two levels above its parent directory — the
    inverse of resolve_transcript_dir's own shape:
    ``<root>/projects/<encoded-cwd>/<session>.jsonl``, so
    ``session_path.parent`` is ``projects/<encoded-cwd>`` and
    ``session_path.parent.parent`` is ``<root>``... two levels up from the
    session's *parent* dir, i.e. three from the file itself."""
    return session_path.parent.parent.parent


def read_statusline_state(claude_config_root: Path, session_id: str) -> dict[str, Any] | None:
    """Reads ``<claude_config_root>/statusline-state/<session_id>.json`` —
    the file docker/shared/statusline-mc.sh writes on every Claude Code
    prompt for this session. Returns ``{"usedPct": float, "usedTokens": int,
    "contextWindowSize": int}`` (``usedTokens`` = the sum of the four
    ``current_usage`` fields Claude Code reports; ``contextWindowSize`` =
    ``context_window.context_window_size``, the CLI's own live context-window
    size — present even before the session's first turn, so it's ground
    truth rather than the ``settings.context_windows`` model->size guess)
    when the file exists, was written less than ``_STATUSLINE_FRESH_SECONDS``
    ago (older means no CLI turn has run recently enough to trust it — the
    agent may have switched sessions or the script may be broken), and
    parses as the expected shape. ``None`` on any failure — missing file
    (most agents, always for Boss), stale mtime, or malformed JSON. Never
    raises; the caller falls back to the static ``resolve_context_window``
    estimate."""
    state_file = claude_config_root / "statusline-state" / f"{session_id}.json"
    try:
        mtime = state_file.stat().st_mtime
    except OSError:
        return None
    if (time.time() - mtime) >= _STATUSLINE_FRESH_SECONDS:
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        ctx = data["context_window"]
        usage = ctx["current_usage"]
        used_pct = float(ctx["used_percentage"])
        context_window_size = int(ctx["context_window_size"])
        components = {
            "input": int(usage.get("input_tokens") or 0),
            "cacheRead": int(usage.get("cache_read_input_tokens") or 0),
            "cacheCreation": int(usage.get("cache_creation_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        }
        used_tokens = sum(components.values())
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "usedPct": used_pct,
        "usedTokens": used_tokens,
        "contextWindowSize": context_window_size,
        "components": components,
    }


def _stamp_usage_source(ev: dict[str, Any], claude_config_root: Path, session_id: str) -> None:
    """Mutates a ``usage`` event in place with ``usedPct``/``source``,
    preferring fresh statusline state (source ``"cli"``) over the static
    ``contextWindow`` estimate parse_transcript_line already stamped
    (source ``"estimate"``, ``usedPct`` left ``None`` for the frontend to
    compute from ``contextWindow`` itself). When statusline state is fresh,
    its own ``context_window_size`` also OVERRIDES the ``contextWindow``
    estimate — the CLI reports its actual context window directly, no need
    to guess from the model name."""
    state = read_statusline_state(claude_config_root, session_id)
    if state is not None:
        ev["usedPct"] = state["usedPct"]
        ev["source"] = "cli"
        ev["contextWindow"] = state["contextWindowSize"]
        # The CLI's own per-field usage describes the WHOLE live context, while
        # the transcript line only describes that one turn — so when it is
        # available it wins, the same way usedPct/contextWindow do. Otherwise
        # the turn-level breakdown parse_transcript_line stamped stays.
        ev["components"] = state["components"]
    else:
        ev["usedPct"] = None
        ev["source"] = "estimate"


# ── History reading (I/O — reads a transcript file, not pure) ───────────────

_RESULT_TRUNCATE_LEN = 4000
_STATS_TOOLS = ("Edit", "Write")


def read_history(
    path: Path,
    adapter: Any,
    limit: int = 200,
    before_uuid: str | None = None,
    observed_windows: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Reads a transcript file top-to-bottom and returns one page of chat
    events plus session metadata.

    ``adapter`` (omp round) is the harness-specific half —
    ``transcript_adapters.adapter_for(agent)`` — and it is REQUIRED. It used
    to default to ``None`` -> ``adapter_for(None)``, i.e. the Claude Code
    parser for whatever wrote ``path``. On a real omp transcript that
    returned ZERO events with ``startedAt`` set: a session that looks
    healthy and empty. That silent wrong answer is exactly what the adapter
    round removes, so it must not survive as a default — the more so since
    ``resolve_aliveness`` falls back the other way (``adapter_for(agent)``),
    and two contradicting fallbacks are worse than none.

    ``observed_windows`` (harness-catalog round): a pre-fetched
    model->context-window map (``harness_catalog.get_observed_model_windows()``)
    threaded into ``parse_transcript_line`` for each ``usage`` event's
    estimate. Fetched by the CALLER (the router, which has async Redis
    access), not by this function — ``read_history`` itself stays a plain
    synchronous function with no Redis dependency of its own; ``None``
    (the default) just skips that resolution tier.

    Streams the file line-by-line (transcripts can grow large over a long
    session). Dedups on the top-level entry ``uuid`` — Claude Code can repeat
    a line verbatim across a resumed session, and re-parsing it would
    duplicate every event derived from it. Internal ``_tool_result`` events
    are merged onto the ``tool`` event with the matching ``toolUseId`` (never
    appended to ``events`` on their own) — matching by id rather than
    position is what lets a multi-tool assistant turn resolve correctly, see
    the module docstring. ``sidechain`` events are left inline; the frontend
    groups them.

    Without ``before_uuid``, returns the newest ``limit`` events (initial
    load). With ``before_uuid``, returns the ``limit`` events immediately
    preceding the first occurrence of that uuid (backward paging) — all
    events sharing one entry's uuid are contiguous, so "first occurrence" is
    that entry's start and excludes the whole entry from the page, never
    just part of it. An unknown ``before_uuid`` yields an empty page.
    """
    # Ohne Pfad: diese Funktion liest die Datei ohnehin von Zeile 1 an, ein
    # zustandsbehafteter Parser sammelt seinen Zustand also unterwegs selbst.
    # (Der Live-Tailer steigt am Dateiende ein und braucht deshalb die
    # Vorgabe — siehe dort.)
    parse_line = adapter.new_parser()

    session_id = path.stem
    try:
        live = (time.time() - path.stat().st_mtime) < _LIVE_WINDOW_SECONDS
    except OSError:
        live = False

    started_at: str | None = None
    events: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    tool_events_by_id: dict[str, dict[str, Any]] = {}
    command_events_by_uuid: dict[str, dict[str, Any]] = {}

    try:
        lines_file = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        lines_file = None

    if lines_file is not None:
        with lines_file:
            for raw_line in lines_file:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    d = json.loads(raw_line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(d, dict):
                    continue

                entry_uuid = adapter.peek_entry_id(raw_line)
                if entry_uuid is not None:
                    if entry_uuid in seen_uuids:
                        continue
                    seen_uuids.add(entry_uuid)

                if started_at is None and d.get("timestamp"):
                    started_at = d["timestamp"]

                for ev in parse_line(raw_line, observed_windows):
                    if ev["kind"] == "_tool_result":
                        tool_ev = tool_events_by_id.get(ev.get("tool_use_id"))
                        if tool_ev is not None:
                            _merge_tool_result(tool_ev, ev)
                        continue

                    if ev["kind"] == "_command_result":
                        cmd_ev = command_events_by_uuid.get(ev.get("parent_uuid"))
                        if cmd_ev is not None:
                            _merge_command_result(cmd_ev, ev)
                        continue

                    if ev["kind"] == "tool":
                        stats = _compute_edit_stats(ev["name"], ev["detail"])
                        if stats is not None:
                            ev["stats"] = stats
                        tool_use_id = ev.get("toolUseId")
                        if tool_use_id is not None:
                            tool_events_by_id[tool_use_id] = ev
                    elif ev["kind"] == "command":
                        command_events_by_uuid[ev["uuid"]] = ev
                    elif ev["kind"] == "usage":
                        adapter.stamp_usage(ev, path)

                    events.append(ev)

    total = len(events)
    if before_uuid is not None:
        cut = next((i for i, e in enumerate(events) if e.get("uuid") == before_uuid), None)
        if cut is None:
            page: list[dict[str, Any]] = []
            has_more = False
        else:
            start = max(0, cut - limit)
            page = events[start:cut]
            has_more = start > 0
    else:
        start = max(0, total - limit)
        page = events[start:]
        has_more = start > 0

    return {
        "events": page,
        "session": {"sessionId": session_id, "live": live, "startedAt": started_at},
        "hasMore": has_more,
        # Eigener Schluessel auf oberster Ebene, NICHT in ``session`` hinein:
        # ``session`` ist im Frontend als sessionId/live/startedAt/aliveness
        # belegt. Und bewusst nur hier, nicht im Live-Tailer: der steigt am
        # Dateiende ein und darf nie einen Ordner scannen. Die Lauf-Liste ist
        # ein Handshake-Wert — ein Subagent, der NACH dem Laden startet,
        # erscheint erst beim naechsten Abruf.
        "subagentRuns": adapter.subagent_runs(path),
    }


def _merge_tool_result(tool_event: dict[str, Any], tool_result: dict[str, Any]) -> None:
    """Merges an internal ``_tool_result`` event onto its matching ``tool``
    event (already looked up by ``toolUseId`` before this is called)."""
    content = tool_result.get("content")
    tool_event["result"] = str(content)[:_RESULT_TRUNCATE_LEN]
    if tool_result.get("is_error"):
        tool_event["status"] = "error"


def _merge_command_result(command_event: dict[str, Any], command_result: dict[str, Any]) -> None:
    """Merges an internal ``_command_result`` event (from a
    ``<local-command-stdout>``/``<local-command-stderr>`` wrapper) onto its
    matching ``command`` event (already looked up by ``parentUuid`` before
    this is called)."""
    command_event["result"] = command_result.get("content")


def _compute_edit_stats(name: str, detail: dict[str, Any]) -> dict[str, int] | None:
    """Edit tool_use inputs carry ``old_string``/``new_string`` — a naive
    line count of each, not a real diff, but enough for a chat summary
    badge. None when neither field is present (e.g. Write's ``content``, or
    any non-Edit/Write tool)."""
    if name not in _STATS_TOOLS:
        return None
    old_string = detail.get("old_string")
    new_string = detail.get("new_string")
    if old_string is None and new_string is None:
        return None
    return {
        "additions": _count_lines(new_string),
        "deletions": _count_lines(old_string),
    }


def _count_lines(value: Any) -> int:
    if not value:
        return 0
    return str(value).count("\n") + 1


# ── Live tailing (I/O — background polling task, not pure) ──────────────────


class ChatTailerManager:
    """Refcounted, per-agent background poller that follows a live Claude
    Code transcript and republishes each new line as a ``chat_event`` SSE
    frame on ``RedisKeys.agent_chat_channel(agent_id)``.

    One asyncio task per agent, shared across every connected SSE client for
    that agent (``acquire``/``release`` refcount it) — N browser tabs on the
    same agent's chat never spawn N pollers. The task is cancelled the moment
    the last client disconnects, so an agent nobody is watching costs nothing.

    Applies the same merge/skip semantics as ``read_history``: an internal
    ``_tool_result`` event is never published on its own — instead, when it
    arrives, the already-published ``tool`` event (matched by ``toolUseId``)
    is mutated in place and *republished* under the same ``uuid``/
    ``toolUseId``, so the frontend reducer replaces its existing tool card
    instead of appending a second one.
    """

    # 0,3 s statt 1,0 s: Der Tailer stat()et nur die Dateigroesse und liest
    # ausschliesslich, wenn sie gewachsen ist — der Takt kostet also fast
    # nichts, spart aber bis zu einer vollen Sekunde pro Antwort. Gemessen
    # 01.09.2026: die CLI schreibt einen fertigen Assistenten-Block auf einen
    # Schlag; bis zu diesem Zeitpunkt sieht die Oberflaeche gar nichts, danach
    # entschied allein dieser Takt, wie lange sie weiter nichts sieht.
    POLL_INTERVAL = 0.3

    # Pane-Sonde: rund alle 2 Sekunden, gemessen in SEKUNDEN statt in
    # Poll-Durchlaeufen. In Ticks gezaehlt wanderte die erste Zustandsmeldung
    # mit jedem geaenderten Poll-Takt mit — wer den Chat oeffnete, sah den
    # Status erst nach N × Takt. Die Sonde ist ein docker-exec-Rundlauf
    # (gemessen 50 ms): die erste laeuft sofort, danach genuegt der Zeittakt.
    STATE_PROBE_INTERVAL_SECONDS = 2.0

    # A transcript that hasn't grown in this long reads as idle for the
    # Boss/host fallback (no pane to capture) and as the transcript_active
    # signal parse_pane_state uses to disambiguate an input-prompt marker
    # (still typing vs. waiting).
    STATE_ACTIVE_WINDOW_SECONDS = 20

    def __init__(self) -> None:
        self._refcounts: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        #: Agent-Zeile je laufendem Tailer — ``release`` braucht sie, um den
        #: Terminal-Strom wieder abzuschalten.
        self._agents: dict[str, Any] = {}

    async def acquire(self, agent_id: str, path: Path, agent: Any | None = None) -> None:
        """Registers one more client for ``agent_id``. Starts the poll task
        if this is the first client; otherwise just bumps the refcount — the
        already-running task keeps tailing from wherever it is.

        ``agent`` (optional) is threaded through to the pane-state probe —
        it's the only place in the tailer that needs the Agent row itself
        (for ``capture_pane``'s runtime/slug lookup) rather than just a path.
        None disables the probe entirely (no state events published), which
        is also how the raw ``ChatTailerManager`` unit tests exercise the
        tailer without a DB-backed Agent.

        The starting offset is stat()'d HERE, synchronously, rather than as
        the first line of ``_run`` — a freshly created task doesn't actually
        start executing until the event loop regains control (the caller's
        next ``await``), so if the size were read inside the task body, a
        caller that writes to the file right after ``acquire()`` returns
        (with no intervening await) would race it: the task would only get
        scheduled after that write and seed from the wrong (post-write)
        size. Reading it here, before the task is even created, closes that
        window."""
        count = self._refcounts.get(agent_id, 0)
        self._refcounts[agent_id] = count + 1
        if count == 0:
            try:
                initial_offset = path.stat().st_size
            except OSError:
                initial_offset = 0
            # Fuer das Abschalten des Terminal-Stroms beim letzten Client:
            # ``release`` bekommt nur die agent_id, nicht die Agent-Zeile.
            if agent is not None:
                self._agents[agent_id] = agent
            self._tasks[agent_id] = asyncio.create_task(
                self._run(agent_id, path, initial_offset, agent)
            )

    async def release(self, agent_id: str) -> None:
        """Drops one client for ``agent_id``. Cancels and awaits the poll
        task once the last client releases."""
        count = self._refcounts.get(agent_id, 0)
        if count <= 1:
            self._refcounts.pop(agent_id, None)
            task = self._tasks.pop(agent_id, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            # Der Terminal-Strom laeuft nur, solange jemand zusieht. Ohne das
            # Abschalten schriebe tmux weiter in eine Datei, die niemand liest.
            agent = self._agents.pop(agent_id, None)
            if agent is not None:
                from app.services import pane_stream

                with contextlib.suppress(Exception):
                    await pane_stream.stop(agent)
        else:
            self._refcounts[agent_id] = count - 1

    async def _run(
        self,
        agent_id: str,
        initial_path: Path,
        initial_offset: int,
        agent: Any | None = None,
    ) -> None:
        from app.services.transcript_adapters import adapter_for

        # Der Harness des Agenten entscheidet, WIE gelesen wird (Parser,
        # Dedup-Feld, Session-Suche, Pane-Regeln). ``agent`` ist nur in
        # Tests None — dann gilt der Claude-Adapter wie bisher.
        adapter = adapter_for(agent)
        # ``new_parser`` darf die Sitzungsdatei LESEN, um seinen Anfangs-
        # zustand zu laden (omp: ``seed_from`` liest die ganze Datei, damit
        # der am Dateiende einsteigende Tailer die Effort-Stufe vom
        # Session-ANFANG kennt). Das ist ein Platten-Lesevorgang wie jeder
        # andere in dieser Schleife und gehoert deshalb in einen Thread —
        # sonst steht die gesamte FastAPI-Schleife, waehrend ein langes
        # Transkript eingelesen wird (einmal je Agent beim ersten
        # SSE-Verbinden).
        parse_line = await asyncio.to_thread(adapter.new_parser, initial_path)
        channel = RedisKeys.agent_chat_channel(agent_id)
        # omp legt seine Sessions eine Ebene tief in pro-cwd-Ordnern ab, der
        # Rollover-Scan muss also ueber der WURZEL laufen, nicht ueber dem
        # Ordner der aktuellen Datei.
        tdir = adapter.session_scan_root(initial_path)
        current_path = initial_path
        tick = 0
        last_pane_state: dict[str, Any] | None = None
        # Seeded from the file's size AT ACQUIRE TIME — NOT 0 — so a first
        # connect (or a refcount 0->1 cycle) tails only new lines instead of
        # re-reading and re-broadcasting the entire existing transcript as
        # live events (which would duplicate everything /chat/history
        # already returned). See acquire()'s docstring for why this is
        # computed there and passed in rather than stat'd here.
        offset = initial_offset
        buffer = b""
        tool_events_by_id: dict[str, dict[str, Any]] = {}
        command_events_by_uuid: dict[str, dict[str, Any]] = {}
        # Fetched once per task lifetime (not per tick — the observed map
        # changes rarely; a long-lived tailer task tolerating some staleness
        # here matches every other TTL-cache in this adapter). A fresh
        # observation THIS session writes below is visible to THIS task
        # immediately regardless (kept in sync locally, see the usage-event
        # branch), so staleness only affects observations from OTHER
        # sessions/agents that happened after this task started.
        # get_observed_model_windows() is itself fail-silent (-> {}).
        observed_windows = await get_observed_model_windows()
        # Cleared on session rollover (below) — bounded by one session's
        # lifetime, not by an explicit cap.
        seen_uuids: set[str] = set()
        # Dedup guard for the rejected-rollover warning below — without it a
        # disallowed newest-mtime file that keeps existing (e.g. Mark's own
        # personal session sitting in Boss's transcript dir) would log once
        # per poll tick forever.
        rejected_rollover_path: Path | None = None

        last_probe_at = 0.0  # 0.0 = noch nie -> die erste Sonde laeuft sofort

        # ── Live-Vorschau aus dem Terminal-Strom ─────────────────────────
        # Das Transkript bekommt einen Assistenten-Block erst, wenn er fertig
        # ist (gemessen: sieben Sekunden Stille in der Datei, waehrend im
        # Terminal der Text laeuft). Der Pane-Strom hat ihn sofort. Er ist die
        # Kuer: schlaegt hier etwas fehl, faellt nur die Vorschau aus.
        preview_state = await self._start_preview(agent)
        try:
            while True:
                await asyncio.sleep(self.POLL_INTERVAL)
                tick += 1

                try:
                    if preview_state is not None:
                        await self._pump_preview(channel, preview_state)

                    now = time.monotonic()
                    probe_due = (now - last_probe_at) >= self.STATE_PROBE_INTERVAL_SECONDS
                    if agent is not None and probe_due:
                        last_probe_at = now
                        new_state = await self._compute_pane_state(
                            agent, current_path, adapter
                        )
                        if new_state != last_pane_state:
                            last_pane_state = new_state
                            await sse.broadcast(
                                channel, "chat_event", {"kind": "state", **new_state}
                            )

                    try:
                        active = await asyncio.to_thread(adapter.find_active_session, tdir)
                    except OSError:
                        active = None
                    if active is not None and active[0] != current_path:
                        # Re-run the same Boss privacy gate the SSE handshake
                        # enforces at connect time (agent_chat.py:80) — a
                        # rollover mid-stream is a second, later "which file
                        # may this agent publish" decision and must not
                        # bypass it (review finding I-1). ``agent`` is only
                        # ``None`` in tests that don't exercise the gate; the
                        # real caller (acquire(), agent_chat.py:121) always
                        # passes it. ``transcript_allowed`` does blocking file
                        # I/O (opens the file, scans up to 20 lines) and this
                        # branch re-runs it every tick for as long as the
                        # rejected file stays newest — to_thread it, same
                        # rule as every other disk read in this loop.
                        allowed = True
                        if agent is not None:
                            allowed = await asyncio.to_thread(
                                adapter.transcript_allowed, agent, active[0]
                            )
                        if not allowed:
                            if active[0] != rejected_rollover_path:
                                rejected_rollover_path = active[0]
                                logger.warning(
                                    "chat tailer: rollover to %s rejected by "
                                    "transcript_allowed (agent_id=%s) — keeping "
                                    "current session %s",
                                    active[0], agent_id, current_path,
                                )
                            # Do NOT switch — keep tailing current_path below.
                        else:
                            rejected_rollover_path = None
                            current_path = active[0]
                            offset = 0
                            buffer = b""
                            tool_events_by_id = {}
                            command_events_by_uuid = {}
                            seen_uuids = set()
                            last_pane_state = None
                            # Frischer Parser: sein Zustand (bei omp die
                            # Effort-Stufe) gehoert zur ALTEN Session. Die
                            # NEUE Datei wird ab Offset 0 gelesen, also
                            # braucht er auch keine Vorgabe daraus.
                            parse_line = adapter.new_parser()
                            # aliveness is hardcoded "active" here rather than
                            # a fresh resolve_aliveness() call: a rollover
                            # only fires for a file find_active_session JUST
                            # confirmed is the newest, freshly written — by
                            # construction its mtime is within the live
                            # window, so an extra async round trip would only
                            # re-derive what's already known for free.
                            await sse.broadcast(
                                channel, "chat_event",
                                {"kind": "session_changed", "aliveness": "active"},
                            )
                            continue

                    try:
                        new_offset, chunk = await asyncio.to_thread(
                            _read_new_chunk, current_path, offset
                        )
                    except OSError:
                        # File disappeared (rotated/deleted mid-session) —
                        # state is unknown, but the directory keeps getting
                        # polled so a replacement (or the same path
                        # reappearing) is picked up.
                        continue

                    if chunk is None:
                        continue

                    offset = new_offset
                    buffer += chunk
                    lines = buffer.split(b"\n")
                    buffer = lines.pop()  # last element: partial line (or b"") — held for next tick

                    for raw_bytes in lines:
                        raw_line = raw_bytes.decode("utf-8", errors="replace").strip()
                        if not raw_line:
                            continue

                        entry_uuid = adapter.peek_entry_id(raw_line)
                        if entry_uuid is not None:
                            if entry_uuid in seen_uuids:
                                continue
                            seen_uuids.add(entry_uuid)

                        for ev in parse_line(raw_line, observed_windows):
                            if ev["kind"] == "_tool_result":
                                tool_ev = tool_events_by_id.pop(ev.get("tool_use_id"), None)
                                if tool_ev is not None:
                                    _merge_tool_result(tool_ev, ev)
                                    await sse.broadcast(channel, "chat_event", tool_ev)
                                continue

                            if ev["kind"] == "_command_result":
                                cmd_ev = command_events_by_uuid.pop(ev.get("parent_uuid"), None)
                                if cmd_ev is not None:
                                    _merge_command_result(cmd_ev, ev)
                                    await sse.broadcast(channel, "chat_event", cmd_ev)
                                continue

                            if ev["kind"] == "tool":
                                stats = _compute_edit_stats(ev["name"], ev["detail"])
                                if stats is not None:
                                    ev["stats"] = stats
                                tool_use_id = ev.get("toolUseId")
                                if tool_use_id is not None:
                                    tool_events_by_id[tool_use_id] = ev
                            elif ev["kind"] == "command":
                                command_events_by_uuid[ev["uuid"]] = ev
                            elif ev["kind"] == "usage":
                                # Sync file I/O (stat + read of the statusline
                                # state file) -> to_thread, same rule as
                                # _read_new_chunk above: the event loop never
                                # blocks on disk. current_path is re-derived
                                # per event (not hoisted) since a rollover mid-
                                # tick swaps it.
                                await asyncio.to_thread(
                                    adapter.stamp_usage, ev, current_path
                                )
                                # A fresh statusline read (source=="cli") is
                                # ground truth for THIS model's window right
                                # now — feed it into the shared observed map
                                # (harness_catalog round) so every other
                                # agent's estimate benefits too, and update
                                # this task's own local copy immediately
                                # rather than waiting for the next re-fetch
                                # (there isn't one — see the fetch-once
                                # comment above).
                                if (
                                    ev.get("source") == "cli"
                                    and ev.get("model")
                                    and ev.get("contextWindow")
                                ):
                                    observed_windows[ev["model"]] = ev["contextWindow"]
                                    await observe_model_window(ev["model"], ev["contextWindow"])

                            await sse.broadcast(channel, "chat_event", ev)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A single bad iteration (e.g. a transient Redis error in
                    # sse.broadcast) must not silently kill the task while
                    # clients stay connected — log and keep tailing.
                    logger.error(
                        "chat tailer: poll iteration failed for agent %s", agent_id, exc_info=True
                    )
        finally:
            # A silently dead tailer looks identical to "no new events yet"
            # from the outside — make every exit visible.
            logger.warning("chat tailer loop exited (agent_id=%s)", agent_id)

    @staticmethod
    def _transcript_suggests_turn_ended(path: Path) -> bool:
        """True, wenn die letzte inhaltliche Transkript-Zeile die
        ABSCHLUSS-Antwort des Agenten ist — dann ist der Zug vorbei, egal wie
        frisch die Datei ist.

        Operator-Befund (18.08.2026 nachts): nach Turn-Ende drehte die
        Statuszeile noch bis zu ~20s die Arbeits-Verben weiter, weil die
        working/idle-Disambiguierung allein am Datei-Alter hing
        (STATE_ACTIVE_WINDOW_SECONDS) — die frische mtime stammte aber genau
        von der ABSCHLUSS-Antwort.

        Die erste Fassung las "assistant-Zeile OHNE tool_use = Zug beendet".
        Das war an einer erfundenen Datei-Gestalt gemessen. Echte Claude-Code-
        Transkripte schreiben JEDEN Content-Block als EIGENE Zeile — nachgemessen
        an zwei echten Dateien (20.08.2026): die Blocktyp-Mengen waren
        ausschliesslich ('tool_use',)=119, ('thinking',)=101, ('text',)=89, NIE
        gemischt. Damit galt jede thinking- und jede Zwischentext-Zeile als
        Zug-Ende: 245 Urteile ueber beide Dateien, davon 182 falsch (die
        naechste Zeile setzte denselben Zug fort). Live sichtbar als Springen
        der Statuszeile zwischen "Bereit" und "Arbeitet", als "Nicht
        bestaetigt"-Warnung im Chat — und, am teuersten, als ausgehebelte
        Belegt-Vorpruefung von ``_set_effort_boss``, die dann /effort in einen
        ARBEITENDEN Boss tippte.

        Das echte Zug-Ende steht in ``message.stop_reason``: "tool_use"
        solange es weitergeht, "end_turn" bei der letzten API-Antwort. Alle
        Zeilen EINER Antwort tragen dasselbe stop_reason, darum reicht
        end_turn allein nicht — verlangt wird zusaetzlich ein TEXT-Block, denn
        die Antwort endet mit ihrem Text, nicht mit dem Denken davor. Gegen
        dieselben zwei Dateien nachgemessen: 17 Urteile, 0 falsch.

        Alles andere (user/tool_result, assistant MIT tool_use, Subagenten-
        Zeilen, fehlendes stop_reason, Metadaten, unlesbare Riesenzeile) laesst
        bewusst das alte mtime-Verhalten gelten — nur der falsche Nachlauf
        wird entfernt, die Redraw-Luecken-Absicherung bleibt.

        Synchron und billig (ein Tail-Read von max 256KB pro Probe-Tick);
        Aufrufer wrappt in asyncio.to_thread. Fail-silent -> False."""
        try:
            size = path.stat().st_size
            with open(path, "rb") as f:
                f.seek(max(0, size - 262_144))
                tail = f.read()
            lines = [ln for ln in tail.split(b"\n") if ln.strip()]
            if not lines:
                return False
            # Rueckwaerts bis zur letzten INHALTLICHEN Zeile: nach der Antwort
            # schreibt Claude Code noch system-Zeilen (Stop-Hook-Protokoll,
            # Zug-Dauer-Metadaten mit durationMs/messageCount — live gesehen
            # beim ersten Messlauf dieses Fixes, der genau daran scheiterte).
            # Nur user/assistant tragen die Zug-Semantik; alles andere wird
            # uebersprungen. Scan begrenzt — irgendwo in den letzten 50 Zeilen
            # liegt die Semantik immer, sonst gilt konservativ das
            # mtime-Verhalten. lines[0] kann ein abgeschnittener Zeilenrest
            # sein (Chunk-Grenze): unparseabar -> einfach aeltere Zeile, stop.
            for raw in list(reversed(lines))[:50]:
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    return False
                etype = entry.get("type")
                if etype not in ("user", "assistant"):
                    continue
                if etype != "assistant":
                    return False
                if entry.get("isSidechain"):
                    # Ein Subagent beendet nur SEINEN Zug — ueber den Hauptzug
                    # ist damit nichts bewiesen.
                    return False
                message = entry.get("message") or {}
                content = message.get("content")
                if not isinstance(content, list):
                    return False
                types = {b.get("type") for b in content if isinstance(b, dict)}
                if "tool_use" in types:
                    return False
                if message.get("stop_reason") != "end_turn":
                    return False
                return "text" in types
            return False
        except Exception:
            return False

    async def _start_preview(self, agent: Any | None) -> dict[str, Any] | None:
        """Schaltet den Terminal-Strom ein und legt den Emulator an.

        Rueckgabe ist der Zustand, den ``_pump_preview`` fortschreibt — oder
        None, wenn dieser Agent keinen Strom hat (Host-Agenten) oder das
        Einschalten fehlschlug. Fehler sind hier nie fatal.
        """
        if agent is None:
            return None
        try:
            from app.services import pane_stream
            from app.services.pane_preview import PanePreview

            path = await pane_stream.start(agent)
            if path is None:
                return None
            return {"path": path, "offset": 0, "screen": PanePreview(), "last_sent": "", "pending": None}
        except Exception:  # noqa: BLE001
            logger.warning("preview: Start fehlgeschlagen", exc_info=True)
            return None

    async def _pump_preview(self, channel: str, state: dict[str, Any]) -> None:
        """Liest den Zuwachs des Stroms und schickt die Vorschau.

        Zwei Regeln, beide aus Messungen:

        * Gesendet wird erst, wenn ZWEI Durchlaeufe denselben Text ergeben.
          Friert man den Strom mitten im Neuzeichnen ein — und das tut jeder
          Poll —, steht kurz eine halb ueberschriebene Zeile da. Ein Tick
          Verzoegerung kostet 0,3 s und erspart dem Leser das Flackern.
        * Nie eine Dedup-Kennung: die Vorschau konkurriert nicht mit dem
          Transkript, sie wird davon abgeloest.
        """
        path: Path = state["path"]

        def _read() -> bytes:
            try:
                size = path.stat().st_size
            except OSError:
                return b""
            if size < state["offset"]:      # Datei wurde geleert -> von vorn
                state["offset"] = 0
                state["screen"] = state["screen"].__class__()
            if size == state["offset"]:
                return b""
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                chunk = handle.read()
            state["offset"] += len(chunk)
            return chunk

        chunk = await asyncio.to_thread(_read)
        if chunk:
            state["screen"].feed(chunk)

        text = state["screen"].text().strip()
        if not text or text == state["last_sent"]:
            return
        if state["pending"] != text:
            state["pending"] = text      # erst beim naechsten gleichen Stand senden
            return

        state["last_sent"] = text
        await sse.broadcast(
            channel,
            "chat_event",
            {
                "kind": "preview",
                "uuid": None,
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": text,
                "source": "pane",
            },
        )

    async def _compute_pane_state(
        self, agent: Any, current_path: Path, adapter: Any | None = None
    ) -> dict[str, Any]:
        """One probe tick's worth of state classification (A6). Computes
        ``transcript_active`` from the current session file's mtime (used
        both as ``parse_pane_state``'s disambiguation signal and as the
        entire signal for agents ``capture_pane`` can't reach), then either
        parses a captured pane snapshot or falls back to the mtime-only
        heuristic for Boss/host agents — which, per the design brief, must
        never report ``permission_prompt`` since there's no pane text to
        have found one in. Also stamps ``aliveness`` (``resolve_aliveness``)
        into the result — cheap to add here since it rides the same already-
        throttled probe tick (``STATE_PROBE_EVERY_N_TICKS``) rather than
        polling on its own cadence, and its own docker-side check
        (``pane_state.process_alive``) is itself cached ~30s.

        ``adapter`` (omp-Runde) liefert die harness-eigenen Pane-Regeln, die
        Zug-Ende-Probe und den Prozessnamen. ``None`` = Claude Code."""
        if adapter is None:
            from app.services.transcript_adapters import adapter_for

            adapter = adapter_for(agent)
        try:
            mtime = await asyncio.to_thread(lambda: current_path.stat().st_mtime)
            transcript_active = (time.time() - mtime) < self.STATE_ACTIVE_WINDOW_SECONDS
        except OSError:
            transcript_active = False

        # Frische mtime allein heisst nicht "arbeitet": direkt nach dem Zug
        # stammt sie von der Abschluss-Antwort selbst. Endet das Transkript
        # mit einer reinen Text-Antwort, ist der Zug vorbei (Details im
        # Helper) — das nimmt sowohl dem Pane-Parser die falsche
        # working-Aufloesung als auch dem pane-losen Boss-Zweig den
        # 20s-Nachlauf.
        if transcript_active and await asyncio.to_thread(
            adapter.transcript_suggests_turn_ended, current_path
        ):
            transcript_active = False

        aliveness = await resolve_aliveness(agent, current_path, adapter)

        pane_text = await capture_pane(agent)
        if pane_text is None:
            return {
                "status": "working" if transcript_active else "idle",
                "prompt": None,
                "aliveness": aliveness,
            }

        return {
            **adapter.parse_pane_state(pane_text, transcript_active),
            "aliveness": aliveness,
        }


def _read_new_chunk(path: Path, offset: int) -> tuple[int, bytes | None]:
    """Blocking: stat + binary-read the bytes appended since ``offset``.
    Runs via ``asyncio.to_thread`` — never call directly from the event
    loop. Binary mode + byte offsets (not text mode) so a multi-byte UTF-8
    character split across two polls can't be double-counted or truncated;
    the caller decodes only after buffering complete lines.

    Returns ``(new_offset, chunk)``, or ``(offset, None)`` if there's
    nothing new to read (also on a read failure after the stat succeeded).
    """
    size = path.stat().st_size
    if size <= offset:
        return offset, None
    with path.open("rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    return size, chunk


def _peek_uuid(line: str) -> str | None:
    """Best-effort extraction of the top-level ``uuid`` field for live-path
    dedup, mirroring ``read_history``'s ``seen_uuids`` — Claude Code can
    repeat a line verbatim across a resumed session. Never raises."""
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    return d.get("uuid")


tailer_manager = ChatTailerManager()
