#!/usr/bin/env python3
"""
acp_chat_events.py — pure mapping: ACP `session/update` params -> omp-format
transcript lines, written as JSONL into the omp sessions tree (Subtask 3/4).

WHY THIS LIVES IN THE BRIDGE: the ACP driver path (OMP_DRIVER=acp, run_acp_once)
never touches the omp TUI transcript the native path writes — the headless
`omp acp` child streams its events over JSON-RPC and they evaporate once the
turn ends. The backend chat view (services/transcript_chat.py
ChatTailerManager + services/omp_chat.py) reads exactly ONE thing: the JSONL
session files under `$PI_CODING_AGENT_DIR/sessions/<encoded-cwd>/`. So the
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
    that resolves omp's parallel tool calls. Interim in_progress updates are
    folded silently: the card exists, partial output would just overwrite
    itself repeatedly.

  * usage_update (size/used — context window, NOT tokens) is remembered and
    stamped onto assistant lines as message.usage.contextWindow/usedTokens;
    the prompt RESULT's usage (inputTokens/outputTokens, from
    session/prompt reply — same fields omp native writes) becomes a proper
    usage block on the final assistant flush.

  * session/request_permission -> a custom_message line (customType
    "acp-permission"), which omp_chat renders as a teammate line: the
    operator sees WHAT asked. The decision follows as its own
    acp-permission-decision line. The interactive ApprovalCard flow stays
    native-path-only — ACP permissions are decided by bridge policy/mc ask,
    and inventing an interactive prompt for a decision already taken would
    lie about who decides.

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

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# omp entry ids are 8 hex chars; ours carry a distinct prefix so a mixed
# history (native + bridged lines in one file) stays attributable.
_ID_PREFIX = "acp"

# Result text truncation mirrors omp_chat._RESULT_TRUNCATE_LEN (same schema,
# same reason: a 500 KB tool output must not land in the chat view whole).
_RESULT_TRUNCATE_LEN = 4000

_TITLE_TRUNCATE_LEN = 200

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


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


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


@dataclass
class ACPEventMapper:
    """Accumulates one ACP run's events into omp-format transcript line dicts.

    One instance per bridge run (= one ACP session). Consumed by
    bridge.run_acp_once via a ChatEventSink; also usable standalone in tests.
    """

    seq: int = 0
    # agent_message_chunk accumulation: messageId -> full text so far.
    _text_by_message: dict[str, str] = field(default_factory=dict)
    _thought_by_message: dict[str, str] = field(default_factory=dict)
    # toolCallId -> {"name", "title", "arguments"} as seeded by tool_call.
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Latest usage_update (context window size / used tokens), if any.
    _usage_window: Optional[dict[str, int]] = None
    # Token usage from the session/prompt result (set via set_prompt_usage).
    _prompt_usage: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------
    # mapping
    # ------------------------------------------------------------------

    def map_update(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """One `session/update` params dict -> 0..n omp-format transcript
        line dicts. Never raises; unknown shapes yield []."""
        try:
            update = params.get("update") or {}
            if not isinstance(update, dict):
                return []
            su = update.get("sessionUpdate")
            if su == "agent_message_chunk":
                return self._chunk(update, self._text_by_message, "text")
            if su == "agent_thought_chunk":
                return self._chunk(update, self._thought_by_message, "thinking")
            if su == "tool_call":
                return self._tool_seed(update)
            if su == "tool_call_update":
                return self._tool_update(update)
            if su == "usage_update":
                size, used = update.get("size"), update.get("used")
                if isinstance(size, int) and size > 0 and isinstance(used, int):
                    self._usage_window = {"size": size, "used": used}
                return []
            # config_option_update / available_commands_update /
            # session_info_update / anything unknown — no chat value.
            return []
        except Exception:  # noqa: BLE001 — a broken update must not kill the run
            return []

    def set_prompt_usage(self, usage: Optional[dict[str, Any]]) -> None:
        """Record the session/prompt reply's usage (inputTokens/outputTokens
        etc.) for stamping onto subsequent assistant flushes."""
        if isinstance(usage, dict) and usage:
            self._prompt_usage = usage

    def map_permission_request(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """`session/request_permission` -> a visible custom_message line
        (teammate role in the chat view): the operator sees WHAT asked and
        which choices existed."""
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
        return [
            {
                "type": "custom_message",
                "customType": "acp-permission",
                "content": question,
                "display": True,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def map_permission_outcome(self, params: dict[str, Any], choice: str) -> list[dict[str, Any]]:
        """The decision taken on a permission request, as its own line."""
        tool = params.get("toolCall") or {}
        title = self._tool_title_of(tool)
        return [
            {
                "type": "custom_message",
                "customType": "acp-permission-decision",
                "content": f"Freigabe entschieden: {choice} — {title}",
                "display": True,
                "attribution": "agent",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
            }
        ]

    def map_user_prompt(self, prompt_text: str) -> list[dict[str, Any]]:
        """The user turn itself -> a user message line, so the chat view shows
        what the bridge asked (the native transcript carries the same via the
        TUI's input echo)."""
        text = str(prompt_text or "").strip()
        if not text:
            return []
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": text[:8000]}],
                    "attribution": "user",
                },
            }
        ]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
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
        -> None (no invented numbers)."""
        if not self._prompt_usage and not self._usage_window:
            return None
        usage = self._prompt_usage or {}
        window = self._usage_window or {}
        return {
            "input": int(usage.get("inputTokens") or 0),
            "cacheRead": 0,
            "cacheWrite": 0,
            "output": int(usage.get("outputTokens") or 0),
            "contextWindow": window.get("size"),
            "usedTokens": window.get("used"),
        }

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
        entry: dict[str, Any] = {
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
        usage = self._usage_block()
        if usage is not None:
            entry["message"]["usage"] = usage
        return [entry]

    def _tool_seed(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return []
        kind = str(update.get("kind") or "tool")
        title = self._tool_title_of(update)
        raw_input = update.get("rawInput")
        arguments = dict(raw_input) if isinstance(raw_input, dict) else {}
        arguments.setdefault("i", title)
        self._tools[tool_call_id] = {
            "name": _KIND_TO_NAME.get(kind, kind),
            "title": title,
            "arguments": arguments,
        }
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": tool_call_id,
                            "name": self._tools[tool_call_id]["name"],
                            "arguments": arguments,
                        }
                    ],
                    "model": None,
                },
            }
        ]

    def _tool_update(self, update: dict[str, Any]) -> list[dict[str, Any]]:
        tool_call_id = update.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return []
        status = str(update.get("status") or "").lower()
        if status not in _TERMINAL_STATUSES:
            # in_progress/pending: the card already exists from tool_call;
            # partial output would overwrite itself repeatedly — stay quiet.
            return []
        tool = self._tools.get(tool_call_id)
        # A settled update for an id we never seeded: without title/name we
        # know too little to render a card — show nothing rather than invent
        # (fail closed).
        if tool is None:
            return []
        raw_output = update.get("rawOutput")
        # rawOutput is {"content": [blocks], "details": {...}} in the fixture;
        # tolerate a bare block list too.
        if isinstance(raw_output, dict):
            raw_output = raw_output.get("content")
        output_text = _trunc(_content_text(raw_output))
        return [
            {
                "type": "message",
                "id": self._next_id(),
                "parentId": None,
                "timestamp": _now_iso(),
                "message": {
                    "role": "toolResult",
                    "toolCallId": tool_call_id,
                    "toolName": tool["name"],
                    "content": [{"type": "text", "text": output_text}],
                    "details": {"title": tool["title"], "status": status},
                    "isError": status in ("failed", "error"),
                },
            }
        ]

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    @staticmethod
    def dump(entries: list[dict[str, Any]]) -> list[str]:
        out: list[str] = []
        for entry in entries:
            try:
                out.append(json.dumps(entry, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
        return out


def _encode_cwd(path: str) -> str:
    """omp's own session-folder convention: leading/trailing double dash,
    inner slashes -> single dash (``/workspace/foo`` -> ``--workspace-foo--``,
    ``/workspace`` -> ``--workspace--``). Verified against the live tree."""
    name = path.strip("/") or "root"
    return "--" + name.replace("/", "-") + "--"


def session_dir(
    agent_dir_env: str | None = None, cwd: str | None = None
) -> Optional[Path]:
    """The omp sessions directory for THIS bridge container, fail-closed.

    Layout: ``$PI_CODING_AGENT_DIR/sessions/<encoded-cwd>/`` — the same root
    the backend's omp_chat.resolve_transcript_dir reads through the
    ``~/.mc/agents/<slug>/omp-sessions`` bind mount. The cwd encoded is the
    ACP-pinned one (OMP_ACP_CWD), so an ACP run lands in its own folder
    instead of polluting a native session's directory.
    """
    agent_dir = agent_dir_env or os.environ.get("PI_CODING_AGENT_DIR")
    if not agent_dir:
        return None
    workdir = (
        cwd
        or os.environ.get("OMP_ACP_CWD")
        or os.environ.get("OMP_DEFAULT_CWD")
        or "/workspace"
    )
    path = Path(agent_dir) / "sessions" / _encode_cwd(workdir)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


class ChatEventSink:
    """Appends mapped events as JSONL into the omp sessions tree, one file per
    ACP session (``<ts>_<sessionId>.jsonl``, the same shape omp writes so
    find_active_session ranks it like any native session).

    - append-only, whole lines, one ``write`` call per batch (the tailer
      polls at 0.3 s and reads byte-wise — partial multi-byte UTF-8 across
      polls can't happen because each line is flushed complete)
    - degrades to a no-op when the directory/file is unavailable — never
      raises into the ACP turn
    """

    def __init__(self, directory: Optional[Path], session_id: str):
        self._dir = directory
        self._session_id = session_id or "acp-session"
        self._path: Optional[Path] = None
        if directory is not None:
            ts = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime())
            candidate = directory / f"{ts}_{self._session_id}.jsonl"
            header = {
                "type": "session",
                "version": 3,
                "id": self._session_id,
                "timestamp": _now_iso(),
                "cwd": os.environ.get("OMP_ACP_CWD") or "",
                "bridge": "acp",
            }
            try:
                with open(candidate, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(header, ensure_ascii=False) + "\n")
                self._path = candidate
            except OSError:
                self._path = None

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
