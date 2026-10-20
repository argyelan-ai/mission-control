"""The one door for every Docker call that changes a container (ADR-094 §7).

Why this module exists: an MC update replaces the stack underneath the
backend. While it runs, the system is *drained* — no new work, and above all
no second ``docker compose up`` racing the update engine's own. Background
loops (runtime switch, provisioning, recovery) and buttons (start, restart,
force-recreate) used to call ``docker`` directly from half a dozen files; a
drain could not reach them all. Now every call that creates, replaces,
starts, restarts, stops or removes a container goes through :func:`run` or
:func:`create_subprocess`, and a lint test
(``tests/test_docker_gateway_lint.py``) fails when a new direct call appears.

What the gateway does:

* **No drain → transparent.** It prepends ``docker`` and hands argv and
  keyword arguments unchanged to :func:`subprocess.run` /
  :func:`asyncio.create_subprocess_exec`. Return values and exceptions
  (``TimeoutExpired``, ``FileNotFoundError``, …) are exactly the old ones, so
  callers keep their own error contracts.
* **Drain → "deferred".** Calls that create, replace, start, restart or
  remove a container raise :class:`DockerDeferred` instead of running.
  Stopping a container, ``exec`` inside one, build-cache pruning and all
  read-only calls stay free: they start no work and do not touch what the
  update replaces.

Where the drain comes from: a :class:`DrainSource`. The
``MC_UPDATE_DRAIN`` setting (:class:`SettingsDrainSource`, a manual switch,
off by default) and the Postgres drain the update engine sets
(``system_state``, ``services/update_drain.py``); the boot path
(``background.prepare_process``) wires both with
``update_drain.install_gateway_source()``. Reads are
cached for :data:`DRAIN_CACHE_SECONDS` so a busy loop does not query the
database per container call, and a source that fails keeps the last known
state — a drain must not lift because one read hiccupped.

Scope: the local Docker daemon the MC stack runs on. Commands sent over SSH to
model hosts (``runtime_manager``) are not MC's own containers and are not
routed here.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from app.config import settings

logger = logging.getLogger("mc.docker_gateway")

#: How long one drain read is trusted (ADR-094 §7: "10 s cache").
DRAIN_CACHE_SECONDS = 10.0


# ── classification ────────────────────────────────────────────────────────

#: Plain ``docker <sub>`` commands that never change a container.
_FREE_SUBCOMMANDS = frozenset({
    "exec",  # runs inside an existing container, creates none
    "inspect", "ps", "logs", "version", "info", "stats", "top", "port",
    "events", "diff", "images",
    "stop", "kill",  # stopping starts no work; the operator may need it mid-drain
    "builder",  # build-cache housekeeping, no container involved
    "image",  # image inspect/ls/prune never touch a running container
})
#: ``docker container <sub>`` forms that are free (the rest is guarded).
_FREE_CONTAINER_SUBCOMMANDS = frozenset({
    "inspect", "ls", "list", "ps", "logs", "stats", "top", "port", "diff",
    "stop", "kill", "exec",
})
#: ``docker compose <sub>`` forms that are free (``up``/``run``/… are guarded).
_FREE_COMPOSE_SUBCOMMANDS = frozenset({
    "ps", "ls", "logs", "config", "images", "top", "port", "version", "events",
    "stop", "kill",
})
#: Compose global options that take a separate value (``-p NAME``).
_COMPOSE_VALUE_OPTIONS = frozenset({
    "-p", "--project-name", "-f", "--file", "--env-file", "--profile",
    "--project-directory", "--ansi", "--progress", "--parallel",
})


def _compose_subcommand(args: Sequence[str]) -> str | None:
    """The compose subcommand after the global options, or None."""
    skip = False
    for token in args:
        if skip:
            skip = False
            continue
        if token.startswith("-"):
            if token in _COMPOSE_VALUE_OPTIONS:
                skip = True
            continue
        return token
    return None


def deferred_in_drain(args: Sequence[str]) -> bool:
    """True when ``docker <args>`` creates/replaces/starts/restarts/removes a container.

    Fails closed: an unknown or missing subcommand counts as a change.
    """
    if not args:
        return True
    sub = args[0]
    if sub == "compose":
        compose_sub = _compose_subcommand(args[1:])
        return compose_sub not in _FREE_COMPOSE_SUBCOMMANDS
    if sub == "container":
        return len(args) < 2 or args[1] not in _FREE_CONTAINER_SUBCOMMANDS
    return sub not in _FREE_SUBCOMMANDS


# ── drain state ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DrainState:
    """Whether the system is drained, and by which update run."""

    active: bool
    run_id: str | None = None
    reason: str | None = None


NOT_DRAINED = DrainState(active=False)


class DrainSource(Protocol):
    """Where the drain lives.

    ``read`` is sync and must be thread-safe: the gateway is called from the
    event loop (several call sites run ``subprocess.run`` inline in async
    code) and from worker threads alike. A source backed by the database
    therefore must not block the loop on a cold read — e.g. serve a value an
    async task keeps fresh. Exceptions are fine: :class:`DrainGuard` keeps the
    last known state.
    """

    def read(self) -> DrainState: ...


class SettingsDrainSource:
    """The ``MC_UPDATE_DRAIN`` setting — a manual drain switch, off by default."""

    def read(self) -> DrainState:
        if getattr(settings, "mc_update_drain", False):
            return DrainState(active=True, reason="MC_UPDATE_DRAIN is set")
        return NOT_DRAINED


class AnyDrainSource:
    """Drained when any of the given sources says so (first active one wins)."""

    def __init__(self, *sources: DrainSource) -> None:
        self.sources = sources

    def read(self) -> DrainState:
        for source in self.sources:
            state = source.read()
            if state.active:
                return state
        return NOT_DRAINED


class DrainGuard:
    """Caches one source's answer for ``ttl`` seconds."""

    def __init__(
        self,
        source: DrainSource,
        *,
        ttl: float = DRAIN_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source = source
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._state: DrainState | None = None
        self._read_at = 0.0

    def state(self) -> DrainState:
        with self._lock:
            now = self._clock()
            if self._state is not None and now - self._read_at < self._ttl:
                return self._state
            try:
                fresh = self.source.read()
            except Exception as exc:  # noqa: BLE001 — never let a read break docker
                fresh = self._state or NOT_DRAINED
                logger.warning(
                    "drain read failed (%s: %s) — keeping %s",
                    type(exc).__name__, exc,
                    "drained" if fresh.active else "not drained",
                )
            self._state = fresh
            self._read_at = now
            return fresh

    def reset(self) -> None:
        with self._lock:
            self._state = None
            self._read_at = 0.0


_guard = DrainGuard(SettingsDrainSource())


def set_drain_source(source: DrainSource) -> DrainSource:
    """Swap the drain source and return the previous one."""
    global _guard
    previous = _guard.source
    _guard = DrainGuard(source)
    return previous


def drain_state() -> DrainState:
    return _guard.state()


def reset_drain_cache() -> None:
    _guard.reset()


class DockerDeferred(RuntimeError):
    """A container change was refused because the system is drained."""

    def __init__(self, args: Sequence[str], state: DrainState) -> None:
        self.docker_args = list(args)
        self.run_id = state.run_id
        action = " ".join(self.docker_args[:2]) or "docker"
        detail = f" (update run {state.run_id})" if state.run_id else ""
        super().__init__(
            f"deferred: an MC update is in progress{detail} — "
            f"'docker {action}' runs again after it"
        )


def _check(args: Sequence[str]) -> None:
    if not deferred_in_drain(args):
        return
    state = drain_state()
    if state.active:
        err = DockerDeferred(args, state)
        logger.warning("docker call refused in drain: %s", err)
        raise err


# ── the door ──────────────────────────────────────────────────────────────


def run(args: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run(["docker", *args], **kwargs)`` behind the drain guard.

    Raises :class:`DockerDeferred` in a drain for container changes; every
    other outcome (including subprocess exceptions) is the plain call's.
    """
    _check(args)
    return subprocess.run(["docker", *args], **kwargs)


async def create_subprocess(args: Sequence[str], **kwargs: Any) -> asyncio.subprocess.Process:
    """``asyncio.create_subprocess_exec("docker", *args, **kwargs)`` behind the guard."""
    _check(args)
    return await asyncio.create_subprocess_exec("docker", *args, **kwargs)
