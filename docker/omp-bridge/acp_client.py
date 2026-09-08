#!/usr/bin/env python3
"""
acp_client.py — thin synchronous client library for `omp acp` (Agent Client
Protocol over stdio, line-delimited JSON-RPC).

Spawns `omp acp` as a child process and speaks JSON-RPC 2.0 over stdin/stdout
(one JSON object per line). A dedicated reader thread dispatches responses,
server->client requests (`session/request_permission`, `fs/*`) and
notifications (`session/update`).

Wire contract (verified against omp 18.1.10, 2026-09-08, golden fixtures in
`rpc/acp-*.ndjson`):

    initialize -> session/new -> session/set_config_option -> session/prompt
    session/cancel is a NOTIFICATION (no id) and is safe mid-turn: the pending
    prompt resolves with stopReason="cancelled" and the process/session live on.

    prompt params: {"sessionId", "prompt": [content blocks]} — NOT "content".
    set_config_option params: {"sessionId", "configId", "value"} — NOT "key".
    request_permission reply: {"outcome": {"outcome": "selected", "optionId":
    "allow_once"|"allow_always"|...}} — the client-facing shortcut verbs
    (allow_once etc.) are mapped to the optionId omp echoes back in
    `params.options`.

stderr is logged separately, never parsed.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("acp_client")

# Permission shortcuts -> optionId omp understands ($ys map in omp's
# permission handler). Option ids come from the request's `options` list, so
# we resolve kind -> optionId per request instead of hardcoding.
ALLOW_ONCE = "allow_once"
ALLOW_ALWAYS = "allow_always"
REJECT_ONCE = "reject_once"
REJECT_ALWAYS = "reject_always"

_KIND_TO_OUTCOME = {
    "allow_once": ALLOW_ONCE,
    "allow_always": ALLOW_ALWAYS,
    "reject_once": REJECT_ONCE,
    "reject_always": REJECT_ALWAYS,
}


class ACPError(RuntimeError):
    """Server returned a JSON-RPC error, or the transport broke."""


class ACPTimeout(ACPError):
    """No reply within the deadline."""


@dataclass
class PromptResult:
    """Terminal result of one `session/prompt` turn."""
    stopReason: str
    usage: Optional[dict] = None


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None


class ACPClient:
    """Synchronous ACP client around one `omp acp` child process.

    Usage:
        c = ACPClient(cwd="/work")
        c.initialize()
        sid = c.new_session(cwd="/work")
        c.set_config_option(sid, "model", "mc-openai/GLM-5.3-Flash-EXL3")
        c.on_event(lambda ev: print(ev))
        c.on_permission(lambda req: "allow_once")
        res = c.prompt(sid, "hello")   # blocks until turn end
        c.close()
    """

    def __init__(
        self,
        command: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
    ):
        self._command = command or ["omp", "acp"]
        self._cwd = cwd
        self._fs_jail: Optional[str] = None  # realpath(cwd) — fs/* restricted here (Review #464 Major)
        self._env = env
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._writer_lock = threading.Lock()
        self._next_id = 0
        self._id_lock = threading.Lock()

        self._pending: dict[Any, _Pending] = {}
        # server->client requests awaiting OUR reply (request_permission, fs/*)
        self._server_requests: dict[Any, _Pending] = {}

        self._event_cbs: list[Callable[[dict], None]] = []
        self._permission_cb: Optional[Callable[[dict], str]] = None
        self._session_id: Optional[str] = None
        self._closed = False
        self._start_error: Optional[BaseException] = None

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _ensure_process(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        if self._closed:
            raise ACPError("client is closed")
        self._proc = subprocess.Popen(
            self._command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self._cwd,
            env=self._env,
        )
        self._start_error = None
        self._reader = threading.Thread(
            target=self._read_loop, name="acp-reader", daemon=True
        )
        self._reader.start()
        self._stderr_drain = threading.Thread(
            target=self._drain_stderr, name="acp-stderr", daemon=True
        )
        self._stderr_drain.start()
        atexit.register(self.close)
        return self._proc

    def _drain_stderr(self) -> None:
        proc = self._proc
        assert proc and proc.stderr
        for line in proc.stderr:
            logger.debug("[omp acp stderr] %s", line.rstrip())

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc and proc.stdout
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("non-JSON stdout line: %.200s", line)
                    continue
                try:
                    self._dispatch(msg)
                except Exception:  # noqa: BLE001 — callback isolation
                    logger.exception("dispatch failure for %s", msg.get("method") or msg.get("id"))
        except Exception:  # noqa: BLE001 — stdout closed / crash
            if not self._closed:
                self._start_error = ACPError("omp acp stdout closed unexpectedly")
        finally:
            self._wake_all()

    def _dispatch(self, msg: dict) -> None:
        # 1. Response to one of our requests (has id, method absent)
        if "id" in msg and "method" not in msg:
            pend = self._pending.pop(msg["id"], None)
            if pend is not None:
                pend.response = msg
                pend.event.set()
            return
        method = msg.get("method", "")
        # 2. Server->client request (id + method) — answer it
        if "id" in msg:
            if method == "session/request_permission":
                self._handle_permission_request(msg)
            elif method.startswith("fs/"):
                # Headless fs requests: satisfy from disk so tools work
                # without a callback. read: return file content or error.
                self._handle_fs_request(msg)
            else:
                logger.debug("ignoring server request %s", method)
                self._send_raw({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "method not handled"}})
            return
        # 3. Notification
        if method == "session/update":
            self._fire_event_cbs(msg.get("params") or {})
        else:
            logger.debug("ignoring notification %s", method)

    def _handle_permission_request(self, msg: dict) -> None:
        rid = msg["id"]
        params = msg.get("params") or {}
        if self._permission_cb is None:
            # Safe default: reject once
            option_id = self._resolve_option_id(params, REJECT_ONCE)
            self._send_raw(self._permission_reply(rid, option_id))
            return
        try:
            choice = self._permission_cb(params)
        except Exception:  # noqa: BLE001 — a broken callback must not hang omp
            logger.exception("on_permission callback failed; rejecting")
            choice = REJECT_ONCE
        option_id = self._resolve_option_id(params, choice)
        self._send_raw(self._permission_reply(rid, option_id))

    @staticmethod
    def _permission_reply(rid: Any, option_id: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
        }

    @staticmethod
    def _resolve_option_id(params: dict, choice: str) -> str:
        """Map a shortcut verb to the optionId omp offered on this request."""
        options = params.get("options") or []
        if choice in (ALLOW_ONCE, ALLOW_ALWAYS, REJECT_ONCE, REJECT_ALWAYS):
            for opt in options:
                if opt.get("kind") == choice:
                    return opt.get("optionId", choice)
            # omp's fixed option list uses optionId == kind
            return choice
        # caller passed a raw optionId
        for opt in options:
            if opt.get("optionId") == choice:
                return choice
        return choice

    def _handle_fs_request(self, msg: dict) -> None:
        rid = msg["id"]
        params = msg.get("params") or {}
        method = msg.get("method", "")
        # Review #464 Major: fs/read_text_file + fs/write_text_file are
        # restricted to the session's realpath(cwd). A symlink or `..`
        # escape resolves through realpath, so the check is on the FINAL
        # path, not the textual one. No jail (no session yet) -> reject.
        jail = self._fs_jail
        path = params.get("path")
        if method in ("fs/read_text_file", "fs/write_text_file"):
            if not jail or not isinstance(path, str) or not path:
                self._send_raw({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32002,
                                          "message": "fs request before session/new (no cwd jail)"}})
                return
            real = os.path.realpath(path)
            if real != jail and not real.startswith(jail + os.sep):
                self._send_raw({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32002,
                                          "message": f"path {path!r} outside session cwd {jail!r}"}})
                return
        try:
            if method == "fs/read_text_file":
                with open(real, "r", encoding="utf-8") as f:
                    self._send_raw({"jsonrpc": "2.0", "id": rid,
                                    "result": {"content": f.read()}})
            elif method == "fs/write_text_file":
                with open(real, "w", encoding="utf-8") as f:
                    f.write(params.get("content", ""))
                self._send_raw({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                self._send_raw({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32601,
                                          "message": f"unhandled {method}"}})
        except OSError as exc:
            self._send_raw({"jsonrpc": "2.0", "id": rid,
                            "error": {"code": -32000, "message": str(exc)}})

    def _send_raw(self, obj: dict) -> None:
        proc = self._ensure_process()
        assert proc.stdin
        data = json.dumps(obj)
        with self._writer_lock:
            try:
                proc.stdin.write(data + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise ACPError(f"failed to write to omp acp: {exc}") from exc

    def _next_request_id(self) -> int:
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _request(self, method: str, params: dict, timeout: float = 300.0) -> dict:
        rid = self._next_request_id()
        pend = _Pending()
        self._pending[rid] = pend
        try:
            self._send_raw({"jsonrpc": "2.0", "id": rid, "method": method,
                            "params": params})
        except ACPError:
            self._pending.pop(rid, None)
            raise
        if not pend.event.wait(timeout):
            self._pending.pop(rid, None)
            raise ACPTimeout(f"no response for {method} within {timeout}s")
        resp = pend.response or {}
        if "error" in resp:
            err = resp["error"]
            raise ACPError(f"{method} failed: {err.get('message')} "
                           f"({err.get('data', {}).get('details', '') if isinstance(err.get('data'), dict) else err.get('data', '')})")
        return resp.get("result") or {}

    def _fire_event_cbs(self, params: dict) -> None:
        for cb in list(self._event_cbs):
            try:
                cb(params)
            except Exception:  # noqa: BLE001
                logger.exception("on_event callback failed")

    def _wake_all(self) -> None:
        for pend in list(self._pending.values()):
            pend.event.set()
        for pend in list(self._server_requests.values()):
            pend.event.set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialize(self, timeout: float = 30.0) -> dict:
        """Handshake. Returns server capabilities
        (agentInfo, authMethods, protocolCapabilities, ...)."""
        result = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": True,
                                          "writeTextFile": True}},
        }, timeout=timeout)
        return result

    def new_session(self, cwd: str, mcp_servers: Optional[list] = None,
                    timeout: float = 60.0) -> str:
        """Create a session; returns sessionId."""
        result = self._request("session/new", {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        }, timeout=timeout)
        sid = result.get("sessionId")
        if not sid:
            raise ACPError(f"session/new returned no sessionId: {result!r}")
        self._session_id = sid
        # Review #464 Major: fs/read_text_file + fs/write_text_file are
        # jailed to the session's realpath(cwd) — the child must never read
        # or write outside the working directory it was granted.
        try:
            self._fs_jail = os.path.realpath(cwd)
        except OSError:
            self._fs_jail = None
        return sid

    def set_config_option(self, session_id: str, key: str, value: Any,
                          timeout: float = 30.0) -> dict:
        """Set one config option (`model`, `mode`, `thinking`, ...).

        Wire name is `configId` (verified against omp 18.1.10).
        """
        return self._request("session/set_config_option", {
            "sessionId": session_id,
            "configId": key,
            "value": value,
        }, timeout=timeout)

    def set_model(self, session_id: str, name: str) -> dict:
        """Convenience wrapper: `set_config_option(sid, "model", name)`."""
        return self.set_config_option(session_id, "model", name)

    def prompt(self, session_id: str, text: str,
               timeout: float = 600.0) -> PromptResult:
        """Send one user turn; block until the turn settles.

        Returns PromptResult(stopReason, usage). `session/update` events flow
        to the on_event callbacks while this call blocks.
        """
        result = self._request("session/prompt", {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=timeout)
        return PromptResult(stopReason=result.get("stopReason", ""),
                            usage=result.get("usage"))

    def cancel(self, session_id: Optional[str] = None) -> None:
        """Send `session/cancel` NOTIFICATION (no id, no reply).

        Safe mid-turn: the pending prompt() resolves with stopReason
        "cancelled"; process and session stay alive.
        """
        sid = session_id or self._session_id
        if not sid:
            raise ACPError("cancel() called before any session exists")
        self._send_raw({"jsonrpc": "2.0", "method": "session/cancel",
                        "params": {"sessionId": sid}})

    def on_event(self, cb: Callable[[dict], None]) -> None:
        """Register a callback receiving the `session/update` params dict.

        The interesting `update.sessionUpdate` values seen from omp:
        agent_message_chunk, agent_thought_chunk, tool_call,
        tool_call_update, usage_update, available_commands_update,
        config_option_update, session_info_update.
        """
        self._event_cbs.append(cb)

    def on_permission(self, cb: Callable[[dict], str]) -> None:
        """Register the permission decision callback.

        Receives the `session/request_permission` params (sessionId, toolCall,
        options). Returns one of "allow_once" | "allow_always" |
        "reject_once" | "reject_always" — or a raw optionId from the request.
        Without a callback, every request is rejected once.
        """
        self._permission_cb = cb

    def close(self, timeout: float = 5.0) -> None:
        """Terminate the child process cleanly."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        self._wake_all()
        if proc.poll() is None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._proc = None

    # ------------------------------------------------------------------
    # Convenience: one-liner session bootstrap
    # ------------------------------------------------------------------

    def start_session(self, cwd: str, model: Optional[str] = None,
                      mode: Optional[str] = None,
                      thinking: Optional[str] = None) -> str:
        """initialize + new_session + optional config options. Returns sid."""
        self._ensure_process()
        self.initialize()
        sid = self.new_session(cwd)
        if model:
            self.set_config_option(sid, "model", model)
        if mode:
            self.set_config_option(sid, "mode", mode)
        if thinking:
            self.set_config_option(sid, "thinking", thinking)
        return sid


__all__ = [
    "ACPClient", "ACPError", "ACPTimeout", "PromptResult",
    "ALLOW_ONCE", "ALLOW_ALWAYS", "REJECT_ONCE", "REJECT_ALWAYS",
]
