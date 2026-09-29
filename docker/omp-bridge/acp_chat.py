#!/usr/bin/env python3
"""
acp_chat.py — long-lived chat daemon for the OMP_DRIVER=acp path.

WHY: tasks already run over ACP (`bridge.run_acp_once`, one process per
attempt), but the Sessions chat still typed into the native TUI in tmux
window 0 — two brains, and every composer control (effort chip, /model, Stop)
was a key press against a console an ACP agent should not need. This daemon
gives the chat its OWN long-lived ACP session: one `omp acp` child, ONE ACP
session that survives restarts via `session/load`, one turn at a time.

Spec: docs/specs/chat-over-acp.md. Contract in one picture:

    acp_chat_ctl.py <op>  ──unix socket──►  ChatSocketServer
                                                 │
                                                 ▼
                                            ChatSession
                                              │      │
                          transcript *.jsonl ◄┘      └─► previews/*.jsonl
                          (what the backend chat reader tails)

Everything the operator must SEE is a transcript line, written through the
SAME mapper the task path uses (`acp_chat_events.ACPEventMapper`): the user
turn, the streamed preview, exactly ONE final assistant line — and failures
as `chat_error` lines, because an error only the log knows about is an error
the operator debugs blind.

Run: `python3 acp_chat.py --serve [--socket PATH]` (tmux window 3).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import acp_chat_events  # noqa: E402
import acp_client  # noqa: E402

logger = logging.getLogger("acp_chat")

#: Provider/quota failures worth their own code — the operator's fix is a key
#: or a budget, not a retry, so the chat must say so instead of "rpc_error".
_PROVIDER_ERROR_RE = re.compile(
    r"\b(401|403|429)\b|unauthor|forbidden|quota|insufficient|rate.?limit|credit",
    re.IGNORECASE,
)

#: Preview flush throttle, same numbers as bridge.run_acp_once: one line per
#: chunk is pointless I/O for a replace-me slot.
_PREVIEW_GROWTH_CHARS = 200
_PREVIEW_INTERVAL_S = 0.25

_STATE_VERSION = 1
_STATE_FILENAME = "acp-chat-state.json"
_PERSIST_FILENAME = "acp-chat.json"
_DEFAULT_SOCKET_NAME = "acp-chat.sock"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_socket_path() -> str:
    """`$OMP_HOME/acp-chat.sock`, with the image's OMP_HOME as fallback."""
    home = os.environ.get("OMP_HOME") or "/home/agent/.omp"
    return os.path.join(home, _DEFAULT_SOCKET_NAME)


def default_cwd() -> str:
    """The ACP working directory — the same one the task path pins, so chat
    and tasks land in ONE sessions folder (see acp_chat_events.session_dir)."""
    return (
        os.environ.get("OMP_ACP_CWD")
        or os.environ.get("OMP_DEFAULT_CWD")
        or "/workspace"
    )


class ChatSession:
    """One long-lived ACP chat session: prompts in, transcript lines out.

    `client_factory` is injected so tests drive an in-memory fake; production
    passes a factory building an `acp_client.ACPClient`. Thread-safe: the
    socket server calls prompt/cancel/config from connection threads while a
    turn runs in the worker thread.
    """

    def __init__(
        self,
        client_factory: Callable[[], Any],
        cwd: str,
        state_dir: Path | str,
        sessions_dir: Path | str,
        *,
        driver: str = "omp",
        permission_policy: Optional[str] = None,
        prompt_timeout: float = 3600.0,
        task_id: str = "",
        model: Optional[str] = None,
    ):
        self._client_factory = client_factory
        self._cwd = str(cwd)
        # The fully-qualified `<provider>/<model>` every session is pinned
        # to right after session/new AND session/load (see _pin_model).
        # None = the agent binary picks (Hermes: `hermes acp` has its own
        # model config, there is nothing to pin).
        self._model = (model or "").strip() or None
        self._state_dir = Path(state_dir)
        self._sessions_dir = Path(sessions_dir)
        self._driver = driver
        self._permission_policy = permission_policy or os.environ.get(
            "OMP_ACP_PERMISSIONS", "ask"
        )
        self._prompt_timeout = prompt_timeout
        self._task_id = task_id

        self._client: Any = None
        self._session_id: Optional[str] = None
        self._mapper = acp_chat_events.ACPEventMapper()
        # Entry ids are the tailer's dedup key. A fresh mapper starts at
        # seq=0, so a restarted daemon appending to the SAME transcript file
        # would re-issue acp00001… and the backend's seen-set would swallow
        # the new lines. A random start offset makes a collision across
        # restarts practically impossible.
        self._mapper.seq = random.randrange(1 << 28)
        self._sink: Optional[acp_chat_events.ChatEventSink] = None
        self._preview: Optional[acp_chat_events.PreviewEventSink] = None

        self._lock = threading.RLock()
        self._idle = threading.Event()
        self._idle.set()
        self._busy = False
        self._turn = 0
        self._config_options: list[dict] = []
        self._commands: list[dict] = []
        self._last_error: Optional[dict] = None
        self._worker: Optional[threading.Thread] = None
        self._closed = False

        self._full_text: list[str] = []
        self._throttle: dict = {"chars": -10 ** 9, "at": 0.0, "pending": None}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the child, handshake, and resume or open the ACP session."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        persisted = self._read_persisted()
        self._connect(persisted)
        self._write_state()

    def _connect(self, persisted: dict) -> None:
        client = self._client_factory()
        client.on_event(self._on_event)
        client.on_permission(self._on_permission)
        client._ensure_process()
        client.initialize()
        self._client = client

        stored_id = persisted.get("sessionId")
        stored_transcript = persisted.get("transcript")
        reset_detail = ""
        if stored_id and stored_transcript and Path(stored_transcript).exists():
            try:
                client.load_session(stored_id, self._cwd)
                self._session_id = stored_id
                self._mapper.set_session_id(stored_id)
                self._sink = acp_chat_events.ChatEventSink.appending(
                    Path(stored_transcript), stored_id
                )
            except Exception as exc:  # noqa: BLE001 — any load failure is a reset
                reset_detail = f"{type(exc).__name__}: {exc}"
        if self._session_id is None:
            self._session_id = client.new_session(self._cwd)
            self._mapper.set_session_id(self._session_id)
            self._sink = acp_chat_events.ChatEventSink(
                self._sessions_dir, self._session_id
            )
        self._preview = acp_chat_events.PreviewEventSink(
            self._sessions_dir, self._session_id
        )
        self._absorb_session_result(getattr(client, "last_session_result", None))
        self._pin_model()
        self._write_persisted()
        if reset_detail:
            # The operator loses the old history here — that is an EVENT,
            # not a log line (Boss: errors must be visible in the chat).
            self._emit_error(
                "session_reset",
                reset_detail,
                text="Previous chat session could not be loaded — "
                     "started a new one (history starts over).",
            )

    def _pin_model(self) -> None:
        """Pin the session to the MC model (twin of bridge.py's #483 fix).

        `omp acp` inherits the bridge environment; OPENAI_API_KEY activates
        omp's BUILT-IN openai provider, and without an explicit selector a
        fresh session opens on openai/gpt-5.5 with the shim key — live
        14.09.2026 the first chat turn after a container recreate answered
        "401 Incorrect API key provided: sk-noauth". launch-omp.sh always
        passes --model, bridge.py sets it per task session; the chat daemon
        has to do the same on every session/new and session/load. A refused
        pin is a chat-visible error, not a crash: the daemon still serves
        state/cancel, and the operator sees WHY in the chat."""
        if not self._model:
            return
        self.config("model", self._model)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — shutdown is best-effort
                pass

    # ------------------------------------------------------------------
    # control ops (the socket protocol)
    # ------------------------------------------------------------------

    def prompt(self, text: str) -> dict:
        """Start ONE turn. Returns immediately; the turn runs in a worker."""
        text = str(text or "")
        with self._lock:
            if self._busy:
                # Rejected, and the operator sees why — silently dropping a
                # typed message is the worst possible failure here.
                self._emit_error(
                    "busy", "a turn is already running",
                    text="A reply is already running — wait or press Stop.",
                )
                return {"ok": False, "error": "busy"}
            if self._closed or self._client is None:
                return {"ok": False, "error": "not_started"}
            self._turn += 1
            self._busy = True
            self._idle.clear()
            turn = self._turn
            self._worker = threading.Thread(
                target=self._run_turn, args=(text, turn),
                name="acp-chat-turn", daemon=True,
            )
        self._write_state()
        self._worker.start()
        return {"ok": True, "turn": turn}

    def cancel(self) -> dict:
        """`session/cancel` — a no-op when idle, never an error."""
        with self._lock:
            client, sid, busy = self._client, self._session_id, self._busy
        if client is not None and sid and busy:
            try:
                client.cancel(sid)
            except Exception as exc:  # noqa: BLE001 — cancel is best-effort
                logger.warning("cancel failed: %s", exc)
        return {"ok": True}

    def config(self, option_id: str, value: Any) -> dict:
        """Set one config option (`thinking`, `model`, `mode`, ...)."""
        with self._lock:
            client, sid = self._client, self._session_id
        if client is None or not sid:
            return {"ok": False, "error": "not_started"}
        try:
            client.set_config_option(sid, option_id, value)
        except Exception as exc:  # noqa: BLE001 — any failure is chat-visible
            detail = f"{exc}"
            code = "provider_error" if _PROVIDER_ERROR_RE.search(detail) else "rpc_error"
            self._emit_error(code, detail,
                             text=f"Einstellung {option_id}={value!r} abgelehnt: {detail}")
            return {"ok": False, "error": code, "detail": detail}
        with self._lock:
            for opt in self._config_options:
                if opt.get("id") == option_id:
                    opt["currentValue"] = value
            options = json.loads(json.dumps(self._config_options))
        self._write_state()
        return {"ok": True, "configOptions": options}

    def state(self) -> dict:
        """The `acp-chat-state.json` payload (also the `state` op's body)."""
        with self._lock:
            return {
                "version": _STATE_VERSION,
                "driver": self._driver,
                "sessionId": self._session_id,
                "busy": self._busy,
                "turn": self._turn,
                "transcript": str(self.transcript_path) if self.transcript_path else None,
                "configOptions": json.loads(json.dumps(self._config_options)),
                "commands": json.loads(json.dumps(self._commands)),
                "updatedAt": _now_iso(),
                "lastError": json.loads(json.dumps(self._last_error))
                if self._last_error else None,
            }

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no turn is running. True when idle, False on timeout."""
        return self._idle.wait(timeout)

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------

    @property
    def transcript_path(self) -> Optional[Path]:
        return self._sink.path if self._sink is not None else None

    @property
    def preview_path(self) -> Optional[Path]:
        return self._preview.path if self._preview is not None else None

    @property
    def state_file(self) -> Path:
        return self._sessions_dir / _STATE_FILENAME

    @property
    def persist_file(self) -> Path:
        return self._state_dir / _PERSIST_FILENAME

    # ------------------------------------------------------------------
    # the turn
    # ------------------------------------------------------------------

    def _run_turn(self, text: str, turn: int) -> None:
        result = None
        error_text = ""
        self._mapper.begin_turn()
        self._full_text = []
        self._throttle = {"chars": -10 ** 9, "at": time.monotonic(), "pending": None}
        try:
            with self._lock:
                client, sid = self._client, self._session_id
            self._emit_transcript(self._mapper.map_user_prompt(text))
            result = client.prompt(sid, text, timeout=self._prompt_timeout)
        except Exception as exc:  # noqa: BLE001 — a dead turn must still report
            error_text = f"{exc}"
        finally:
            # The LAST preview state always reaches the sink — the preview
            # slot must never rest on a stale snapshot.
            if self._throttle.get("pending") is not None:
                self._emit_preview([self._throttle["pending"]])
                self._throttle["pending"] = None

        with self._lock:
            closed = self._closed
        if closed:
            # close() ran while this turn was in flight (a `/restart` that
            # tore down the child mid-turn — the whole point of this branch,
            # see ChatDaemon.restart). client.prompt() only returned because
            # close() force-woke the pending RPC wait, NOT because the turn
            # actually finished — result/error_text describe nothing real.
            #
            # This session is retired: ChatDaemon already swapped in a
            # DIFFERENT ChatSession object (new client, new session id) that
            # may already be writing to shared paths (`sessions_dir` and
            # `state_dir` are workspace-scoped, not session-object-scoped —
            # a loaded session even APPENDS to the same transcript file).
            # Emitting a transcript line, calling `_restart_child()` (which
            # spawns yet another real child process nobody will ever close),
            # or writing acp-chat-state.json / the persist file here would
            # race the active session's own writes and can clobber its
            # sessionId with this dead session's — kill the turn silently and
            # stop touching anything.
            with self._lock:
                self._busy = False
            self._idle.set()
            return

        final_text = "".join(self._full_text)
        stop_reason = getattr(result, "stopReason", "") if result is not None else ""
        if result is not None:
            self._mapper.set_prompt_usage(getattr(result, "usage", None))
            self._emit_transcript(self._mapper.map_final_assistant_message(
                final_text, stop_reason="stop" if stop_reason == "end_turn" else "",
            ))

        if not self._client_alive():
            self._emit_error(
                "process_exit",
                error_text or f"child exited during turn {turn}",
                text="The ACP process died — reconnecting the session.",
            )
            self._restart_child()
        else:
            code, detail = _classify_turn(stop_reason, final_text, error_text)
            if code:
                self._emit_error(code, detail)

        with self._lock:
            self._busy = False
        # The mirror must be on disk BEFORE the idle flag opens: a waiter
        # that reads acp-chat-state.json the moment it wakes would otherwise
        # see the stale busy=true snapshot of the turn it just waited out.
        self._write_state()
        self._idle.set()

    def _client_alive(self) -> bool:
        """False only when a REAL child process died. An injected/in-memory
        client (tests, Hermes in-process) has no `_proc` and counts as alive."""
        proc = getattr(self._client, "_proc", None)
        if proc is None:
            return True
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def _restart_child(self) -> None:
        """Respawn the child and re-`session/load` — history keeps its file."""
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
        persisted = {
            "sessionId": self._session_id,
            "transcript": str(self.transcript_path) if self.transcript_path else "",
        }
        self._session_id = None
        try:
            self._connect(persisted)
        except Exception as exc:  # noqa: BLE001 — a failed respawn stays visible
            logger.error("child restart failed: %s", exc)
            self._emit_error("process_exit", f"restart failed: {exc}")

    # ------------------------------------------------------------------
    # ACP events
    # ------------------------------------------------------------------

    def _on_event(self, params: dict) -> None:
        try:
            update = params.get("update") or {}
            su = update.get("sessionUpdate")
            if su in ("agent_message_chunk", "agent_thought_chunk"):
                content = update.get("content") or {}
                if content.get("type") == "text" and su == "agent_message_chunk":
                    self._full_text.append(content.get("text") or "")
            elif su == "config_option_update":
                self._set_config_options(update.get("configOptions"))
                self._write_state()
            elif su == "available_commands_update":
                self._set_commands(update.get("availableCommands"))
                self._write_state()

            for entry in self._mapper.map_update(params, stream=True):
                if entry.get("customType") != acp_chat_events.PREVIEW_CUSTOM_TYPE:
                    self._emit_transcript([entry])
                    continue
                body = entry.get("content") or ""
                grew = len(body) - self._throttle["chars"] > _PREVIEW_GROWTH_CHARS
                elapsed = time.monotonic() - self._throttle["at"] >= _PREVIEW_INTERVAL_S
                if grew or elapsed:
                    self._throttle["chars"] = len(body)
                    self._throttle["at"] = time.monotonic()
                    self._throttle["pending"] = None
                    self._emit_preview([entry])
                else:
                    self._throttle["pending"] = entry
        except Exception:  # noqa: BLE001 — a broken update must not kill the turn
            logger.exception("event handling failed")

    def _on_permission(self, params: dict) -> str:
        """Delegate to the bridge's policy helper — ONE decision path for
        tasks and chat. An unimportable bridge falls back to the same rule
        the helper applies for `yolo`, and rejects otherwise (fail-closed).

        `yolo` means nobody is actually being asked — the request/decision
        pair still gets written to the transcript (`auto_approved=True`
        marks them `display: False`, see the mapper docstrings) so the
        chat view stops filling with self-answered permission cards
        between every tool call (task 663f70fb)."""
        auto_approved = self._permission_policy == "yolo"
        try:
            self._emit_transcript(
                self._mapper.map_permission_request(params, auto_approved=auto_approved)
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            import bridge  # lazy: the daemon must boot without the driver

            choice = bridge._acp_permission_decision(
                params, policy=self._permission_policy, task_id=self._task_id
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("permission helper unavailable (%s); fail-closed", exc)
            choice = (acp_client.ALLOW_ALWAYS
                      if self._permission_policy == "yolo"
                      else acp_client.REJECT_ONCE)
        try:
            self._emit_transcript(
                self._mapper.map_permission_outcome(params, choice, auto_approved=auto_approved)
            )
        except Exception:  # noqa: BLE001
            pass
        return choice

    # ------------------------------------------------------------------
    # sinks + state
    # ------------------------------------------------------------------

    def _emit_transcript(self, entries: list[dict]) -> None:
        if not entries or self._sink is None:
            return
        try:
            self._sink.write(acp_chat_events.ACPEventMapper.dump(entries))
        except Exception:  # noqa: BLE001 — the chat view never kills the turn
            logger.exception("transcript write failed")

    def _emit_preview(self, entries: list[dict]) -> None:
        if not entries or self._preview is None:
            return
        try:
            self._preview.write(acp_chat_events.ACPEventMapper.dump(entries))
        except Exception:  # noqa: BLE001
            logger.exception("preview write failed")

    def _emit_error(self, code: str, detail: str = "", text: Optional[str] = None) -> None:
        with self._lock:
            self._last_error = {"code": code, "detail": str(detail)[:2000],
                                "at": _now_iso()}
        self._emit_transcript(self._mapper.map_chat_error(code, detail, text))
        self._write_state()

    def _absorb_session_result(self, result: Optional[dict]) -> None:
        """`session/new`/`session/load` carry the initial configOptions. Live
        omp sends `availableCommands` only as a notification; reading it here
        too costs nothing and helps a server that does include it."""
        if not isinstance(result, dict):
            return
        self._set_config_options(result.get("configOptions"))
        self._set_commands(result.get("availableCommands"))

    def _set_config_options(self, options: Any) -> None:
        if not isinstance(options, list):
            return
        with self._lock:
            self._config_options = [o for o in options if isinstance(o, dict)]

    def _set_commands(self, commands: Any) -> None:
        if not isinstance(commands, list):
            return
        with self._lock:
            self._commands = [c for c in commands if isinstance(c, dict)]

    def _write_state(self) -> None:
        """Mirror the state next to the transcript, where the backend reads
        it (same mount). Atomic rename: a reader never sees half a file."""
        payload = self.state()
        target = self.state_file
        tmp = target.with_suffix(".json.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, target)
        except OSError:
            logger.warning("state mirror unavailable: %s", target)

    def _read_persisted(self) -> dict:
        try:
            with open(self.persist_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_persisted(self) -> None:
        payload = {
            "sessionId": self._session_id,
            "transcript": str(self.transcript_path) if self.transcript_path else "",
        }
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_file.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, self.persist_file)
        except OSError:
            logger.warning("session id not persisted: %s", self.persist_file)


def _classify_turn(stop_reason: str, final_text: str, error_text: str) -> tuple[
        Optional[str], str]:
    """Turn outcome -> (chat_error code, detail). `None` = nothing to report.

    `cancelled` is deliberately NOT an error: the operator pressed Stop.
    """
    if error_text:
        code = "provider_error" if _PROVIDER_ERROR_RE.search(error_text) else "rpc_error"
        return code, error_text
    if stop_reason == "cancelled":
        return None, ""
    if stop_reason == "error":
        code = "provider_error" if _PROVIDER_ERROR_RE.search(final_text) else "rpc_error"
        return code, final_text[:1000] or "stopReason=error"
    if not final_text.strip():
        # A turn that produced nothing is a failure the operator must see —
        # a silent empty bubble reads as "the agent ignored me".
        if _PROVIDER_ERROR_RE.search(final_text):
            return "provider_error", final_text[:1000]
        return "empty_turn", f"stopReason={stop_reason!r}, no agent text"
    return None, ""


# ---------------------------------------------------------------------------
# control protocol
# ---------------------------------------------------------------------------


def dispatch(session: ChatSession, request: dict) -> dict:
    """One control request -> one response (spec's op table)."""
    op = request.get("op")
    if op == "prompt":
        return session.prompt(request.get("text") or "")
    if op == "cancel":
        return session.cancel()
    if op == "config":
        return session.config(request.get("id"), request.get("value"))
    if op == "state":
        return {"ok": True, **session.state()}
    return {"ok": False, "error": "unknown_op", "detail": str(op)}


class _Handler(socketserver.StreamRequestHandler):
    """One connection = one request line = one response line."""

    def handle(self) -> None:
        raw = self.rfile.readline()
        if not raw:
            return
        try:
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            response = {"ok": False, "error": "bad_request", "detail": str(exc)}
        else:
            try:
                response = dispatch(self.server.chat_session, request)
            except Exception as exc:  # noqa: BLE001 — never drop a connection
                logger.exception("dispatch failed")
                response = {"ok": False, "error": "internal",
                            "detail": f"{type(exc).__name__}: {exc}"}
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ChatSocketServer:
    """Newline-delimited JSON over a Unix socket in front of a ChatSession."""

    def __init__(self, session: ChatSession, socket_path: str):
        self._path = str(socket_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # A stale socket file from a killed daemon would block bind().
        if os.path.exists(self._path):
            try:
                os.unlink(self._path)
            except OSError:
                pass
        self._server = _UnixServer(self._path, _Handler)
        self._server.chat_session = session
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    @property
    def path(self) -> str:
        return self._path

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
            try:
                os.unlink(self._path)
            except OSError:
                pass


def request(socket_path: str, payload: dict, timeout: float = 30.0) -> dict:
    """Client side of the protocol — used by acp_chat_ctl.py and Hermes."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while b"\n" not in b"".join(chunks):
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
    finally:
        sock.close()
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ConnectionError("daemon closed the connection without a response")
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# entrypoint (tmux window 3)
# ---------------------------------------------------------------------------


def model_selector(env: "Mapping[str, str]") -> Optional[str]:
    """OMP_ACP_MODEL (explicit override) > OMP_MODEL_SELECTOR (entrypoint-
    rendered) > mc-openai/<OPENAI_MODEL> — the same precedence as
    launch-omp.sh and bridge._acp_model_selector, so chat and task sessions
    of one agent can never drift onto different models. None when nothing
    is set (the caller decides whether that is a boot error)."""
    for key in ("OMP_ACP_MODEL", "OMP_MODEL_SELECTOR"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    openai_model = (env.get("OPENAI_MODEL") or "").strip()
    return f"mc-openai/{openai_model}" if openai_model else None


def build_session() -> ChatSession:
    """The production wiring: an `omp acp` child on the pinned ACP cwd, with
    the transcript written where the backend's chat reader tails it."""
    cwd = default_cwd()
    model = model_selector(os.environ)
    if model is None:
        # No baked-in default: a missing model is a boot error, not a silent
        # fallback to omp's built-in provider catalog (ADR-054, as in
        # bridge._default_model_selector).
        raise RuntimeError(
            "OMP_MODEL_SELECTOR / OPENAI_MODEL not set — entrypoint must "
            "render models.yml first; refusing to open a chat session on "
            "omp's built-in default model"
        )
    sessions_dir = acp_chat_events.session_dir(cwd=cwd)
    if sessions_dir is None:
        raise RuntimeError(
            "no sessions directory for the chat transcript — neither "
            "OMP_HOME+OMP_PROFILE nor PI_CODING_AGENT_DIR is set (the "
            "entrypoint exports them)"
        )
    state_dir = Path(os.environ.get("OMP_HOME") or "/home/agent/.omp")
    return ChatSession(
        client_factory=lambda: acp_client.ACPClient(cwd=cwd),
        cwd=cwd,
        state_dir=state_dir,
        sessions_dir=sessions_dir,
        driver="omp",
        model=model,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ACP chat daemon")
    parser.add_argument("--serve", action="store_true",
                        help="run the socket server (the only mode)")
    parser.add_argument("--socket", default=None,
                        help=f"socket path (default: $OMP_HOME/{_DEFAULT_SOCKET_NAME})")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("OMP_ACP_CHAT_LOGLEVEL", "INFO"),
        format="[acp-chat] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if not args.serve:
        parser.error("--serve is required")

    session = build_session()
    session.start()
    server = ChatSocketServer(session, args.socket or default_socket_path())
    sys.stderr.write(
        f"[acp-chat] ready: session={session.state()['sessionId']} "
        f"model={session._model} socket={server.path} "
        f"transcript={session.transcript_path}\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
