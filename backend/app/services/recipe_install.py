"""One-click installation of a local recipe onto a box (PR 6).

Some engines are not a `docker run` away. ds4-server (DwarfStar 4) has to be
cloned, built against a pinned CUDA fork and fed ~110 GiB of asymmetric GGUF
before it can serve a single token. That is an hours-long job — this module is
what makes it survivable and watchable.

Shape of a run
--------------
1. **Render** the recipe's ``install_template`` (same renderer as the launch
   command — one templating implementation in this codebase, not two).
2. **Check the disk** with ``df`` against ``est_weights_gb`` and write a
   warning into the log. A warning, not a block: the estimate is an estimate,
   and the operator may know about a mount we cannot see.
3. **Launch detached** on the box with ``nohup``, output redirected into a
   file there, and remember the PID.
4. **Follow the file** — poll it byte-offset-wise and push new lines into the
   Redis log the UI polls (services/job_log).

Why detached-plus-tail and not "run it over SSH and stream the pipe": the SSH
session would have to stay open for the entire download. Any hiccup — laptop
sleeping, backend redeploy, wifi — kills the install halfway through 110 GiB.
Detached, the box keeps working and a reconnecting poller simply picks the log
back up. The exit code survives too: the wrapper appends a ``MC_EXIT:<code>``
marker, which is the only in-band way to learn how a nohup'd job ended.

Idempotence is the recipe's job, not ours. ``install_template`` must be safe to
run twice (clone only when missing, skip weights already on disk) — the seeded
ds4 entry delegates to a script that is exactly that. We say so in the log
before starting so an operator watching a re-run knows what to expect.
"""

from __future__ import annotations

import asyncio
import logging
import re

from app.services.host_resolver import ResolvedHost
from app.services.job_log import JobLog

logger = logging.getLogger("mc.recipe_install")

LOG_TTL = 24 * 3600      # an install is an all-evening affair; 1h would age out mid-run
STATUS_TTL = 24 * 3600

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)

_NAMESPACE = "recipe:install"

# Module-level so tests can shrink them.
_poll_interval = 5.0
# 8h ceiling: past that, MC stops claiming to know what the box is doing. The
# job itself is detached and keeps running — the log says so.
_max_seconds = 8 * 3600

_SSH_TIMEOUT = 60.0

# Written by the wrapper once the install command returns. The only way to get
# an exit code back out of a detached process.
_EXIT_MARKER = "MC_EXIT:"
_EXIT_RE = re.compile(rf"{_EXIT_MARKER}(-?\d+)")


def install_detach_command(command: str, log_path: str) -> str:
    """Shell line that starts the install on a box, prints its PID and returns.

    Only the ``nohup`` job goes to the background (``{ … & }``), with stdin from
    /dev/null — same rule as ``runtime_manager.detached_launch``. The old form
    ``mkdir … && : > log && nohup … & echo $!`` backgrounded the whole ``&&``
    list as a subshell that kept the SSH session's stdout open until the
    install ended (live 08.10.2026): the SSH call timed out and MC marked a
    running install as failed.
    """
    from shlex import quote as shlex_quote

    wrapped = f"{command}; echo \"{_EXIT_MARKER}$?\""
    return (
        f"mkdir -p ~/.cache/mc && : > {log_path} && "
        f"{{ nohup bash -lc {shlex_quote(wrapped)} >> {log_path} 2>&1 < /dev/null & }} && echo $!"
    )


def failure_reason(exc: BaseException) -> str:
    """A reason the operator can read — a bare TimeoutError has an empty str()."""
    return str(exc) or type(exc).__name__


def job_for(host_id: str, slug: str) -> JobLog:
    """The job log for one (box, recipe) pair.

    Keyed by both: installing the same recipe on two boxes are two independent
    jobs, and one box may install two engines.
    """
    return JobLog(
        _NAMESPACE,
        f"{host_id}:{slug}",
        log_ttl=LOG_TTL,
        status_ttl=STATUS_TTL,
        logger=logger,
    )


def remote_log_path(slug: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(slug))
    return f"~/.cache/mc/install-{safe}.log"


async def get_status(host_id: str, slug: str) -> dict | None:
    return await job_for(host_id, slug).get_status()


async def read_log(host_id: str, slug: str, cursor: int = 0) -> dict:
    result = await job_for(host_id, slug).read(cursor)
    result.setdefault("host_id", str(host_id))
    result.setdefault("slug", slug)
    return result


def _parse_free_gb(df_output: str) -> float | None:
    """Free GB from ``df -Pk <dir>``. None when the output isn't parseable —
    an unknown disk figure must not invent a number to compare against."""
    lines = [ln for ln in df_output.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = lines[-1].split()
    if len(parts) < 4:
        return None
    try:
        return round(int(parts[3]) / (1024 * 1024), 1)
    except ValueError:
        return None


class _Run:
    """One install run: render, check, launch, follow."""

    def __init__(
        self,
        host_id: str,
        slug: str,
        host: ResolvedHost,
        *,
        command: str,
        est_weights_gb: float | None = None,
        display_name: str | None = None,
        worker: ResolvedHost | None = None,
    ) -> None:
        self.host_id = str(host_id)
        self.slug = slug
        self.host = host
        # Two-box recipes: the second box. The job itself still runs on the
        # head only (the recipe's installer reaches the worker on its own);
        # MC uses this just to check the worker's disk too.
        self.worker = worker
        self.command = command
        self.est_weights_gb = est_weights_gb
        self.display_name = display_name or slug
        self.job = job_for(self.host_id, slug)
        self.log_path = remote_log_path(slug)

    async def log(self, text: str, level: str = "info") -> None:
        await self.job.append(text, level)

    async def set_status(self, status: str, *, phase: str, message: str | None = None) -> None:
        await self.job.set_status(
            status,
            phase=phase,
            message=message,
            extra={"host_id": self.host_id, "slug": self.slug},
        )

    async def run_ssh(
        self, command: str, *, timeout: float = _SSH_TIMEOUT, host: ResolvedHost | None = None
    ):
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        return await _ssh_run(command, host=host or self.host, timeout=timeout)

    # ── steps ───────────────────────────────────────────────────────────────

    async def check_disk(self) -> None:
        """Compare free disk against the estimated weight size. Warn, never block.

        A two-box recipe puts its weights on both boxes, so the worker is
        checked the same way — with its own label in the log.
        """
        await self._check_disk_on(self.host, prefix="")
        if self.worker is not None:
            await self._check_disk_on(self.worker, prefix="Zweite Box: ")

    async def _check_disk_on(self, host: ResolvedHost, *, prefix: str) -> None:
        try:
            stdout, _, exit_code = await self.run_ssh("df -Pk $HOME", timeout=30, host=host)
        except Exception as exc:  # noqa: BLE001 — a failed check is not a failed install
            await self.log(f"{prefix}Speicherplatz konnte nicht geprüft werden: {exc}", level="warn")
            return
        free_gb = _parse_free_gb(stdout) if exit_code == 0 else None
        if free_gb is None:
            await self.log(f"{prefix}Speicherplatz konnte nicht ermittelt werden (df unlesbar).", level="warn")
            return
        if self.est_weights_gb is None:
            await self.log(f"{prefix}Freier Speicher: {free_gb} GB.")
            return
        # 10 % head room: the build tree, a partial download and the package
        # cache all live next to the weights.
        needed = round(self.est_weights_gb * 1.1, 1)
        if free_gb < needed:
            await self.log(
                f"{prefix}WARNUNG: nur {free_gb} GB frei, geschätzt gebraucht werden ~{needed} GB "
                f"({self.est_weights_gb} GB Gewichte + 10 % Reserve). Die Installation "
                f"läuft trotzdem los — sie wird abbrechen, wenn der Platz wirklich fehlt.",
                level="warn",
            )
        else:
            await self.log(f"{prefix}Speicherplatz ok: {free_gb} GB frei, ~{needed} GB gebraucht.")

    async def launch(self) -> str | None:
        """Start the install detached. Returns the remote PID, or None."""
        detach = install_detach_command(self.command, self.log_path)
        stdout, stderr, exit_code = await self.run_ssh(detach)
        if exit_code != 0:
            raise RuntimeError(stderr.strip() or f"Start der Installation schlug fehl (exit {exit_code})")
        pid = stdout.strip().splitlines()[-1] if stdout.strip() else None
        await self.log(f"Installation läuft auf der Box (PID {pid or '?'}), Log: {self.log_path}")
        return pid

    async def follow(self, pid: str | None) -> None:
        """Stream the remote log into Redis until the exit marker shows up."""
        offset = 0
        deadline = asyncio.get_running_loop().time() + _max_seconds
        while True:
            await asyncio.sleep(_poll_interval)
            try:
                stdout, _, exit_code = await self.run_ssh(
                    f"tail -c +{offset + 1} {self.log_path} 2>/dev/null"
                )
            except Exception as exc:  # noqa: BLE001 — a dropped poll is not a failed install
                await self.log(f"Log konnte nicht gelesen werden ({exc}) — nächster Versuch.", level="warn")
                continue
            if exit_code == 0 and stdout:
                offset += len(stdout.encode("utf-8", errors="replace"))
                for line in stdout.splitlines():
                    if not line.strip():
                        continue
                    match = _EXIT_RE.search(line)
                    if match:
                        await self._finish(int(match.group(1)))
                        return
                    await self.log(line)

            if asyncio.get_running_loop().time() >= deadline:
                await self.log(
                    f"MC beobachtet diese Installation seit {_max_seconds // 3600} h und hört "
                    f"hier auf. Der Prozess auf der Box läuft weiter — Fortschritt: "
                    f"tail -f {self.log_path}",
                    level="warn",
                )
                await self.set_status(
                    STATUS_FAILED,
                    phase="timeout",
                    message="Zeitlimit der Beobachtung erreicht — Installation läuft evtl. weiter.",
                )
                return

            if pid and pid.isdigit():
                # The marker is the truth; this only catches a process that
                # died without ever writing it (kill -9, box rebooted).
                try:
                    _, _, alive = await self.run_ssh(f"kill -0 {int(pid)} 2>/dev/null", timeout=20)
                except Exception as exc:  # noqa: BLE001 — a failed check proves nothing
                    logger.debug("install %s: liveness check failed: %s", self.slug, exc)
                    continue
                if alive != 0:
                    await self.log(
                        "Der Installationsprozess ist weg, ohne ein Ergebnis zu hinterlassen "
                        "(abgebrochen oder Box neu gestartet).",
                        level="error",
                    )
                    await self.set_status(
                        STATUS_FAILED, phase="lost", message="Prozess ohne Ergebnis beendet."
                    )
                    return

    async def _finish(self, exit_code: int) -> None:
        if exit_code == 0:
            message = f"{self.display_name} installiert."
            await self.log(message)
            await self.set_status(STATUS_DONE, phase="done", message=message)
            return
        message = (
            f"Installation fehlgeschlagen (exit {exit_code}). "
            f"Vollständiges Log auf der Box: {self.log_path}"
        )
        await self.log(message, level="error")
        await self.set_status(STATUS_FAILED, phase="failed", message=message)


async def run_install(
    host_id: str,
    slug: str,
    host: ResolvedHost,
    *,
    command: str,
    est_weights_gb: float | None = None,
    display_name: str | None = None,
    worker: ResolvedHost | None = None,
) -> None:
    """The whole run. Writes its own progress; never raises to the caller."""
    run = _Run(
        host_id, slug, host,
        command=command, est_weights_gb=est_weights_gb, display_name=display_name,
        worker=worker,
    )
    try:
        await run.set_status(STATUS_RUNNING, phase="preflight")
        await run.log(
            f"Installation von {run.display_name} gestartet. Der Befehl ist idempotent — "
            f"ein zweiter Lauf holt nur Fehlendes nach."
        )
        await run.log(f"Befehl: {command}")
        await run.check_disk()

        await run.set_status(STATUS_RUNNING, phase="install")
        pid = await run.launch()
        await run.follow(pid)
    except Exception as exc:  # noqa: BLE001 — the run reports, it never propagates
        logger.exception("install %s on %s failed", slug, host_id)
        await run.log(failure_reason(exc), level="error")
        await run.set_status(STATUS_FAILED, phase="failed", message=failure_reason(exc))


async def start_install(
    host_id: str,
    slug: str,
    host: ResolvedHost,
    *,
    command: str,
    est_weights_gb: float | None = None,
    display_name: str | None = None,
    worker: ResolvedHost | None = None,
) -> None:
    """Clear the previous run's log and spawn the new one in the background.

    The caller (the router) rejects a start while another run for the same
    (box, recipe) is still ``running`` — two installers writing the same clone
    and the same weight directory is a corrupted checkout, not a faster
    download.
    """
    job = job_for(str(host_id), slug)
    await job.reset()
    await job.set_status(
        STATUS_RUNNING,
        phase="starting",
        extra={"host_id": str(host_id), "slug": slug},
    )
    asyncio.create_task(
        run_install(
            str(host_id), slug, host,
            command=command, est_weights_gb=est_weights_gb, display_name=display_name,
            worker=worker,
        )
    )
