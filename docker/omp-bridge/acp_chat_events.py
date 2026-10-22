#!/usr/bin/env python3
"""
acp_chat_events.py — pure mapping: ACP `session/update` params -> omp-format
transcript lines, written as JSONL into the omp sessions tree (Subtask 3/4).

WHY THIS LIVES IN THE BRIDGE: the ACP driver path (OMP_DRIVER=acp, run_acp_once)
never touches the omp TUI transcript the native path writes — the headless
`omp acp` child streams its events over JSON-RPC and they evaporate once the
turn ends. The backend chat view (services/transcript_chat.py
ChatTailerManager + services/omp_chat.py) reads exactly ONE thing: the JSONL
session files under the omp sessions root (see ``session_dir``: the
profile tree ``$OMP_HOME/profiles/$OMP_PROFILE/agent/sessions/`` when a
profile is set, else ``$PI_CODING_AGENT_DIR/sessions/``). So the
bridge writes the ACP events there, in the SAME line format omp itself uses,
and the existing tailer/SSE/history pipeline serves an ACP run like any
native run — no backend change, no frontend change, no second chat path.

FORMAT CONTRACT (verified against real omp transcripts under
~/.omp/profiles/mc-agent/agent/sessions/, 2026-09-08, and mirrored by
backend/tests/test_omp_chat.py):

  * Entry identity / dedup key is the TOP-LEVEL `id` field. Every emitted
    line gets a fresh `acpXXXXX` id — the tailer's seen-set would swallow
    repeat flushes of the same id, and the frontend reducer replaces only on
    equal `${kind}:${uuid}` keys.

  * agent_message_chunk streams one chunk per WORD, all chunks of a message
    sharing one `messageId` (golden fixture rpc/acp-permission-tool.ndjson:
    ~25 chunks for one sentence). omp's own format has no chunk concept —
    each assistant entry is a complete block. Mapping each chunk to a line
    carrying the FULL text so far makes the chat timeline show the message
    growing word by word (newest line on top always contains every previous
    word — order-insensitive by construction), with the final line holding
    the complete text. Mapping only deltas would render reverse-ordered
    fragments; mapping only the finished message would show nothing for the
    whole turn. Per-chunk full snapshots are the only variant that streams
    AND survives display in any order.

  * tool_call seeds the card: an assistant line with a `toolCall` block
    (id=toolCallId, name=mapped from the ACP kind, arguments=rawInput). The
    omp parser turns that block into the frontend's ToolEvent. ACP `title`
    ("$ echo …") travels as the `i` intent argument so omp_chat's
    build_tool_title renders it verbatim.

  * tool_call_update with a terminal status (completed/failed/error) emits a
    `toolResult` message (role "toolResult", toolCallId linking it back);
    read_history/the tailer merge it onto the card by id — the same pairing
    that resolves omp's parallel tool calls. Its ``details`` also carry
    ``diffs`` (ACP content ``type:"diff"`` — path/oldText/newText, one per
    edited file, texts capped at ``_DIFF_TEXT_LIMIT``) and ``locations``
    (the file paths ACP named for the call), both only when present.

  * LIVE TOOL OUTPUT (chat package P1): an in_progress ``tool_call_update``
    carries the WHOLE output so far (omp 18.10.1 sends a snapshot per
    output line, verified in rpc/acp-tool-live-diff-cost-18.10.1.ndjson).
    It becomes a VOLATILE ``custom_message``/``acp-tool-output`` line with
    ``data.toolCallId`` — routed to the preview channel (``is_volatile``),
    never the transcript: ANSI stripped, tail-capped at
    ``_TOOL_OUTPUT_TAIL_CHARS`` on a line boundary, at most one line per
    ``_TOOL_OUTPUT_INTERVAL_S`` per call, identical snapshots skipped. A
    withheld newer snapshot flushes with the next update of any kind OR,
    if the command then goes quiet, via ``ToolOutputPump`` (a burst
    followed by silence must not freeze the phone on its first line). The
    settled toolResult replaces it on the card and drops anything withheld.

  * The ``tool_call`` seed's ``locations`` ride on the ``toolCall`` block as
    ``locations: [path, ...]`` — so a running card already names its file.

  * usage_update (size/used — context window, NOT tokens) is remembered and
    stamped onto assistant lines as message.usage.contextWindow/usedTokens;
    its ``cost`` ({amount, currency}, SESSION-cumulative, sent by omp only
    when > 0) becomes message.usage.sessionCost — a key of its own, because
    omp's native per-message ``usage.cost`` means something else;
    the prompt RESULT's usage (inputTokens/outputTokens, from
    session/prompt reply — same fields omp native writes) becomes a proper
    usage block on the final assistant flush.

  * plan (the agent's full todo list, sent whole on every change) becomes a
    ``custom`` line with customType ``acp-plan`` and ``data.entries`` =
    [{content, status, priority}]. The backend parser maps it to the
    harness-neutral ``todo`` snapshot event (backend chat_todos.py) for the
    chat's progress panel. Before, plan updates were dropped.

  * ACP PREVIEW (Vorschau-Kanal, Folge-PR zu #471): agent text/thought
    chunks NEVER touch this transcript file. They stream into a SIBLING
    file — ``previews/<ts>_<sessionId>_<uniq>.jsonl`` next to the
    transcript copy (both under ``<session folder>/.mc/``) — that the
    backend's tailer (transcript_chat.ChatTailerManager
    via omp_chat.preview_channel) tails as a pure volatile channel,
    broadcasting ``kind: "preview", source: "acp"`` SSE frames. Nothing
    preview-shaped is ever written here, so read_history and the permanent
    timeline stay clean by construction (before: custom_message
    "acp-preview" lines lived in this JSONL and every reader had to filter
    them out).

  * WHERE (S1-R3): the copy, its ``previews/`` and its ``media/`` live in
    ``<session folder>/.mc/``, never next to omp's own file of the same
    session — omp would take the copy for its session and load it
    (``MC_SINK_DIRNAME``). ``sink_migration.py`` moves older copies there.

FAIL-CLOSED PRIVACY: session_dir() derives the target directory from
PI_CODING_AGENT_DIR + the pinned OMP_ACP_CWD only; nothing from prompt,
model output, or tool args widens the path. If the directory cannot be
determined or created, the sink degrades to a no-op — a chat view that shows
nothing is honest, a crash inside the ACP turn is not.

The mapping is PURE and never raises: an unknown/malformed ACP update yields
no events (same policy as OmpLineParser — transcript formats change without
notice, a parser must not die on them).
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# omp entry ids are 8 hex chars; ours carry a distinct prefix so a mixed
# history (native + bridged lines in one file) stays attributable.
_ID_PREFIX = "acp"

# Result text truncation mirrors omp_chat._RESULT_TRUNCATE_LEN (same schema,
# same reason: a 500 KB tool output must not land in the chat view whole).
_RESULT_TRUNCATE_LEN = 4000

# customType of the volatile preview flushes written to the SIBLING preview
# file (never the transcript JSONL). Shared by bridge.run_acp_once's routing
# and the tests.
PREVIEW_CUSTOM_TYPE = "acp-preview"

# customType of a forwarded ACP `plan` update (the agent's todo list). The
# backend's omp_chat parser turns it into a `todo` snapshot event
# (backend/app/services/chat_todos.py).
PLAN_CUSTOM_TYPE = "acp-plan"

# customType of a chat-visible failure line (chat over ACP). The backend's
# _parse_custom_message turns it into an error-styled chat event.
CHAT_ERROR_CUSTOM_TYPE = "chat_error"
#: A notice the operator must see, not an error (map_chat_notice).
CHAT_NOTICE_CUSTOM_TYPE = "chat_notice"

#: MC's display copy of a session (``ChatEventSink``), its preview channel
#: and its pictures live in ``<session folder>/.mc/`` — never next to omp's
#: own session file (S1-R3, docs/specs/chat-over-acp.md "Storage").
#: omp 18.10.1 takes every ``*.jsonl`` whose first line is
#: ``{"type": "session", "id": <string>}`` for one of its sessions (MC's
#: header passes) and finds them with ``Bun.Glob("*.jsonl")`` per folder
#: (``SessionManager.list``) and ``Bun.Glob("*/*.jsonl")`` over the root
#: (``listAll``). Load and resume take the YOUNGER of two files with one id
#: — often MC's copy: live 20.10.2026 the current chat failed to load ("ACP
#: session load was cancelled") and silently started over. Two levels below
#: the root, ``.mc/`` is outside both globs — that alone keeps omp 18.10.1
#: away from it. The leading dot is a second guard for a recursive search
#: some later omp might run: Bun's docs say ``Bun.Glob`` skips dot names
#: unless ``dot: true`` (a local Bun 1.3 agrees; NOT checked at the Bun
#: build inside omp's binary — an assumption). The
#: backend reads the same name (``omp_chat._MC_SINK_DIRNAME``, pinned by
#: backend/tests/test_mc_sink_dirname_contract.py); ``sink_migration.py``
#: moves the copies written before.
MC_SINK_DIRNAME = ".mc"
#: The longest session header line read (a header is a few hundred bytes).
_HEADER_BYTES = 65536

_TITLE_TRUNCATE_LEN = 200

#: A thought's preview line carries only the END of the thought (operator
#: finding 20.10.2026): every line used to carry the WHOLE thought — 1 097
#: lines / 33 MB of preview file in five minutes of DeepSeek reasoning — and
#: the chat only ever shows the last lines of a running thought anyway.
#: ``data.chars`` keeps the full size, so the chat can still say how much.
THINKING_PREVIEW_TAIL_CHARS = 2000
#: A finished thought kept in the transcript is capped: head + tail survive
#: (the plan at the start, the conclusion at the end), the middle is cut.
_THINKING_KEEP_CHARS = 12000

# Chat package P3 — question cards and plan mode.
# A question omp asks over ACP (`elicitation/create`, form mode): ONE
# ``custom_message`` line carrying the question text (``content``, so an
# older backend still shows it as a plain notice) and in ``data`` the card's
# id + the requested schema the frontend builds buttons/fields from.
ELICITATION_CUSTOM_TYPE = "acp-elicitation"
# The fate of that question, as its own ``custom`` line (state, not a turn):
# ``data = {elicitationId, status: answered|declined|cancelled|expired,
# content?}``. The card reads it to show "answered: X" for good — also after
# a reload, because it is in the transcript.
ELICITATION_RESULT_CUSTOM_TYPE = "acp-elicitation-result"
# ACP ``current_mode_update`` (plan mode on/off) as a quiet ``custom`` line
# ``data = {modeId}`` — the composer's mode chip follows it.
MODE_CUSTOM_TYPE = "acp-mode"

# customType of a live tool-output snapshot (chat package P1). VOLATILE: it
# rides the preview channel next to the text previews and never enters the
# transcript JSONL — see ``is_volatile``. The backend's preview-channel
# reader turns it into a ``tool_output`` chat event.
TOOL_OUTPUT_CUSTOM_TYPE = "acp-tool-output"

# Live output: the newest this many characters (cut back to a whole line) —
# a phone shows ~12 lines; a 50 MB `npm install` log must never travel.
_TOOL_OUTPUT_TAIL_CHARS = 2000
# At most one snapshot per call per this many seconds (omp sends one per
# output line; a chatty build would otherwise write hundreds per second).
_TOOL_OUTPUT_INTERVAL_S = 0.5

# Per-file diff texts (whole-file old/new from omp) are capped here; a
# capped pair is marked ``truncated`` so the card can say so.
_DIFF_TEXT_LIMIT = 60_000
_MAX_DIFFS = 20
_MAX_LOCATIONS = 20
_LOCATION_LEN = 500

# Images from tool results (chat package P2). The bytes NEVER enter the
# transcript JSONL: the settled toolResult line carries only a reference
# (``details.images``) and the picture lands as a file in
# ``<session dir>/media/<sha256>.<ext>`` (see ``ChatEventSink.write_media``).
# Only these four raster types, recognised by their MAGIC BYTES — the label
# a tool puts on the block is not trusted (an SVG can carry script).
_IMAGE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGES = 8
_MEDIA_NAME_RE = re.compile(r"^[a-f0-9]{64}\.(?:png|jpg|webp|gif)$")


def sniff_image_mime(data: bytes) -> Optional[str]:
    """The image type the BYTES say, or None for anything off the list."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _image_blocks(update: dict[str, Any]) -> list[dict[str, Any]]:
    """The image blocks of a settled update: ACP ``content`` items
    (``{"type":"content","content":{"type":"image",...}}``) first, then
    ``rawOutput.content`` bare blocks. omp sends the same picture in both
    (fixture acp-tool-image-18.10.1) — duplicates fold by hash later."""
    blocks: list[dict[str, Any]] = []
    content = update.get("content")
    if isinstance(content, list):
        for item in content:
            inner = item.get("content") if isinstance(item, dict) else None
            if isinstance(inner, dict) and inner.get("type") == "image":
                blocks.append(inner)
    raw = update.get("rawOutput")
    if isinstance(raw, dict):
        raw = raw.get("content")
    if isinstance(raw, list):
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "image":
                blocks.append(block)
    return blocks


def _extract_images(update: dict[str, Any]) -> list[tuple[str, str, bytes]]:
    """(sha256, mime, bytes) per distinct, valid picture — capped. Anything
    that is not base64 of a whitelisted raster type is dropped silently
    (an omp ``blob:sha256:`` reference has no bytes here; it never resolves
    inside the bridge)."""
    import base64
    import binascii
    import hashlib

    out: list[tuple[str, str, bytes]] = []
    seen: set[str] = set()
    for block in _image_blocks(update):
        data = block.get("data")
        if not isinstance(data, str) or not data or data.startswith("blob:"):
            continue
        # base64 is 4/3 the size: a string this long cannot fit the cap.
        if len(data) > (_MAX_IMAGE_BYTES * 4) // 3 + 8:
            continue
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            continue
        if not raw or len(raw) > _MAX_IMAGE_BYTES:
            continue
        mime = sniff_image_mime(raw)
        if mime is None:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)
        out.append((sha, mime, raw))
        if len(out) >= _MAX_IMAGES:
            break
    return out

# CSI (ESC [ … final), OSC (ESC ] … BEL/ST) and lone two-byte escapes, plus
# C0 controls except \n and \t. Terminal colour codes are noise on a phone.
_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\-_]"
)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Terminal escapes out, CRLF/CR to LF, other C0 controls out."""
    s = _ANSI_RE.sub("", text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL_RE.sub("", s)


#: Updates that end a running thought (the model moved on). Bookkeeping
#: updates (usage, config, commands, session info) do not: omp can send them
#: in the middle of a long thought.
_THOUGHT_ENDING_UPDATES = frozenset({
    "agent_message_chunk", "tool_call", "tool_call_update", "plan",
})

#: Updates that end a text SEGMENT (stream mode, spec A D7): the agent's
#: words written so far land as their own permanent line before the tool
#: call, the new thought or the plan — the chat reads them where they were
#: written. Not ``tool_call_update`` (a running tool's output is not a new
#: step) and not the bookkeeping updates (usage, config, mode).
_TEXT_ENDING_UPDATES = frozenset({"tool_call", "agent_thought_chunk", "plan"})

#: Chunk updates that are a boundary only when they carry words: an empty
#: or non-text chunk writes nothing, so it must not end a segment or a
#: thought either (review #936 S1 — empty reasoning deltas would otherwise
#: break the chat into mid-word bubbles).
_CHUNK_UPDATES = frozenset({"agent_message_chunk", "agent_thought_chunk"})


def _carries_text(update: dict[str, Any]) -> bool:
    """True when a chunk update holds a non-empty text block."""
    content = update.get("content")
    if not isinstance(content, dict) or content.get("type") != "text":
        return False
    text = content.get("text")
    return isinstance(text, str) and bool(text)


def is_volatile(entry: dict[str, Any]) -> bool:
    """True for lines that belong on the PREVIEW channel, never the
    transcript: text previews and live tool output. Both callers
    (bridge.run_acp_once and the chat daemon) route on this."""
    return entry.get("customType") in (PREVIEW_CUSTOM_TYPE, TOOL_OUTPUT_CUSTOM_TYPE)


def is_text_line(entry: dict[str, Any]) -> bool:
    """True for a permanent assistant TEXT line (a segment or the turn's
    final line). Both callers reset their preview throttle when one is
    written: a withheld snapshot of that text must never reach the preview
    channel after it (D7 — it would show the same words twice)."""
    if entry.get("type") != "message":
        return False
    message = entry.get("message") or {}
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return False
    return any(isinstance(b, dict) and b.get("type") == "text"
               for b in message.get("content") or [])

# ACP toolCall status values that settle a tool call.
_TERMINAL_STATUSES = ("completed", "failed", "error")

# ACP tool kinds -> omp-ish tool names, so omp_chat.build_tool_title renders
# a readable card title. Unknown kinds pass through unchanged.
_KIND_TO_NAME = {
    "execute": "bash",
    "read": "read",
    "edit": "edit",
    "write": "write",
    "delete": "delete",
    "search": "grep",
}


#: A tool name as omp writes it (``ask``, ``browser``, ``web_search`` …).
_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")

#: How much of omp's own session file the name lookup reads (from the end):
#: the call was written moments ago, so it sits near the end.
_NAME_LOOKUP_TAIL_BYTES = 512 * 1024


def omp_tool_name(session_file: Optional[Path], tool_call_id: str) -> Optional[str]:
    """The tool name omp stored for ``tool_call_id`` in its own session
    file (``{"type":"toolCall","id":…,"name":…}``), or ``None``. Reads only
    the file's tail. Never raises."""
    if session_file is None or not tool_call_id:
        return None
    try:
        size = session_file.stat().st_size
        with open(session_file, "rb") as fh:
            fh.seek(max(0, size - _NAME_LOOKUP_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(
        r'"id"\s*:\s*"' + re.escape(tool_call_id) + r'"\s*,\s*"name"\s*:\s*"([^"]{1,64})"',
        tail,
    )
    return match.group(1) if match else None


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _tail_on_boundary(text: str, limit: int) -> str:
    """The last ``limit`` chars of ``text``, starting at a line or word
    boundary when one is near (a preview should not open mid-word)."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    # The EARLIEST boundary in reach (review R3: max() skipped the cut
    # whenever the first newline lay deeper than the window).
    cuts = [i for i in (tail.find("\n"), tail.find(" ")) if 0 <= i < 200]
    if cuts:
        tail = tail[min(cuts) + 1:]
    return tail


def _keep_thought(text: str) -> str:
    if len(text) <= _THINKING_KEEP_CHARS:
        return text
    half = _THINKING_KEEP_CHARS // 2
    return text[:half] + "\n…\n" + text[-half:]


def _trunc(text: Any, limit: int = _RESULT_TRUNCATE_LEN) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def _content_text(content: Any) -> str:
    """Flatten an ACP `content`/`rawOutput.content` block list to text.
    Non-text blocks are skipped — fail closed: an image blob is never
    rendered as text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block.get("type") == "content":
            # omp's own nesting, seen in the permission fixture's tool content.
            inner = block.get("content")
            if isinstance(inner, dict) and inner.get("type") == "text":
                text = inner.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _tail(text: str, limit: int = _TOOL_OUTPUT_TAIL_CHARS) -> str:
    """The newest ``limit`` characters, cut back to a whole line and marked
    with a leading ``…`` line when anything was dropped."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    return "…\n" + tail


def _clean_locations(raw: Any) -> list[str]:
    """ACP ``locations`` ([{path, line?}]) -> unique path strings, capped.
    Anything not a non-empty string path is dropped (fail closed)."""
    out: list[str] = []
    if not isinstance(raw, list):
        return out
    for loc in raw:
        path = loc.get("path") if isinstance(loc, dict) else None
        if not isinstance(path, str) or not path.strip():
            continue
        path = path.strip()[:_LOCATION_LEN]
        if path not in out:
            out.append(path)
        if len(out) >= _MAX_LOCATIONS:
            break
    return out


def _clean_diffs(content: Any) -> list[dict[str, Any]]:
    """ACP tool ``content`` -> the ``type:"diff"`` items as
    {path, oldText, newText[, truncated]}. ``oldText`` stays ``None`` for a
    created file (ACP: null). Texts are capped at ``_DIFF_TEXT_LIMIT``."""
    out: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return out
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "diff":
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        old, new = item.get("oldText"), item.get("newText")
        old = old if isinstance(old, str) else None
        new = new if isinstance(new, str) else ""
        diff: dict[str, Any] = {"path": path.strip()[:_LOCATION_LEN], "oldText": old, "newText": new}
        if (old is not None and len(old) > _DIFF_TEXT_LIMIT) or len(new) > _DIFF_TEXT_LIMIT:
            diff["oldText"] = old[:_DIFF_TEXT_LIMIT] if old is not None else None
            diff["newText"] = new[:_DIFF_TEXT_LIMIT]
            diff["truncated"] = True
        out.append(diff)
        if len(out) >= _MAX_DIFFS:
            break
    return out


def _clean_cost(raw: Any) -> Optional[dict[str, Any]]:
    """ACP usage_update ``cost`` -> {amount, currency} only for a real,
    positive, finite amount; anything else -> None (never a fake figure)."""
    if not isinstance(raw, dict):
        return None
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None
    currency = raw.get("currency")
    currency = currency.strip().upper()[:8] if isinstance(currency, str) and currency.strip() else "USD"
    return {"amount": float(amount), "currency": currency}


def _cut_rest(text: str, cut_forms: tuple) -> str:
    """The final line's rest after the chat's verdict (S1): ``()`` hides it
    all, else omp's error line (in any of ``cut_forms``) is cut off its END
    only. Segments written earlier are never touched — they are on screen."""
    if not cut_forms:
        if text.strip():
            sys.stderr.write("[acp] verdict hides the reply rest; earlier segments stay\n")
        return ""
    import model_errors  # noqa: PLC0415 — sibling module, only for this cut

    return model_errors.reply_without_omp_line(text, cut_forms)


@dataclass
class ACPEventMapper:
    """Accumulates one ACP run's events into omp-format transcript line dicts.

    One instance per bridge run (= one ACP session). Consumed by
    bridge.run_acp_once via a ChatEventSink; also usable standalone in tests.

    Streaming contract (Review #465 Option b, narrowed by spec A D7 —
    docs/specs/chat-over-acp.md "Text segments"):
    - ``map_update(params, stream=True)`` maps agent text chunks into
      PREVIEW lines (``custom_message``/``acp-preview`` carrying the growing
      snapshot of the CURRENT segment). The chat reducer treats preview as
      replace-me; it never stacks bubbles. Previews never enter the
      transcript.
    - ONE permanent assistant text line per text SEGMENT. A segment ends at
      a tool call, the start of a thought, a plan, a permission/elicitation
      card, a chat notice/error line (``_flush_text``) or the turn end
      (``map_final_assistant_message``). A turn without such a boundary
      still writes exactly one line.
    - ``usage`` + ``stopReason`` are stamped ONLY on the turn's last
      assistant line (never 0/0 per chunk); when no text is left for it,
      that line has ``content: []``. The final line never repeats flushed
      text and never contains thought text.
    - ``set_session_id`` carries the REAL ``session/new`` id into the sink
      (file name + session header), replacing the "acp-session" stub.
    """

    seq: int = 0
    # agent_message_chunk accumulation: messageId -> text of the open
    # segment so far. Cleared when the segment is written (``_flush_text``).
    _text_by_message: dict[str, str] = field(default_factory=dict)
    # The keys of ``_text_by_message`` in chunk-arrival order (D7: a
    # segment is joined in the order it was written, never by dict order).
    _text_order: list[str] = field(default_factory=list)
    # This turn already wrote a text segment line / a thinking line: the
    # final line then carries only the mapper's own unflushed rest, and
    # always exists (``content: []`` when empty) to carry stopReason+usage.
    _turn_flushed_any: bool = False
    _turn_thought_any: bool = False
    _thought_by_message: dict[str, str] = field(default_factory=dict)
    # toolCallId -> {"name", "title", "arguments"} as seeded by tool_call.
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Latest usage_update (context window size / used tokens), if any.
    _usage_window: Optional[dict[str, int]] = None
    # Token usage from the session/prompt result (set via set_prompt_usage).
    _prompt_usage: Optional[dict[str, Any]] = None
    # REAL ACP sessionId (set via set_session_id once session/new replied).
    _session_id: Optional[str] = None
    # Latest usage_update cost ({amount, currency}), session-cumulative.
    _cost: Optional[dict[str, Any]] = None
    # Live tool output (P1): toolCallId -> {"text", "at", "pending"}.
    _live: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Injectable monotonic clock (tests drive the throttle deterministically).
    clock: Callable[[], float] = field(default=time.monotonic)
    # The thought being streamed right now (stream mode): its messageId key,
    # its start (ISO for the chat, monotonic for the duration). It is kept
    # ONCE in the transcript when it ends — see ``_flush_thought``.
    _open_thought: Optional[dict[str, Any]] = None
    # map_update runs on the ACP reader thread, ToolOutputPump on its own:
    # one re-entrant lock keeps the live state and the entry ids consistent.
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)
    # ACP carries no tool NAME — only a coarse ``kind`` ("other" for ask,
    # task, browser, …) and a title (often the model's intent). The chat
    # daemon plugs in a lookup into omp's own session file, where the call
    # is stored with its real name (P3 live finding: "other: …" cards).
    tool_name_resolver: Optional[Callable[[str], Optional[str]]] = field(
        default=None, repr=False, compare=False
    )

    # ------------------------------------------------------------------
    # mapping
    # ------------------------------------------------------------------

    def map_update(
        self, params: dict[str, Any], *, stream: bool = False
    ) -> list[dict[str, Any]]:
        """One `session/update` params dict -> 0..n omp-format transcript
        line dicts. Never raises; unknown shapes yield [].

        ``stream=True`` (the live bridge path): agent text chunks become
        PREVIEW lines instead of permanent assistant messages (Option b).
        ``stream=False`` (legacy/tests): chunks map to assistant message
        lines exactly as before.
        """
        try:
            update = params.get("update") or {}
            if not isinstance(update, dict):
                return []
            with self._lock:
                # A withheld live-output snapshot whose interval has passed
                # rides out with whatever update comes next (P1 coalescing).
                flushed = self._flush_due_tool_output()
                return flushed + self._map_one(update, stream)
        except Exception:  # noqa: BLE001 — a broken update must not kill the run
            return []

    def flush_tool_output(self) -> list[dict[str, Any]]:
        """Withheld live-output snapshots whose interval has passed — for
        ``ToolOutputPump`` when no further update arrives. Never raises."""
        try:
            with self._lock:
                return self._flush_due_tool_output()
        except Exception:  # noqa: BLE001
            return []

    def _map_one(self, update: dict[str, Any], stream: bool) -> list[dict[str, Any]]:
        su = update.get("sessionUpdate")
        if su in _CHUNK_UPDATES and not _carries_text(update):
            # An empty (or non-text) chunk writes nothing and ends nothing:
            # neither the open text segment nor the open thought.
            return self._map_one_inner(update, su, stream)
        # The agent's words so far end where a tool call, a thought or a
        # plan begins (D7): they land as their own line first.
        pre = self._flush_text() if stream and su in _TEXT_ENDING_UPDATES else []
        if su in _THOUGHT_ENDING_UPDATES or (
            su == "agent_thought_chunk" and self._other_thought(update)
        ):
            # The model stopped thinking (it answers, calls a tool, plans) or
            # began a new thought: keep the finished one first, so the
            # transcript reads in the order it happened.
            return pre + self._flush_thought() + self._map_one_inner(update, su, stream)
        return pre + self._map_one_inner(update, su, stream)

    def _map_one_inner(
        self, update: dict[str, Any], su: Any, stream: bool
    ) -> list[dict[str, Any]]:
        if su == "agent_message_chunk":
            if stream:
                return self._preview_chunk(update, "text")
            return self._chunk(update, self._text_by_message, "text")
        if su == "agent_thought_chunk":
            if stream:
                return self._preview_chunk(update, "thinking")
            return self._chunk(update, self._thought_by_message, "thinking")
        if su == "tool_call":
            return self._tool_seed(update)
        if su == "tool_call_update":
            return self._tool_update(update)
        if su == "usage_update":
            size, used = update.get("size"), update.get("used")
            if isinstance(size, int) and size > 0 and isinstance(used, int):
                self._usage_window = {"size": size, "used": used}
            cost = _clean_cost(update.get("cost"))
            if cost is not None:
                self._cost = cost
            return []
        if su == "plan":
            return self._plan(update)
        if su == "current_mode_update":
            return self.map_mode(update.get("currentModeId"))
        # config_option_update / available_commands_update /
        # session_info_update / anything unknown — no chat value
        # (the chat daemon mirrors them into its state file instead).
        return []

    def _other_thought(self, update: dict[str, Any]) -> bool:
        if self._open_thought is None:
            return False
        message_id = update.get("messageId")
        key = message_id if isinstance(message_id, str) and message_id else "_anon"
        return key != self._open_thought["key"]

    def _flush_thought(self) -> list[dict[str, Any]]:
        """The open thought -> ONE permanent assistant ``thinking`` line
        (``durationMs`` = how long the model thought), or [] when none is
        open. Called when the thought ends: the next answer chunk, tool call
        or plan, the turn's final line, a chat error. The backend parser
        (omp_chat) already turns a ``thinking`` block into the chat's
        ``thinking`` event — the history keeps "thought for 1:45"."""
        open_thought, self._open_thought = self._open_thought, None
        if open_thought is None:
            return []
        text = self._thought_by_message.pop(open_thought["key"], "")
        if not text.strip():
            return []
        duration_ms = max(0, int(round((self.clock() - open_thought["mono"]) * 1000)))
        self._turn_thought_any = True
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "thinking",
                        "thinking": _keep_thought(text),
                        "durationMs": duration_ms,
                    }],
                    "model": None,
                },
            }
        ]

    def _take_text(self) -> str:
        """The open segment's text in chunk-arrival order; clears it, so the
        next preview snapshot holds only what comes after."""
        with self._lock:
            keys = self._text_order + [k for k in self._text_by_message
                                       if k not in self._text_order]
            self._text_order = []
            return "".join(self._text_by_message.pop(k, "") for k in keys)

    def _flush_text(self) -> list[dict[str, Any]]:
        """The open text segment -> ONE permanent assistant text line
        (stream mode, D7), or [] when there is none or it is only
        whitespace. No ``stopReason`` and no ``usage``: both belong to the
        turn's last line only (``map_final_assistant_message``)."""
        with self._lock:
            text = self._take_text()
            if not text.strip():
                return []
            self._turn_flushed_any = True
            return [
                {
                    "type": "message",
                    "id": self._next_id(),
                    "parentId": None,
                    "timestamp": _now_iso(),
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                        "model": None,
                        "stopReason": None,
                    },
                }
            ]

    def flush_on_abort(self) -> list[dict[str, Any]]:
        """A run that ends without a prompt result (error, abort): the open
        thought and the open text segment still land as lines — never lost
        with the run (N11). Neither carries a stop reason. Never raises."""
        try:
            with self._lock:
                return self._flush_thought() + self._flush_text()
        except Exception:  # noqa: BLE001 — the error path must not break
            return []

    def map_mode(self, mode_id: Any) -> list[dict[str, Any]]:
        """``current_mode_update`` -> one quiet ``custom``/``acp-mode`` line.
        Not a card: the composer's mode chip reads it (newest wins)."""
        if not isinstance(mode_id, str) or not mode_id.strip():
            return []
        return [
            {
                "type": "custom",
                "customType": MODE_CUSTOM_TYPE,
                "data": {"modeId": mode_id.strip()[:64]},
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def map_elicitation(
        self, elicitation_id: str, message: str, schema: dict[str, Any],
        *, plan_approval: bool = False,
    ) -> list[dict[str, Any]]:
        """An open question (``elicitation/create``) -> one visible
        ``custom_message``/``acp-elicitation`` line. ``schema`` is the
        already-cleaned ``requestedSchema`` (see acp_chat.clean_schema)."""
        text = str(message or "").strip() or "?"
        return self._flush_text() + [
            {
                "type": "custom_message",
                "customType": ELICITATION_CUSTOM_TYPE,
                "content": text[:_RESULT_TRUNCATE_LEN],
                "display": True,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "data": {
                    "elicitationId": elicitation_id,
                    "message": text[:_RESULT_TRUNCATE_LEN],
                    "schema": schema,
                    # omp's plan approval (acp_chat.is_plan_approval): the card
                    # offers "Cancel (stops agent)" instead of "Skip".
                    "planApproval": bool(plan_approval),
                },
            }
        ]

    def map_elicitation_result(
        self, elicitation_id: str, status: str, content: Optional[dict[str, Any]] = None
    ) -> list[dict[str, Any]]:
        """What became of a question -> one ``custom``/``acp-elicitation-result``
        line (state for the card, never a row of its own)."""
        data: dict[str, Any] = {"elicitationId": elicitation_id, "status": status}
        if content:
            data["content"] = content
        return [
            {
                "type": "custom",
                "customType": ELICITATION_RESULT_CUSTOM_TYPE,
                "data": data,
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def _plan(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        """ACP ``plan`` -> one ``custom``/``acp-plan`` transcript line.

        A plan is the agent's full todo list (ACP sends the WHOLE list on
        every change). The backend (``omp_chat`` -> ``chat_todos``) turns
        the line into a harness-neutral ``todo`` snapshot for the chat's
        progress panel; newest line wins. A ``custom`` entry, not a
        ``custom_message``: it is state, not a conversation turn — an older
        backend that does not know ``acp-plan`` skips ``custom`` lines
        silently instead of printing them as a system notice.

        Fail closed: only the three ACP entry fields (content/status/
        priority, strings) are copied; anything else (``_meta``) stays out
        of the transcript. Not a list -> no line.
        """
        raw = update.get("entries")
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            clean = {"content": _trunc(content.strip(), _TITLE_TRUNCATE_LEN)}
            for key in ("status", "priority"):
                value = entry.get(key)
                if isinstance(value, str) and value:
                    clean[key] = value[:32]
            entries.append(clean)
        return [
            {
                "type": "custom",
                "customType": PLAN_CUSTOM_TYPE,
                "data": {"entries": entries},
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def begin_turn(self) -> None:
        """Reset the per-turn chunk accumulators, keeping identity + seq.

        run_acp_once builds one mapper per attempt; the chat daemon reuses ONE
        mapper for the whole session. Without this reset a second turn reusing
        a messageId would render the previous turn's text prepended — and
        resetting `seq` instead would re-issue entry ids the tailer already
        deduplicated away.
        """
        self._text_by_message.clear()
        self._text_order.clear()
        self._thought_by_message.clear()
        self._open_thought = None
        self._prompt_usage = None
        self._turn_flushed_any = False
        self._turn_thought_any = False

    def set_session_id(self, session_id: Optional[str]) -> None:
        """Carry the REAL `session/new` sessionId into the sink (Review
        #465 low 5): file name + session header use it, so the chat view's
        session rollover keys on the actual ACP session, not a stub."""
        if isinstance(session_id, str) and session_id.strip():
            self._session_id = session_id.strip()

    def set_prompt_usage(self, usage: Optional[dict[str, Any]]) -> None:
        """Record the session/prompt reply's usage (inputTokens/outputTokens
        etc.) for stamping onto the FINAL assistant line (never per chunk)."""
        if isinstance(usage, dict) and usage:
            self._prompt_usage = usage

    def map_permission_request(
        self, params: dict[str, Any], *, auto_approved: bool = False
    ) -> list[dict[str, Any]]:
        """`session/request_permission` -> a visible custom_message line
        (teammate role in the chat view): the operator sees WHAT asked and
        which choices existed.

        ``auto_approved`` (the caller's `yolo` policy: nobody is actually
        being asked, `bridge._acp_permission_decision` answers on its own)
        sets ``display: False`` instead of dropping the entry — the line
        stays in the transcript file (still useful for a later `dump`/replay
        read), but `omp_chat._parse_custom_message`'s existing `display is
        False` gate (same one `acp-preview`/silent notices already rely on)
        keeps it out of the chat the operator reads. Task 663f70fb: five of
        these interleaved with every tool card crowded out the actual
        conversation, and a policy that never asks has nothing to show.
        """
        tool = params.get("toolCall") or {}
        title = self._tool_title_of(tool)
        kind = tool.get("kind") or "tool"
        options = [
            str(o.get("name") or o.get("optionId") or "")
            for o in (params.get("options") or [])
            if isinstance(o, dict)
        ]
        question = f"Freigabe-Anfrage ({kind}): {title}"
        named = [o for o in options if o]
        if named:
            question += " [" + " | ".join(named) + "]"
        return self._flush_text() + [
            {
                "type": "custom_message",
                "customType": "acp-permission",
                "content": question,
                "display": not auto_approved,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def map_permission_outcome(
        self, params: dict[str, Any], choice: str, *, auto_approved: bool = False
    ) -> list[dict[str, Any]]:
        """The decision taken on a permission request, as its own line.

        Same ``auto_approved`` -> ``display: False`` rule as
        :meth:`map_permission_request` — see its docstring.
        """
        tool = params.get("toolCall") or {}
        title = self._tool_title_of(tool)
        return [
            {
                "type": "custom_message",
                "customType": "acp-permission-decision",
                "content": f"Freigabe entschieden: {choice} — {title}",
                "display": not auto_approved,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def map_user_prompt(
        self, prompt_text: str, *, origin: Optional[str] = None,
        task_id: str = "", title: Optional[str] = None,
        redispatch: bool = False,
    ) -> list[dict[str, Any]]:
        """The user turn itself -> a user message line, so the chat view shows
        what the bridge asked (the native transcript carries the same via the
        TUI's input echo).

        ``origin`` names who wrote the turn when it was NOT the operator:
        ``"task"`` (a task dispatch prompt) or ``"nudge"`` (a continue-nudge).
        Such a line carries ``attribution: "mc"`` + ``origin``, and the chat
        renders it as an event "from Mission Control" — not as a bubble the
        operator typed (incident 19.10.2026: the whole Operating Card of a
        bench task showed up as the operator's own message). The native TUI
        path gets the same via its ``@task-….md`` mention.

        ``title`` / ``redispatch`` (task prompts only): the task's title from
        its ``# New Task:`` / ``# CORRECTION NEEDED:`` header, and whether it
        is a correction re-dispatch — the chat titles the task event with it
        instead of re-parsing the prompt text."""
        text = str(prompt_text or "").strip()
        if not text:
            return []
        message: dict[str, Any] = {
            "role": "user",
            "content": [{"type": "text", "text": text[:8000]}],
            "attribution": "user",
        }
        if origin:
            message["attribution"] = "mc"
            message["origin"] = {"kind": str(origin), "taskId": str(task_id or "") or None}
            if title:
                message["origin"]["title"] = str(title)[:200]
            if redispatch:
                message["origin"]["redispatch"] = True
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": message,
            }
        ]

    def map_chat_notice(
        self, code: str, text: str, data: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        """A chat-visible NOTICE -> ONE ``chat_notice`` custom_message line.

        Not an error: the turn goes on, but the operator must learn that
        something was decided for them (e.g. ``model_fallback`` — the model
        the session was pinned to is no longer offered, another one is used).
        ``data`` carries ``code`` plus the values the frontend fills into its
        localized sentence; ``content`` is the English fallback text."""
        payload = {"code": code}
        payload.update({k: v for k, v in (data or {}).items() if v is not None})
        return self._flush_text() + [
            {
                "type": "custom_message",
                "customType": CHAT_NOTICE_CUSTOM_TYPE,
                "content": str(text or code)[:_RESULT_TRUNCATE_LEN],
                "display": True,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "data": payload,
            }
        ]

    def map_chat_error(
        self, code: str, detail: str = "", text: Optional[str] = None,
        data: Optional[dict] = None,
    ) -> list[dict[str, Any]]:
        """A chat-visible failure -> ONE ``chat_error`` custom_message line.

        Chat over ACP (spec docs/specs/chat-over-acp.md): errors must land in
        the timeline as an EVENT, not only in a log the operator never sees.
        ``data`` carries the machine-readable pair the frontend styles on
        (``code`` chip + ``detail`` body); ``content`` is the human text.
        Codes: rpc_error, provider_error, empty_turn, process_exit, busy,
        session_reset, turn_interrupted, queue_lost, ...

        The ``data`` argument adds fields of the card (``retryText`` for the
        "Send again" button, ``model`` for ``model_missing``). They never
        replace ``code`` or ``detail``, a None value is left out, and they
        are not cut: a text cut short is a text the operator never wrote.
        Without it the line is exactly what it always was.
        """
        body = text or detail or code
        payload: dict[str, Any] = {"code": code, "detail": str(detail)[:_RESULT_TRUNCATE_LEN]}
        payload.update({k: v for k, v in (data or {}).items()
                        if v is not None and k not in payload})
        return self._flush_text() + self._flush_thought() + [
            {
                "type": "custom_message",
                "customType": CHAT_ERROR_CUSTOM_TYPE,
                "content": str(body)[:_RESULT_TRUNCATE_LEN],
                "display": True,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "data": payload,
            }
        ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        with self._lock:
            self.seq += 1
            return f"{_ID_PREFIX}{self.seq:05x}"

    @staticmethod
    def _tool_title_of(tool: dict[str, Any]) -> str:
        title = tool.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()[:_TITLE_TRUNCATE_LEN]
        raw = tool.get("rawInput")
        if isinstance(raw, dict):
            cmd = raw.get("command")
            if isinstance(cmd, str) and cmd.strip():
                return ("$ " + cmd.strip())[:_TITLE_TRUNCATE_LEN]
        return str(tool.get("kind") or "tool")[:_TITLE_TRUNCATE_LEN]

    def _usage_block(self) -> Optional[dict[str, Any]]:
        """message.usage in the shape omp_chat._parse_assistant reads:
        input/cacheRead/cacheWrite/output counts. Token counts come from the
        prompt result; the context window from usage_update. Neither present
        -> None (no invented numbers).

        cacheRead/cacheWrite used to be hard-coded 0 (#464 mid), so the chat
        never showed a cache hit rate for an ACP agent. omp's ACP
        `session/prompt` result carries them as `cachedReadTokens`/
        `cachedWriteTokens` — the ACP spec's own field names (verified:
        agent_client_protocol 0.9.0 `acp/schema.py` `Usage` model; UNSTABLE
        per the spec but the real wire shape, not an omp-only convention).
        Missing on a given turn (older omp, or a turn with nothing cached)
        -> 0, same "no cache activity" meaning omp's native format uses."""
        if not self._prompt_usage and not self._usage_window:
            return None
        usage = self._prompt_usage or {}
        window = self._usage_window or {}
        block: dict[str, Any] = {
            "input": int(usage.get("inputTokens") or 0),
            "cacheRead": int(usage.get("cachedReadTokens") or 0),
            "cacheWrite": int(usage.get("cachedWriteTokens") or 0),
            "output": int(usage.get("outputTokens") or 0),
            "contextWindow": window.get("size"),
            "usedTokens": window.get("used"),
        }
        # P1: the session's running cost, only when the harness reported a
        # real one (omp omits it at 0 — a local model costs nothing, and
        # "0.00 $" next to it would be a number nobody measured).
        if self._cost is not None:
            block["sessionCost"] = dict(self._cost)
        return block

    def _chunk(
        self,
        update: dict[str, Any],
        store: dict[str, str],
        block_type: str,
    ) -> list[dict[str, Any]]:
        content = update.get("content") or {}
        if not isinstance(content, dict) or content.get("type") != "text":
            return []
        text = content.get("text")
        if not isinstance(text, str) or not text:
            return []
        message_id = update.get("messageId")
        key = message_id if isinstance(message_id, str) and message_id else "_anon"
        store[key] = store.get(key, "") + text
        # Review #465 mid 3: NO usage on chunk lines — every chunk carried a
        # 0/0 usage block once _prompt_usage landed, which fed the token
        # counter (#393) garbage. Usage lives ONLY on the final message.
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "assistant",
                    "content": [{"type": block_type, block_type: store[key]}],
                    "model": None,
                },
            }
        ]

    def _preview_chunk(
        self,
        update: dict[str, Any],
        block_type: str,
    ) -> list[dict[str, Any]]:
        """Vorschau-Kanal (Folge-PR zu #471): one agent text chunk -> a
        PREVIEW line for the SIBLING preview file, keyed by the SAME
        customType every flush so the chat's preview slot replaces (never
        stacks). These lines NEVER enter the transcript JSONL —
        bridge.run_acp_once routes them to the preview sink — so the
        permanent timeline holds one assistant line per text segment (D7)
        and the volatile channel holds the growing snapshot of the open
        segment. No usage, no fresh message identity per chunk."""
        content = update.get("content") or {}
        if not isinstance(content, dict) or content.get("type") != "text":
            return []
        text = content.get("text")
        if not isinstance(text, str) or not text:
            return []
        message_id = update.get("messageId")
        key = message_id if isinstance(message_id, str) and message_id else "_anon"
        store = self._text_by_message if block_type == "text" else self._thought_by_message
        if block_type == "text" and key not in store:
            self._text_order.append(key)
        store[key] = store.get(key, "") + text
        line: dict[str, Any] = {
            "type": "custom_message",
            "customType": PREVIEW_CUSTOM_TYPE,
            "content": store[key],
            "display": True,
            "attribution": "agent",
            "id": self._next_id(),
            "parentId": None,
            "timestamp": _now_iso(),
        }
        if block_type == "thinking":
            # The chat must SAY the model is thinking (20.10.2026: minutes of
            # reasoning looked like "nothing happens"): mark the line, carry
            # the size and the start, and only the end of the text.
            if self._open_thought is None or self._open_thought["key"] != key:
                self._open_thought = {
                    "key": key, "iso": line["timestamp"], "mono": self.clock(),
                }
            whole = store[key]
            line["content"] = _tail_on_boundary(whole, THINKING_PREVIEW_TAIL_CHARS)
            line["data"] = {
                "block": "thinking",
                "chars": len(whole),
                "startedAt": self._open_thought["iso"],
            }
        return [line]

    def map_final_assistant_message(
        self, final_text: str, *, stop_reason: str = "stop",
        cut_forms: Optional[tuple] = None,
    ) -> list[dict[str, Any]]:
        """The turn's LAST assistant line: the text not written yet, real
        usage (never 0/0) and the mapper's model=None.

        - No text segment was written this turn: exactly the line Review
          #465 Option b defined — ``final_text`` (the caller's; the chat
          passes its verdict's ``reply``), and no line at all when it is
          empty and the turn wrote nothing else (a turn that produced
          nothing leaves no empty bubble).
        - Segments were written (D7): the text is the mapper's OWN unflushed
          rest, never a prefix computation on the caller's string (S1).
          ``cut_forms`` (the chat's verdict, ``ChatTurnVerdict.cut_forms``)
          cuts omp's error line off the END of that rest
          (``model_errors.reply_without_omp_line``); ``()`` hides the whole
          rest. Segments already written stay — they are on screen.
        - When nothing is left, the line still comes with ``content: []``
          so that stopReason + usage end the turn (the parser yields only
          ``usage`` for it: no bubble). The same holds for a turn whose only
          output was thinking: the thought is its own line, never glued into
          this one (F1).

        ``stop_reason`` stamps `message.stopReason` — the backend's
        ``omp_chat.transcript_suggests_turn_ended`` probe reads it to decide
        working vs idle; ACP's `end_turn` maps to omp's `stop`. Only the
        terminal reasons land here: a cancelled/error turn keeps the probe
        conservative via the non-terminal default the caller passes.
        """
        with self._lock:
            thought = self._flush_thought()
            # The turn's open text ends here either way: nothing of it may
            # come back later (a card's flush, the next turn's notice).
            rest = self._take_text()
            if self._turn_flushed_any:
                text = rest if cut_forms is None else _cut_rest(rest, cut_forms)
            else:
                text = str(final_text or "")
            if text.strip():
                content: list[dict[str, Any]] = [{"type": "text", "text": text}]
            elif self._turn_flushed_any or self._turn_thought_any:
                content = []
            else:
                return thought
        entry: dict[str, Any] = {
            "type": "message",
            "id": self._next_id(),
            "parentId": None,
            "timestamp": _now_iso(),
            "message": {
                "role": "assistant",
                "content": content,
                "model": None,
                "stopReason": stop_reason if stop_reason == "stop" else None,
            },
        }
        usage = self._usage_block()
        if usage is not None:
            entry["message"]["usage"] = usage
        return thought + [entry]

    def _tool_seed(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return []
        kind = str(update.get("kind") or "tool")
        title = self._tool_title_of(update)
        raw_input = update.get("rawInput")
        arguments = dict(raw_input) if isinstance(raw_input, dict) else {}
        arguments.setdefault("i", title)
        locations = _clean_locations(update.get("locations"))
        self._tools[tool_call_id] = {
            "name": self._tool_name(tool_call_id, kind),
            "title": title,
            "arguments": arguments,
            "locations": locations,
        }
        block: dict[str, Any] = {
            "type": "toolCall",
            "id": tool_call_id,
            "name": self._tools[tool_call_id]["name"],
            "arguments": arguments,
        }
        if locations:
            block["locations"] = list(locations)
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "assistant",
                    "content": [block],
                    "model": None,
                },
            }
        ]

    def _tool_name(self, tool_call_id: str, kind: str) -> str:
        """The kinds that name exactly one tool map directly; any other kind
        asks ``tool_name_resolver`` for the real name and keeps the kind when
        it has no answer. Never raises."""
        if kind in _KIND_TO_NAME:
            return _KIND_TO_NAME[kind]
        if self.tool_name_resolver is not None:
            try:
                name = self.tool_name_resolver(tool_call_id)
            except Exception:  # noqa: BLE001 — a lookup must not break the turn
                name = None
            if isinstance(name, str) and _TOOL_NAME_RE.match(name):
                return name
        return kind

    def _tool_update(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return []
        status = str(update.get("status") or "").lower()
        tool = self._tools.get(tool_call_id)
        # An update for an id we never seeded: without title/name we know
        # too little to render a card — show nothing rather than invent
        # (fail closed). Same for live output: no card, nothing to stream into.
        if tool is None:
            return []
        for path in _clean_locations(update.get("locations")):
            if path not in tool["locations"] and len(tool["locations"]) < _MAX_LOCATIONS:
                tool["locations"].append(path)
        if status not in _TERMINAL_STATUSES:
            # in_progress/pending: the running command's output so far.
            return self._tool_progress(tool_call_id, update)
        # Settled: a withheld snapshot is stale now — the result replaces it.
        self._live.pop(tool_call_id, None)
        raw_output = update.get("rawOutput")
        # rawOutput is {"content": [blocks], "details": {...}} in the fixture;
        # tolerate a bare block list too.
        if isinstance(raw_output, dict):
            raw_output = raw_output.get("content")
        output_text = _trunc(strip_ansi(_content_text(raw_output)))
        details: dict[str, Any] = {"title": tool["title"], "status": status}
        diffs = _clean_diffs(update.get("content"))
        if diffs:
            details["diffs"] = diffs
        if tool["locations"]:
            details["locations"] = list(tool["locations"])
        entry: dict[str, Any] = {
            "type": "message",
            "id": self._next_id(),
            "parentId": None,
            "timestamp": _now_iso(),
            "message": {
                "role": "toolResult",
                "toolCallId": tool_call_id,
                "toolName": tool["name"],
                "content": [{"type": "text", "text": output_text}],
                "details": details,
                "isError": status in ("failed", "error"),
            },
        }
        # P2: pictures as references + an in-memory payload that ``dump``
        # writes to disk (and strips) before the line is serialised.
        images = _extract_images(update)
        if images:
            details["images"] = [
                {"sha256": sha, "mimeType": mime, "bytes": len(raw)} for sha, mime, raw in images
            ]
            entry["_media"] = [
                {"name": f"{sha}.{_IMAGE_EXT[mime]}", "data": raw} for sha, mime, raw in images
            ]
        return [entry]

    def _tool_progress(self, tool_call_id: str, update: dict[str, Any]) -> list[dict[str, Any]]:
        """One in_progress snapshot -> 0..1 volatile ``acp-tool-output`` line
        (throttled per call; see the module docstring, LIVE TOOL OUTPUT)."""
        raw_output = update.get("rawOutput")
        if isinstance(raw_output, dict):
            raw_output = raw_output.get("content")
        text = _tail(strip_ansi(_content_text(raw_output)).rstrip())
        if not text.strip():
            return []
        live = self._live.setdefault(tool_call_id, {"text": None, "at": None, "pending": None})
        if text == live["text"]:
            live["pending"] = None
            return []
        now = self.clock()
        if live["at"] is not None and now - live["at"] < _TOOL_OUTPUT_INTERVAL_S:
            live["pending"] = text
            return []
        return [self._emit_tool_output(tool_call_id, text, now)]

    def _flush_due_tool_output(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not self._live:
            return out
        now = self.clock()
        for tool_call_id, live in list(self._live.items()):
            pending = live.get("pending")
            if pending is None or live["at"] is None:
                continue
            if now - live["at"] >= _TOOL_OUTPUT_INTERVAL_S:
                out.append(self._emit_tool_output(tool_call_id, pending, now))
        return out

    def _emit_tool_output(self, tool_call_id: str, text: str, now: float) -> dict[str, Any]:
        live = self._live.setdefault(tool_call_id, {"text": None, "at": None, "pending": None})
        live.update(text=text, at=now, pending=None)
        return {
            "type": "custom_message",
            "customType": TOOL_OUTPUT_CUSTOM_TYPE,
            "content": text,
            "display": True,
            "attribution": "agent",
            "data": {"toolCallId": tool_call_id},
            "id": self._next_id(),
            "parentId": None,
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    @staticmethod
    def dump(
        entries: list[dict[str, Any]],
        media_writer: Optional[Callable[[str, bytes], bool]] = None,
    ) -> list[str]:
        """Entries -> JSONL strings. A toolResult's ``_media`` payload (P2)
        is handed to ``media_writer(name, bytes)`` first and never
        serialised; a picture the writer did not confirm loses its
        reference in ``details.images`` — no reference without a file (a
        dangling one would render a broken image). No writer -> no
        pictures at all. Never raises."""
        out: list[str] = []
        for entry in entries:
            media = entry.get("_media") if isinstance(entry, dict) else None
            if media:
                entry = _settle_media(entry, media, media_writer)
            try:
                out.append(json.dumps(entry, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
        return out


def _settle_media(
    entry: dict[str, Any],
    media: list[dict[str, Any]],
    media_writer: Optional[Callable[[str, bytes], bool]],
) -> dict[str, Any]:
    """A copy of ``entry`` without ``_media`` whose ``details.images`` keeps
    only the pictures ``media_writer`` actually stored."""
    written: set[str] = set()
    if media_writer is not None:
        for item in media:
            name = item.get("name") if isinstance(item, dict) else None
            data = item.get("data") if isinstance(item, dict) else None
            if not isinstance(name, str) or not isinstance(data, (bytes, bytearray)):
                continue
            try:
                if media_writer(name, bytes(data)):
                    written.add(name.split(".", 1)[0])
            except Exception:  # noqa: BLE001 — a picture never kills the line
                continue
    clean = {k: v for k, v in entry.items() if k != "_media"}
    message = dict(clean.get("message") or {})
    details = dict(message.get("details") or {})
    kept = [img for img in details.get("images") or [] if img.get("sha256") in written]
    if kept:
        details["images"] = kept
    else:
        details.pop("images", None)
    message["details"] = details
    clean["message"] = message
    return clean


class ToolOutputPump:
    """Trailing flush for live tool output (chat package P1).

    The mapper withholds snapshots that arrive faster than
    ``_TOOL_OUTPUT_INTERVAL_S`` and normally releases the newest one with
    the next update. A command that prints a burst and then goes quiet
    sends no next update — without this pump the phone would sit on the
    burst's first line until the command ended. A daemon thread asks the
    mapper every interval and hands anything due to ``emit`` (the preview
    sink). Failure-proof like the sinks: an exception is swallowed, a dead
    pump only means a slightly staler live view.
    """

    def __init__(
        self,
        mapper: "ACPEventMapper",
        emit: Callable[[list[dict[str, Any]]], None],
        interval: float = _TOOL_OUTPUT_INTERVAL_S,
    ):
        self._mapper = mapper
        self._emit = emit
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "ToolOutputPump":
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="acp-tool-output-pump", daemon=True
            )
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                entries = self._mapper.flush_tool_output()
                if entries:
                    self._emit(entries)
            except Exception:  # noqa: BLE001 — never kill the turn
                pass

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def _encode_cwd(path: str) -> str:
    """omp's own session-folder convention: leading/trailing double dash,
    inner slashes -> single dash (``/workspace/foo`` -> ``--workspace-foo--``,
    ``/workspace`` -> ``--workspace--``). Verified against the live tree."""
    name = path.strip("/") or "root"
    return "--" + name.replace("/", "-") + "--"


def _profile_agent_dir() -> Optional[str]:
    """``$OMP_HOME/profiles/<OMP_PROFILE>/agent`` when a profile is set, else None.

    With OMP_PROFILE set, omp keeps its agent dir (sessions, models.yml, DBs)
    under the profile — regardless of PI_CODING_AGENT_DIR, which the image
    still points at the profile-less ``$OMP_HOME/agent``. The compose bind
    mount (``~/.mc/agents/<slug>/omp-sessions``) targets the PROFILE tree, so
    only files written there reach the backend. Live finding 14.09.2026:
    acp-chat-state.json landed in the unmounted tree and the effort switch
    answered 409 input_not_supported although the daemon was healthy."""
    profile = os.environ.get("OMP_PROFILE")
    home = os.environ.get("OMP_HOME")
    if not profile or not home:
        return None
    return str(Path(home) / "profiles" / profile / "agent")


def session_dir(
    agent_dir_env: str | None = None,
    cwd: str | None = None,
    sessions_root: str | Path | None = None,
) -> Optional[Path]:
    """The omp sessions directory for THIS bridge container, fail-closed.

    Layout: ``<agent dir>/sessions/<encoded-cwd>/``, where ``<agent dir>`` is
    resolved in this order: explicit ``agent_dir_env`` → the profile tree
    ``$OMP_HOME/profiles/$OMP_PROFILE/agent`` (omp ignores
    ``PI_CODING_AGENT_DIR`` once ``OMP_PROFILE`` is set, and the container
    image sets it) → ``$PI_CODING_AGENT_DIR``. The profile tree is the root
    the backend's omp_chat.resolve_transcript_dir reads through the
    ``~/.mc/agents/<slug>/omp-sessions`` bind mount; writing under the bare
    ``$PI_CODING_AGENT_DIR`` lands OUTSIDE that mount and is invisible. The cwd encoded is the
    ACP-pinned one (OMP_ACP_CWD), so an ACP run lands in its own folder
    instead of polluting a native session's directory.

    ``sessions_root`` names that root DIRECTLY, for a daemon that runs on the
    HOST and has no such mount to hide behind (the Hermes chat daemon, see
    scripts/hermes_acp_chat.py): there the backend reads
    ``~/.mc/agents/hermes/omp-sessions`` itself, and no value of
    PI_CODING_AGENT_DIR can produce that name — the mount is what renames
    ``sessions`` to ``omp-sessions``, and on the host there is no mount.
    """
    if sessions_root is not None:
        root = Path(sessions_root)
    else:
        agent_dir = agent_dir_env or _profile_agent_dir() or os.environ.get("PI_CODING_AGENT_DIR")
        if not agent_dir:
            return None
        root = Path(agent_dir) / "sessions"
    workdir = (
        cwd
        or os.environ.get("OMP_ACP_CWD")
        or os.environ.get("OMP_DEFAULT_CWD")
        or "/workspace"
    )
    path = root / _encode_cwd(workdir)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def mc_sink_dir(folder: Path) -> Path:
    """``<folder>/.mc`` (``MC_SINK_DIRNAME``) — created, then returned.

    Raises OSError when it cannot be created (a file in its place, a
    read-only tree) or is not a real folder (a symlink — the session folder
    is the agent's own mount, and MC never writes where a link points); the
    sinks catch that and degrade to a no-op, like an unwritable sessions
    tree."""
    target = Path(folder) / MC_SINK_DIRNAME
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise OSError(errno.ENOTDIR, "not a real folder", str(target))
    return target


def open_regular(path: Path) -> Optional[Any]:
    """``path`` opened for reading as text when it is a regular file, else
    None. Never blocks: a FIFO named like a session would hang a plain
    ``open()`` until a writer comes (non-blocking open, then a type check on
    the open file). The caller closes the handle. Never raises."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            if hasattr(os, "set_blocking"):
                os.set_blocking(fd, True)  # from here on an ordinary handle
            return os.fdopen(fd, "r", encoding="utf-8", errors="replace")
    except OSError:
        pass
    os.close(fd)
    return None


def session_header(path: Path) -> Optional[dict]:
    """The session header of a JSONL file — line 1, or line 2 when line 1 is
    a ``{"type": "title"}`` line (omp's own rule) — or None when the file has
    none (not a session for omp either) or is no regular file. Never raises,
    never blocks."""
    fh = open_regular(path)
    if fh is None:
        return None
    try:
        with fh:
            first, second = fh.readline(_HEADER_BYTES), fh.readline(_HEADER_BYTES)
    except (OSError, ValueError):
        return None
    lines = []
    for raw in (first, second):
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        lines.append(value if isinstance(value, dict) else None)
    header = lines[1] if lines[0] is not None and lines[0].get("type") == "title" else lines[0]
    if header is None or header.get("type") != "session" or not isinstance(header.get("id"), str):
        return None
    return header


def is_mc_header(header: Optional[dict]) -> bool:
    """A session header MC wrote: it names the bridge (``"bridge": "acp"``,
    written by ``ChatEventSink``). omp never writes that key (live
    20.10.2026: 266 copies with it, 468 omp files without)."""
    return header is not None and header.get("bridge") == "acp"


def is_mc_sink(path: Path) -> bool:
    """MC's display copy: a regular file whose session header names the
    bridge (``is_mc_header``)."""
    return is_mc_header(session_header(path))


def find_sink(folder: Path, session_id: str) -> Optional[Path]:
    """MC's copy of session ``session_id`` in session folder ``folder``.

    ``<folder>/.mc/`` first, then the old place next to omp's file (a tree
    ``sink_migration`` has not moved yet). Only a file whose header names
    the bridge counts, so omp's own file is never returned. Several copies
    of one id: the newest by modification time. The id is compared as text
    (never a glob pattern). The one lookup for P4's list, resume and fork
    (chat package Stage 1), so they never pick omp's file by mistake."""
    if not session_id:
        return None
    suffix = f"_{session_id}.jsonl"
    for place in (Path(folder) / MC_SINK_DIRNAME, Path(folder)):
        try:
            names = [p for p in place.iterdir()
                     if p.name.endswith(suffix) and not p.name.startswith(".")]
        except OSError:
            continue
        dated = []
        for path in names:
            if not is_mc_sink(path):
                continue
            try:
                dated.append((path.stat().st_mtime, str(path), path))
            except OSError:
                continue
        if dated:
            return max(dated)[2]
    return None


class ChatEventSink:
    """Appends mapped events as JSONL into the omp sessions tree, one file per
    ACP session: ``<session folder>/.mc/<ts>_<sessionId>.jsonl``
    (``MC_SINK_DIRNAME`` explains why not next to omp's own file). The
    header is the one the bridge always wrote — every backend reader of it
    (``bridge``, ``taskId``, ``cwd``) stays the same.

    - append-only, whole lines, one ``write`` call per batch (the tailer
      polls at 0.3 s and reads byte-wise — partial multi-byte UTF-8 across
      polls can't happen because each line is flushed complete)
    - degrades to a no-op when the directory/file is unavailable — never
      raises into the ACP turn
    """

    def __init__(
        self, directory: Optional[Path], session_id: str, task_id: Optional[str] = None
    ):
        """``task_id``: set for a TASK run (bridge serve loop), ``None`` for
        the chat daemon. It lands in the session header as ``taskId``: every
        attempt of a task (continue nudge, reflection retry) is a NEW ACP
        session without the dispatch prompt, and the backend's todo ->
        checklist sync finds the task's plan by this tag — not by "newest
        session", which is the chat (live 12.10.2026)."""
        self._dir = directory
        self._session_id = session_id or "acp-session"
        self._path: Optional[Path] = None
        if directory is not None:
            ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
            header = {
                "type": "session",
                "version": 3,
                "id": self._session_id,
                "timestamp": _now_iso(),
                "cwd": os.environ.get("OMP_ACP_CWD") or "",
                "bridge": "acp",
            }
            if task_id:
                header["taskId"] = task_id
            try:
                candidate = mc_sink_dir(directory) / f"{ts}_{self._session_id}.jsonl"
                with open(candidate, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(header, ensure_ascii=False) + "\n")
                self._path = candidate
            except OSError:
                self._path = None

    @classmethod
    def appending(cls, path: Path, session_id: str) -> "ChatEventSink":
        """Bind a sink to an EXISTING transcript file — no new session header.

        The chat daemon's `session/load` path (chat over ACP): the ACP
        session survives a restart, so its history must keep growing in the
        SAME file. A fresh ChatEventSink would open a second file for one
        session and the chat view's rollover would split the history.
        Fail-closed: an unwritable/absent file degrades to a no-op sink.
        """
        sink = cls(None, session_id)
        try:
            with open(path, "a", encoding="utf-8"):
                pass
            sink._path = Path(path)
        except OSError:
            sink._path = None
        return sink

    #: How many pictures ``media/`` keeps (newest by mtime). The backend
    #: copies each one into its own store the first time a chat shows it,
    #: so this folder is a transit area, not the archive — but a picture
    #: pruned before anyone opened the chat stays unavailable.
    _MEDIA_KEEP = 200

    def write_media(self, name: str, data: bytes) -> bool:
        """One tool picture into ``<transcript dir>/media/<name>`` (chat
        package P2) — ``<session folder>/.mc/media/`` for a copy in ``.mc/``,
        the old ``<session folder>/media/`` for a copy a not yet moved tree
        resumed (``appending``): the backend looks next to the transcript.
        ``name`` must be ``<sha256>.<png|jpg|webp|gif>`` — the
        hash the mapper computed, so the same picture is one file. Atomic
        (temp file + rename), idempotent, never raises: False = not stored
        (the caller then drops the reference)."""
        if self._path is None or not isinstance(name, str) or not _MEDIA_NAME_RE.match(name):
            return False
        try:
            mdir = self._path.parent / "media"
            mdir.mkdir(exist_ok=True)
            target = mdir / name
            if target.is_file():
                return True
            tmp = mdir / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
            self._prune_media(mdir)
            return True
        except OSError:
            return False

    @classmethod
    def _prune_media(cls, mdir: Path) -> None:
        dated: list[tuple[float, Path]] = []
        try:
            for candidate in mdir.iterdir():
                if _MEDIA_NAME_RE.match(candidate.name):
                    try:
                        dated.append((candidate.stat().st_mtime, candidate))
                    except OSError:
                        continue
        except OSError:
            return
        dated.sort(key=lambda item: item[0], reverse=True)
        for _mtime, stale in dated[cls._MEDIA_KEEP:]:
            try:
                stale.unlink()
            except OSError:
                pass

    @property
    def session_id(self) -> str:
        """The sink's sessionId (Review #465 Blocker 1: bridge's sink()
        compares this against its holder to decide reuse — the class only
        ever had `_session_id`, so every second call raised AttributeError
        and the chat stalled after the first transcript line)."""
        return self._session_id

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def write(self, lines: list[str]) -> None:
        if not lines or self._path is None:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
        except OSError:
            # Disk gone/ro — keep the run alive; the chat stays empty
            # (honest) instead of the turn dying.
            self._path = None


class PreviewEventSink:
    """The VOLATILE preview channel (Folge-PR zu #471): appends the mapper's
    preview flushes to
    ``<session_dir>/.mc/previews/<ts>_<sessionId>_<uniq>.jsonl`` (S1-R3:
    always next to the transcript copy in use, see ``beside`` — under
    ``.mc/`` for a new copy, the old ``previews/`` for an old copy resumed
    in place; the move never touches ``previews/``, it only ever held a few
    volatile files)
    — a SIBLING of the transcript file, in a subdirectory
    ``find_active_session``/``_SESSION_GLOBS`` never scan, so the backend's
    history/rollover logic cannot mistake preview lines for transcript
    entries. The tailer reads this file via omp_chat.preview_channel and
    broadcasts each line as a ``kind: "preview", source: "acp"`` SSE frame.

    Same failure discipline as ChatEventSink: append-only, whole lines,
    degrades to a no-op — a dead preview channel never kills the run.

    ``session_id`` mirrors ChatEventSink: bridge's holder compares it to
    decide reuse when the REAL ACP sessionId arrives mid-run.

    Review #473 N1: one file is created per turn and NOTHING ever deleted
    them — an omp-agent left running for days would grow ``previews/``
    without bound, and the tailer's ``preview_channel`` globs the whole
    directory every tick to find the newest file, so the glob itself gets
    linearly more expensive too. On construction, before writing the new
    file, prune the directory down to the 3 newest ``*.jsonl`` (mtime-
    sorted) — the tailer only ever wants the newest one anyway, 3 is slack
    for a resolve mid-rollover. A delete failure (permissions, a racing
    reader) is swallowed: the preview channel must never crash a turn over
    disk hygiene, an unpruned file is just a few KB.
    """

    #: How many preview files survive a prune (Review #473 N1). The tailer
    #: (``omp_chat.preview_channel``) only reads the single newest one; a
    #: few extra are slack for a reader resolving mid-rollover, not a
    #: retention policy.
    _KEEP = 3

    def __init__(
        self,
        directory: Optional[Path],
        session_id: str,
        *,
        previews_dir: Optional[Path] = None,
    ):
        """``directory``: the session folder — the file goes to
        ``<directory>/.mc/previews/``. ``previews_dir``: the exact folder
        instead (``beside``)."""
        self._session_id = session_id or "acp-session"
        self._path: Optional[Path] = None
        if directory is not None or previews_dir is not None:
            try:
                pdir = (Path(previews_dir) if previews_dir is not None
                        else mc_sink_dir(directory) / "previews")
                pdir.mkdir(parents=True, exist_ok=True)
                self._prune(pdir)
                ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
                uniq = uuid.uuid4().hex[:6]
                self._path = pdir / f"{ts}_{self._session_id}_{uniq}.jsonl"
            except OSError:
                self._path = None

    @classmethod
    def beside(cls, transcript: Optional[Path], session_id: str) -> "PreviewEventSink":
        """The preview channel next to the transcript copy IN USE:
        ``<transcript folder>/previews/`` — where the backend looks
        (``omp_chat.preview_channel``). For a new copy that is
        ``.mc/previews/``; for an old copy the daemon resumed in place
        (``ChatEventSink.appending``, the move did not run) it is the old
        ``<session folder>/previews/``. No transcript: a no-op sink."""
        if transcript is None:
            return cls(None, session_id)
        return cls(None, session_id, previews_dir=Path(transcript).parent / "previews")

    @staticmethod
    def _prune(pdir: Path) -> None:
        """Keep only the newest ``_KEEP`` preview files in ``pdir``. Fail-
        closed: any error listing/stat-ing/deleting is swallowed — a full
        ``previews/`` dir is a nuisance, a crashed turn is not (Review #473
        N1)."""
        dated: list[tuple[float, Path]] = []
        try:
            for candidate in pdir.glob("*.jsonl"):
                try:
                    dated.append((candidate.stat().st_mtime, candidate))
                except OSError:
                    continue
        except OSError:
            return
        dated.sort(key=lambda item: item[0], reverse=True)
        for _mtime, stale in dated[PreviewEventSink._KEEP :]:
            try:
                stale.unlink()
            except OSError:
                pass

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def write(self, lines: list[str]) -> None:
        if not lines or self._path is None:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(line + "\n")
        except OSError:
            # Disk gone/ro — the preview dies quietly; the run goes on.
            self._path = None
