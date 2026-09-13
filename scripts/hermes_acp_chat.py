#!/usr/bin/env python3
"""hermes_acp_chat.py — the `HERMES_DRIVER=acp` half of the host bridge.

WHY: Hermes has no chat at all today. The bridge drives a `hermes --yolo` TUI
in tmux and types into it with `tmux send-keys`; the Sessions page shows
nothing, and everything the composer offers (effort, /model, Stop) has no
addressee. Spec: docs/specs/chat-over-acp.md.

Under `HERMES_DRIVER=acp` the bridge instead holds ONE long-lived
`hermes acp` session through the very same `ChatSession` the omp container
runs (docker/omp-bridge/acp_chat.py) — same mapper, same transcript format,
same state file — so the whole backend reader/preview stack serves Hermes
unchanged:

    backend  ──POST http://host.docker.internal:18794/chat/<op>──►  Handler
                                                                      │
                                                                      ▼
                                                                 ChatDaemon
                                                                      │
                                                        acp_chat.ChatSession
                                                          │            │
                              ~/.mc/agents/hermes/omp-sessions/<cwd>/  │
                                       *.jsonl · acp-chat-state.json   │
                                                                 `hermes acp`

The TUI stays DOWN under this driver: two brains on one agent would double
every dispatch.

This module is deliberately separate from `hermes-bridge.py`: that file is
hyphenated (importable only via importlib), already 1100 lines of tmux
lifecycle, and none of the wiring below needs any of it.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("hermes-bridge.acp")

#: Env switch. Anything but ``acp`` (case/space-insensitive) is the native
#: tmux path — the default, and what the live agent runs today.
DRIVER_ENV = "HERMES_DRIVER"
DEFAULT_DRIVER = "native"
ACP_DRIVER = "acp"

#: Twin of backend ``omp_chat._SESSIONS_DIRNAME``. In a container this name
#: comes from the bind mount; on the host the daemon must write it itself.
SESSIONS_DIRNAME = "omp-sessions"

#: Socket name from the spec (``$OMP_HOME/acp-chat.sock``); on the host
#: ``$OMP_HOME`` is the agent's config dir.
SOCKET_NAME = "acp-chat.sock"

#: `hermes acp` (v0.18.0). ``--accept-hooks`` is the ACP twin of the TUI's
#: ``--yolo``: an MC worker is unattended, an approval prompt would hang it.
ACP_ARGS = ("acp", "--accept-hooks")

#: The ACP session's working directory: the browsable task workspace the
#: dispatch prompt already points Hermes at (``~/.mc/workspaces/hermes``,
#: agent_bootstrap.bootstrap_hermes_agent) — NOT the config dir, which the
#: Files API deliberately never shows.
CWD_ENV = "HERMES_ACP_CWD"


def driver() -> str:
    """``"acp"`` or ``"native"`` — read per call, never cached, so a reload
    picks up a changed launchd environment without a code path of its own."""
    value = (os.environ.get(DRIVER_ENV) or "").strip().lower()
    return value or DEFAULT_DRIVER


def is_acp() -> bool:
    return driver() == ACP_DRIVER


def repo_root(script_path: str | Path) -> Path:
    """Where `docker/omp-bridge` lives. ``MC_REPO_PATH`` first (the same
    convention entrypoint.sh uses — the checkout may live anywhere), else the
    parent of this script's `scripts/` directory."""
    env = os.environ.get("MC_REPO_PATH")
    if env:
        return Path(env)
    return Path(script_path).resolve().parent.parent


def import_acp_chat(root: str | Path) -> tuple[Any, Any, Any]:
    """Import the bridge daemon modules from the checkout: (acp_chat,
    acp_chat_events, acp_client). They are plain scripts in an image folder,
    not a package — sys.path insert is the only way in."""
    path = str(Path(root) / "docker" / "omp-bridge")
    if path not in sys.path:
        sys.path.insert(0, path)
    import acp_chat  # noqa: PLC0415 — deliberately lazy: native never needs it
    import acp_chat_events  # noqa: PLC0415
    import acp_client  # noqa: PLC0415

    return acp_chat, acp_chat_events, acp_client


def default_cwd(home: str | Path) -> str:
    return os.environ.get(CWD_ENV) or str(Path(home) / ".mc" / "workspaces" / "hermes")


def socket_path(workspace: str | Path) -> Path:
    return Path(workspace) / SOCKET_NAME


def sessions_dir(*, workspace: str | Path, cwd: str,
                 root: str | Path | None = None) -> Path:
    """``<workspace>/omp-sessions/<encoded-cwd>`` — created, or RuntimeError.

    This path is the whole contract with the backend: it reads exactly
    ``~/.mc/agents/hermes/omp-sessions/<encoded-cwd>/`` (omp_chat
    .resolve_transcript_dir + _SESSIONS_DIRNAME). One character off and the
    Sessions chat stays empty while every log says "delivered" — so the
    encoding is NOT re-implemented here; it comes from the same
    ``acp_chat_events.session_dir`` the container path uses.
    """
    _, events, _ = import_acp_chat(root or repo_root(__file__))
    target = events.session_dir(
        cwd=cwd, sessions_root=Path(workspace) / SESSIONS_DIRNAME
    )
    if target is None:
        raise RuntimeError(
            f"sessions directory not creatable under {workspace}/{SESSIONS_DIRNAME}"
        )
    return target


def build_session(
    *,
    workspace: str | Path,
    hermes_bin: str,
    env: dict[str, str],
    cwd: str,
    root: str | Path | None = None,
    client_cls: Optional[Callable[..., Any]] = None,
):
    """The production wiring: one `hermes acp` child, transcript where the
    backend reads it, persisted session id next to the agent's own config.

    ``env`` (agent.env) is merged OVER os.environ: the child needs
    MC_AGENT_TOKEN/MC_BASE_URL for its own MC tools, and PATH/HOME from the
    launchd process it was started by — an env without PATH cannot even find
    the tools Hermes shells out to.
    """
    acp_chat, _, acp_client = import_acp_chat(root or repo_root(__file__))
    cls = client_cls or acp_client.ACPClient
    command = [hermes_bin, *ACP_ARGS]
    child_env = {**os.environ, **dict(env)}
    target = sessions_dir(workspace=workspace, cwd=cwd, root=root)
    return acp_chat.ChatSession(
        client_factory=lambda: cls(command=command, cwd=cwd, env=child_env),
        cwd=cwd,
        state_dir=Path(workspace),
        sessions_dir=target,
        driver="hermes",
        # The TUI runs --yolo; the chat must not be more timid than the
        # console it replaces, or an unattended turn stalls on a prompt
        # nobody can answer.
        permission_policy="yolo",
    )


class ChatDaemon:
    """Owns the one ChatSession this host serves, and answers the four
    control ops with an HTTP status.

    The session is built by ``factory`` on every ``start()`` — a restart must
    pick up a rotated token from agent.env, exactly like the tmux watchdog
    re-sources it on every loop.
    """

    #: `prompt` returns immediately (the turn runs async), `state` only reads
    #: memory — the op set is the spec's, nothing else is routed.
    OPS = ("prompt", "cancel", "config", "state")

    def __init__(self, factory: Callable[[], Any]):
        self._factory = factory
        self._session: Any = None
        self._lock = threading.RLock()

    @property
    def session(self) -> Any:
        with self._lock:
            return self._session

    @property
    def running(self) -> bool:
        return self.session is not None

    def start(self) -> dict:
        with self._lock:
            if self._session is not None:
                return {"status": "already_running", "driver": ACP_DRIVER}
            session = self._factory()
            session.start()
            self._session = session
        log.info("acp chat daemon started (driver=%s)", ACP_DRIVER)
        return {"status": "started", "driver": ACP_DRIVER}

    def stop(self) -> dict:
        with self._lock:
            session, self._session = self._session, None
        if session is None:
            return {"ok": True, "stopped": False}
        try:
            session.close()
        except Exception as exc:  # noqa: BLE001 — shutdown is best-effort
            log.warning("acp chat daemon close failed: %s", exc)
        log.info("acp chat daemon stopped")
        return {"ok": True, "stopped": True}

    def restart(self) -> dict:
        """Close the child and run it again. The ACP session itself SURVIVES:
        the daemon persists its id and re-`session/load`s on start, so the
        chat history outlives a bridge reload (spec proof 7)."""
        self.stop()
        return self.start()

    def busy(self) -> bool:
        session = self.session
        if session is None:
            return False
        try:
            return bool(session.state().get("busy"))
        except Exception:  # noqa: BLE001 — an unreadable state is not "idle"
            log.warning("acp chat daemon state unreadable", exc_info=True)
            return True

    def prompt(self, text: str) -> dict:
        status, answer = self.request("prompt", {"text": text})
        return answer if status != 502 else {"ok": False, "error": "unreachable"}

    def wait_idle(self, timeout: float) -> bool:
        session = self.session
        if session is None:
            return True
        return bool(session.wait_idle(timeout))

    def request(self, op: str, payload: dict | None = None) -> tuple[int, dict]:
        """One control op -> (HTTP status, JSON body).

        200 = the daemon answered (``ok`` or a plain refusal like
        ``rpc_error`` — the backend's HttpCtlTransport reads the body on
        anything below 500), 409 = ``busy``, 502 = nobody there. Those three
        are exactly what ``acp_chat_transport.HttpCtlTransport`` distinguishes.
        """
        payload = payload or {}
        session = self.session
        if session is None:
            return 502, {"ok": False, "error": "unreachable",
                         "detail": "acp chat daemon not running"}
        try:
            if op == "prompt":
                answer = session.prompt(str(payload.get("text") or ""))
            elif op == "cancel":
                answer = session.cancel()
            elif op == "config":
                answer = session.config(payload.get("id"), payload.get("value"))
            elif op == "state":
                answer = {"ok": True, **session.state()}
            else:
                return 404, {"ok": False, "error": "unknown_op", "detail": str(op)}
        except Exception as exc:  # noqa: BLE001 — never leak a traceback as HTTP 500
            log.exception("acp chat op %s failed", op)
            return 502, {"ok": False, "error": "unreachable",
                         "detail": f"{type(exc).__name__}: {exc}"}

        if not isinstance(answer, dict):
            return 502, {"ok": False, "error": "unreachable",
                         "detail": f"{op}: no JSON object from the session"}
        if answer.get("ok"):
            return 200, answer
        error = answer.get("error")
        if error == "busy":
            return 409, answer
        if error in ("not_started", "unreachable"):
            return 502, answer
        return 200, answer
