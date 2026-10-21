#!/usr/bin/env python3
"""
acp_client.py — thin synchronous client library for `omp acp` (Agent Client
Protocol over stdio, line-delimited JSON-RPC).

Spawns `omp acp` as a child process and speaks JSON-RPC 2.0 over stdin/stdout
(one JSON object per line). A dedicated reader thread dispatches responses,
server->client requests (`session/request_permission`, `fs/*`, and — only
when a handler is registered — `elicitation/create`) and notifications
(`session/update`, `elicitation/complete`).

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
import errno
import json
import logging
import os
import stat
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger("acp_client")

#: Where MC tells a container agent to put its results: the ADR-022 mount
#: (``~/.mc/deliverables/<slug>:/deliverables``, compose_renderer) and the
#: first path the backend accepts for a deliverable
#: (``/deliverables/<task_id>/``, deliverable_paths.accepted_path_prefixes).
#: The fs jail serves it next to the session cwd, so omp's write tool can put
#: a result where the dispatch prompt asks for it. Only used when the folder
#: exists — the host (Hermes) has no such mount and stays at the cwd.
DEFAULT_DELIVERABLES_DIR = "/deliverables"

#: Override for the deliverables root: one or more absolute folders,
#: separated by ``os.pathsep``. Set but empty = no extra root (cwd only).
DELIVERABLES_DIR_ENV = "MC_DELIVERABLES_DIR"

#: Every agent container's checkouts mount
#: (``~/.mc/workspaces/<slug>:/workspace``, compose_renderer; the omp
#: entrypoint's cwd). Served as a scratch root (:data:`DEFAULT_SCRATCH_DIRS`),
#: but never offered as one in a refusal
#: (:meth:`ACPClient._fs_outside_message`): it is not scratch — it lives on
#: in the host's checkouts folder and holds the main clone and every task's
#: worktree.
CHECKOUTS_DIR = "/workspace"

#: The scratch roots, served next to the session cwd and the deliverables
#: root: the container's own /tmp and its checkouts mount /workspace
#: (:data:`CHECKOUTS_DIR`). omp's bash writes both anyway — the jail only kept its read/write tools out (a task
#: in a worktree cwd, or a chat turn, writing a scratch file to /tmp; a
#: task writing a loose file to /workspace). Same realpath check as every
#: root: a link out of /tmp or /workspace (to /etc, to ~/.omp, to the
#: mounts /workspace-ref or /vault) resolves outside and stays refused.
#: /tmp lies on the container's own filesystem next to ~/.omp, so a file
#: served only through a scratch root must also be a regular file with one
#: link (:func:`_open_served`) — a hard link passes realpath.
#:
#: The price of /workspace (operator's decision): a task's cwd is its own
#: worktree there, so its write and edit tools now reach the main clone and
#: the other tasks' worktrees next to it without a refusal — a
#: wrong-checkout edit lands silently. (Reads reached them before: omp
#: reads from disk itself when an fs/read_text_file is refused.)
#:
#: Only served inside a container (:data:`CONTAINER_MARKER`): the host's
#: /tmp is the whole machine's, so the host keeps cwd + deliverables.
DEFAULT_SCRATCH_DIRS = ("/tmp", CHECKOUTS_DIR)

#: Override for the scratch roots: one or more absolute folders, separated
#: by ``os.pathsep`` — used as given, in a container or not. Set but empty
#: = no scratch root.
SCRATCH_DIRS_ENV = "MC_FS_SCRATCH_DIRS"

#: The file Docker puts into every container (backend nodes.py uses the
#: same test). Without it :data:`DEFAULT_SCRATCH_DIRS` is not served.
CONTAINER_MARKER = "/.dockerenv"


def _usable_roots(candidates: Iterable[str], kind: str = "deliverables") -> tuple[str, ...]:
    """realpath of every candidate that is an absolute, existing folder.

    Stored resolved, so the jail compares final paths with final paths. The
    filesystem root is refused: it would open every path on the box.
    """
    roots: list[str] = []
    for raw in candidates:
        cand = raw.strip()
        if not cand:
            continue
        if not os.path.isabs(cand):
            logger.warning("fs jail: ignoring relative %s root %r", kind, cand)
            continue
        real = os.path.realpath(cand)
        if real == os.path.realpath(os.sep):
            logger.warning("fs jail: refusing %r as a %s root", cand, kind)
            continue
        if os.path.isdir(real) and real not in roots:
            roots.append(real)
    return tuple(roots)


def deliverables_roots(environ: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """The deliverables folder(s) the fs jail serves besides the session cwd.

    ``MC_DELIVERABLES_DIR`` when set, else :data:`DEFAULT_DELIVERABLES_DIR`.
    """
    env = os.environ if environ is None else environ
    raw = env.get(DELIVERABLES_DIR_ENV)
    if raw is None:
        return _usable_roots([DEFAULT_DELIVERABLES_DIR])
    return _usable_roots(raw.split(os.pathsep))


def scratch_roots(environ: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """The scratch folders the fs jail serves besides the session cwd.

    ``MC_FS_SCRATCH_DIRS`` when set, else :data:`DEFAULT_SCRATCH_DIRS` —
    those only inside a container, and only the ones that exist.
    """
    env = os.environ if environ is None else environ
    raw = env.get(SCRATCH_DIRS_ENV)
    if raw is not None:
        return _usable_roots(raw.split(os.pathsep), "scratch")
    if not os.path.exists(CONTAINER_MARKER):
        return ()
    return _usable_roots(DEFAULT_SCRATCH_DIRS, "scratch")


class _FsRefused(Exception):
    """A path the jail lets through, but whose file it will not serve."""


def _open_served(path: str, real: str, flags: int, single_link: bool) -> int:
    """Open ``real`` (already checked against the roots) for one fs request.

    Opened without waiting (``O_NONBLOCK``) and kept only when it is a
    regular file: the request is answered on the ACP reader thread, and a
    FIFO would block it with no reply. ``single_link`` (a file served only
    through a scratch root): a file with more than one link is refused —
    /tmp shares the container's filesystem with ~/.omp, and a hard link
    into /tmp passes the realpath check. Nothing is truncated here, so a
    refused write leaves the file as it was.
    """
    try:
        fd = os.open(real, flags | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), 0o666)
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # a FIFO opened for writing, no reader
            raise _FsRefused(f"path {path!r} is not a regular file") from exc
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _FsRefused(f"path {path!r} is not a regular file")
        if single_link and st.st_nlink > 1:
            raise _FsRefused(
                f"path {path!r} has {st.st_nlink} hard links; a file in a scratch "
                "folder is only served with one")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _inside(real: str, root: str) -> bool:
    return real == root or real.startswith(root + os.sep)

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


def default_command() -> list[str]:
    """`omp acp`, plus the lesson guards (guard-hook.mjs) when the image ships them.

    The guards run tools/guards/bash-hook.sh before every bash call and
    post-push-hook.sh after a push — the same checks as the Claude Code hooks.
    Kill switch: OMP_GUARD_HOOK=0. A missing hook file means no guards (old image).
    """
    cmd = ["omp", "acp"]
    hook = os.environ.get("OMP_GUARD_HOOK_FILE", "/opt/omp-bridge/guard-hook.mjs")
    if os.environ.get("OMP_GUARD_HOOK", "1") != "0" and os.path.isfile(hook):
        cmd += ["--hook", hook]
    return cmd


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
        self._command = command or default_command()
        self._cwd = cwd
        self._fs_jail: Optional[str] = None  # realpath(cwd) — fs/* restricted here (Review #464 Major)
        # Served too once a session exists: the deliverables root(s) and,
        # in a container, the scratch roots /tmp and /workspace — read here
        # once (the mounts are there from container start). Kept apart: a
        # file served only through a scratch root gets the one-link check.
        self._fs_deliverables_roots: tuple[str, ...] = deliverables_roots()
        self._fs_scratch_roots: tuple[str, ...] = tuple(
            root for root in scratch_roots() if root not in self._fs_deliverables_roots)
        # The ones a refusal offers as scratch: not the checkouts mount, nor
        # anything inside it (persistent, other tasks' worktrees).
        checkouts = os.path.realpath(CHECKOUTS_DIR)
        self._fs_scratch_hint: tuple[str, ...] = tuple(
            root for root in self._fs_scratch_roots if not _inside(root, checkouts))
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
        # Form elicitation (`elicitation/create`, chat package P3). None =
        # the capability is NOT advertised and omp never asks: select/input
        # resolve undefined, confirm false, a plan proposal is auto-approved.
        # Only the chat daemon registers one — an unattended task run must
        # never block on a question nobody is there to answer.
        self._elicitation_cb: Optional[Callable[[Any, dict], None]] = None
        self._elicitation_complete_cb: Optional[Callable[[dict], None]] = None
        self._session_id: Optional[str] = None
        # Full result of the last session/new or session/load — carries
        # `configOptions` and `availableCommands`, which the chat daemon
        # mirrors into acp-chat-state.json. new_session() returns only the id.
        self.last_session_result: dict = {}
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
            elif method == "elicitation/create" and self._elicitation_cb is not None:
                self._handle_elicitation_request(msg)
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
        elif method == "elicitation/complete" and self._elicitation_complete_cb is not None:
            try:
                self._elicitation_complete_cb(msg.get("params") or {})
            except Exception:  # noqa: BLE001 — callback isolation
                logger.exception("on_elicitation complete callback failed")
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

    def _handle_elicitation_request(self, msg: dict) -> None:
        """Hand an `elicitation/create` to the registered callback WITHOUT
        answering it: the operator answers later (seconds or minutes), so
        the callback only records the request and returns — the reader
        thread must keep serving `session/update` meanwhile. The answer goes
        out through :meth:`respond`. A callback that raises declines the
        request on the spot, so omp is never left waiting on a broken
        client."""
        rid = msg["id"]
        params = msg.get("params") or {}
        try:
            self._elicitation_cb(rid, params)  # type: ignore[misc]
        except Exception:  # noqa: BLE001 — a broken callback must not hang omp
            logger.exception("on_elicitation callback failed; declining")
            self.respond(rid, {"action": "decline"})

    def respond(self, request_id: Any, result: dict) -> None:
        """Answer a server->client request that was left open (elicitation).
        Raises ACPError when the child is gone."""
        self._send_raw({"jsonrpc": "2.0", "id": request_id, "result": result})

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
        """Answer one fs/* request with exactly one reply.

        omp waits for this answer; the reader loop only logs an exception
        and sends nothing, so an unanswered request would hang the tool call.
        Whatever breaks (a file that is not UTF-8, a NUL byte in the path,
        params of the wrong shape) becomes an error reply instead — omp then
        falls back to its own disk access.
        """
        rid = msg["id"]
        method = msg.get("method", "")
        try:
            reply = self._fs_reply(method, msg.get("params") or {})
        except Exception as exc:  # noqa: BLE001 — one reply per request, whatever broke
            logger.warning("fs request %s failed: %s: %s", method, type(exc).__name__, exc)
            reply = {"error": {"code": -32000, "message": str(exc) or type(exc).__name__}}
        self._send_raw({"jsonrpc": "2.0", "id": rid, **reply})

    def _fs_reply(self, method: str, params: Any) -> dict:
        """The reply body (``result`` or ``error``) for one fs/* request."""
        if method not in ("fs/read_text_file", "fs/write_text_file"):
            return {"error": {"code": -32601, "message": f"unhandled {method}"}}
        # Review #464 Major: fs/read_text_file + fs/write_text_file are
        # restricted to the session's realpath(cwd) plus the extra roots
        # (/deliverables, and in a container the scratch roots /tmp and
        # /workspace). A symlink or `..` escape resolves through realpath,
        # so the check is on the FINAL path, not the textual one — out of
        # the cwd and out of every extra root alike. No jail (no session
        # yet) -> reject, whatever the extra roots are.
        jail = self._fs_jail
        path = params.get("path")
        if not jail or not isinstance(path, str) or not path:
            return {"error": {"code": -32002,
                              "message": "fs request before session/new (no cwd jail)"}}
        real = os.path.realpath(path)
        if _inside(real, jail) or any(_inside(real, root)
                                      for root in self._fs_deliverables_roots):
            scratch_only = False
        elif any(_inside(real, root) for root in self._fs_scratch_roots):
            scratch_only = True
        else:
            return {"error": {"code": -32002,
                              "message": self._fs_outside_message(path, jail)}}
        if method == "fs/read_text_file":
            try:
                fd = _open_served(path, real, os.O_RDONLY, scratch_only)
            except _FsRefused as exc:
                return {"error": {"code": -32002, "message": str(exc)}}
            with os.fdopen(fd, "r", encoding="utf-8") as f:
                return {"result": {"content": f.read()}}
        content = params.get("content", "")
        if not isinstance(content, str):
            return {"error": {"code": -32602,
                              "message": "fs/write_text_file: content must be a string"}}
        # Encode first: text that is no valid UTF-8 (a lone surrogate passes
        # json.loads) fails here, before any folder or file is made.
        data = content.encode("utf-8")
        # The first file of a task goes to /deliverables/<task_id>/, a folder
        # nobody made yet (and a new subfolder of the cwd, the same). `real`
        # passed the check above and realpath leaves no `..` and no symlink
        # in its missing tail, so every folder made here lies inside an
        # allowed root.
        os.makedirs(os.path.dirname(real), exist_ok=True)
        try:
            fd = _open_served(path, real, os.O_WRONLY | os.O_CREAT, scratch_only)
        except _FsRefused as exc:
            return {"error": {"code": -32002, "message": str(exc)}}
        with os.fdopen(fd, "wb") as f:
            f.truncate(0)  # only now: a refused file keeps its content
            f.write(data)
        return {"result": {}}

    def _fs_outside_message(self, path: str, jail: str) -> str:
        """The refusal for a path outside every root. It names the folders
        the model may use instead, each for what it is for: results go to
        the deliverables root (MC collects them there), /tmp is scratch.
        /workspace is served but never offered (:data:`CHECKOUTS_DIR`):
        it is no scratch, and naming it would steer a refused model into
        the checkouts. The first words stay "path ... outside session cwd
        ..." — transcript scans of the refusals match on them."""
        message = f"path {path!r} outside session cwd {jail!r}"
        if self._fs_deliverables_roots:
            message += "; results go to " + " or ".join(
                repr(os.path.join(root, "<task_id>") + os.sep)
                for root in self._fs_deliverables_roots)
        if self._fs_scratch_hint:
            message += ("; scratch only (MC never collects it, and /tmp is "
                        "wiped on every container recreate): " + ", ".join(
                            repr(root) for root in self._fs_scratch_hint))
        return message

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
        capabilities: dict = {"fs": {"readTextFile": True,
                                     "writeTextFile": True}}
        if self._elicitation_cb is not None:
            # omp checks `elicitation.form != null` — an empty object is
            # enough. Only advertised when someone will actually answer.
            capabilities["elicitation"] = {"form": {}}
        result = self._request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": capabilities,
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
        self.last_session_result = result
        # Review #464 Major: fs/read_text_file + fs/write_text_file are
        # jailed to the session's realpath(cwd) — the child must never read
        # or write outside the working directory it was granted, except in
        # the deliverables root MC itself sends it to and the container's
        # own scratch folders (_fs_deliverables_roots, _fs_scratch_roots).
        try:
            self._fs_jail = os.path.realpath(cwd)
        except OSError:
            self._fs_jail = None
        return sid

    def load_session(self, session_id: str, cwd: str,
                     mcp_servers: Optional[list] = None,
                     timeout: float = 60.0) -> dict:
        """Resume a previously created session; returns the full result.

        The chat daemon (acp_chat.py) calls this on restart with the id it
        persisted, so a container/bridge restart keeps ONE chat history
        instead of opening a fresh session per process. Raises ACPError when
        the server does not know the id — the caller falls back to
        session/new and reports `session_reset`.
        """
        result = self._request("session/load", {
            "sessionId": session_id,
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        }, timeout=timeout)
        self._session_id = session_id
        self.last_session_result = result
        try:
            self._fs_jail = os.path.realpath(cwd)
        except OSError:
            self._fs_jail = None
        return result

    def close_session(self, session_id: str, timeout: float = 30.0) -> dict:
        """`session/close` — the agent drops the session from memory.

        The chat daemon's `new_session` op calls this for the session it just
        replaced, so a long-lived `omp acp` child does not keep one live
        session per `/new`. omp 18.1.10 and 18.10.1 advertise it
        (`sessionCapabilities.close`); it only disposes the in-memory record,
        the session file on disk stays. An unknown id answers `{}`.
        """
        return self._request("session/close", {"sessionId": session_id},
                             timeout=timeout)

    def set_config_option(self, session_id: str, key: str, value: Any,
                          timeout: float = 30.0) -> dict:
        """Set one config option (`model`, `mode`, `thinking`, ...).

        Wire name is `configId` (verified against omp 18.1.10; re-checked live
        against 18.10.1, 12.10.2026).
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

    def on_elicitation(
        self,
        cb: Callable[[Any, dict], None],
        on_complete: Optional[Callable[[dict], None]] = None,
    ) -> None:
        """Register the form-elicitation handler (chat package P3).

        Must be called BEFORE :meth:`initialize`: registering it is what
        advertises `clientCapabilities.elicitation.form`. ``cb(request_id,
        params)`` receives the `elicitation/create` params (mode, sessionId,
        message, requestedSchema) and must return at once; the answer is sent
        later with ``respond(request_id, {"action": "accept", "content":
        {...}})`` or ``{"action": "decline" | "cancel"}``. ``on_complete``
        gets `elicitation/complete` notifications (omp sends them for URL
        mode only)."""
        self._elicitation_cb = cb
        self._elicitation_complete_cb = on_complete

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
