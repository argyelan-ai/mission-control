"""Host bootstrap — bring a box up to "can run a docker engine" (Box-Wizard, PR 4).

The wizard's second step. It closes exactly three gaps and refuses to do
anything else:

  1. docker missing        → install it (official get.docker.com installer)
  2. nvidia GPU but no
     container toolkit     → install nvidia-container-toolkit
  3. login not in the
     docker group          → add it (and say that a re-login is needed)

Hard boundaries, all deliberate:

* **No driver, kernel or reboot work.** A missing ``nvidia-smi`` is *reported*
  with a hint and nothing else. Installing a GPU driver over SSH means a
  possible kernel module swap on a box the operator may be sitting in front of
  — that is a decision for a human, not for a wizard step.
* **Idempotent.** Every action is preceded by a check; a fully prepared box
  produces a log of "already there" lines and finishes as ``done`` without
  touching anything.
* **Announced before executed.** Each action is written to the log *before* it
  runs, so a command that hangs or kills the connection still leaves behind
  what it was doing.
* **Never a password prompt.** Every privileged step goes through ``sudo -n``.
  If that fails the run stops with status ``needs_sudo`` and hands the operator
  the exact command to run by hand — an SSH session blocking forever on an
  invisible password prompt is the worst possible outcome here.

Progress lives in Redis: a list of log lines plus a status document, both with
a 1h TTL so a crashed run cannot pin a host in "running" forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from app.redis_client import get_redis
from app.services.host_probe import PROBE_COMMAND, parse_probe_output
from app.services.host_resolver import ResolvedHost

logger = logging.getLogger("mc.host_bootstrap")

# Log + status expire together — a run whose backend died must not leave a
# host looking permanently busy.
LOG_TTL = 3600
STATUS_TTL = 3600

# Installing docker pulls packages over the network; 10 min is the realistic
# ceiling on a slow link, and still finite.
INSTALL_TIMEOUT = 600.0
CHECK_TIMEOUT = 30.0

# Terminal statuses — used by the router to decide whether a new run may start.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NEEDS_SUDO = "needs_sudo"
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_SUDO)

_DOCKER_INSTALLER_URL = "https://get.docker.com"


def log_key(host_id: str) -> str:
    return f"mc:host:bootstrap:{host_id}:log"


def status_key(host_id: str) -> str:
    return f"mc:host:bootstrap:{host_id}:status"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Run:
    """One bootstrap run against one host.

    Holds nothing but the host, the redis keys and a step counter — all state
    that outlives the run is in Redis, so a poll from any backend worker sees
    the same picture.
    """

    def __init__(self, host_id: str, host: ResolvedHost):
        self.host_id = str(host_id)
        self.host = host
        self.actions: list[str] = []  # what we actually changed, for the summary

    async def log(self, text: str, level: str = "info") -> None:
        redis = await get_redis()
        entry = json.dumps({"ts": time.time(), "level": level, "text": text})
        await redis.rpush(log_key(self.host_id), entry)
        await redis.expire(log_key(self.host_id), LOG_TTL)
        logger.info("bootstrap[%s] %s: %s", self.host_id, level, text)

    async def set_status(
        self, status: str, *, phase: str, message: str | None = None
    ) -> None:
        redis = await get_redis()
        doc = {
            "status": status,
            "phase": phase,
            "message": message,
            "host_id": self.host_id,
            "updated_at": _now_iso(),
            "actions": self.actions,
        }
        await redis.set(status_key(self.host_id), json.dumps(doc), ex=STATUS_TTL)

    async def run_ssh(self, command: str, *, timeout: float) -> tuple[str, str, int]:
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001

        return await _ssh_run(command, host=self.host, timeout=timeout)


async def _ensure_sudo(run: _Run) -> bool:
    """``sudo -n true`` — passwordless sudo available?

    Checked lazily, only once an action actually needs it: a box that is
    already fully prepared must never fail just because its login can't sudo.
    """
    _, _, exit_code = await run.run_ssh("sudo -n true", timeout=CHECK_TIMEOUT)
    return exit_code == 0


def _sudo_hint(missing: str) -> str:
    return (
        f"Passwortloses sudo fehlt — {missing} kann nicht automatisch "
        f"installiert werden. Einmalig auf der Box ausführen und danach den "
        f"Bootstrap wiederholen:\n"
        f"  echo \"$USER ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/mc-$USER"
    )


async def _install_docker(run: _Run) -> None:
    """Install docker via the official installer.

    Downloaded to a file, hashed, logged, THEN executed — never
    ``curl … | sh``. The operator sees the URL and the checksum of what ran on
    their machine in the log, which is the whole difference between "a script
    from the internet ran as root" and "this script, this hash, ran as root".
    """
    await run.log(f"Docker fehlt — Installer wird geladen: {_DOCKER_INSTALLER_URL}")
    stdout, stderr, exit_code = await run.run_ssh(
        f"curl -fsSL {_DOCKER_INSTALLER_URL} -o /tmp/mc-get-docker.sh "
        f"&& sha256sum /tmp/mc-get-docker.sh 2>/dev/null "
        f"|| shasum -a 256 /tmp/mc-get-docker.sh",
        timeout=CHECK_TIMEOUT,
    )
    if exit_code != 0:
        raise RuntimeError(
            f"Installer konnte nicht geladen werden: {stderr.strip() or stdout.strip() or f'exit {exit_code}'}"
        )
    await run.log(f"Installer geladen (sha256: {stdout.strip()})")

    await run.log("Führe aus: sudo -n sh /tmp/mc-get-docker.sh")
    stdout, stderr, exit_code = await run.run_ssh(
        "sudo -n sh /tmp/mc-get-docker.sh 2>&1", timeout=INSTALL_TIMEOUT
    )
    tail = "\n".join(stdout.splitlines()[-8:])
    if tail:
        await run.log(tail)
    if exit_code != 0:
        raise RuntimeError(
            f"Docker-Installation schlug fehl (exit {exit_code}): "
            f"{stderr.strip() or tail or 'keine Ausgabe'}"
        )
    run.actions.append("docker_installed")
    await run.log("Docker installiert.")


async def _install_nvidia_toolkit(run: _Run, pkg_manager: str | None) -> None:
    """Install nvidia-container-toolkit from NVIDIA's repository.

    apt-based distributions only. On dnf/yum/unknown we log what to do instead
    of guessing a package name — a wrong guess here fails halfway through with
    a half-configured repo, which is worse than an honest "do this by hand".
    """
    if not pkg_manager or "apt-get" not in pkg_manager:
        await run.log(
            "NVIDIA Container Toolkit fehlt, aber die Box nutzt kein apt — "
            "bitte nach NVIDIAs Anleitung für die eigene Distribution "
            "installieren (nvidia-container-toolkit) und den Bootstrap danach "
            "erneut laufen lassen.",
            level="warn",
        )
        return

    await run.log("NVIDIA Container Toolkit fehlt — NVIDIA-Repository wird eingetragen.")
    repo_cmd = (
        "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey "
        "| sudo -n gpg --batch --yes --dearmor "
        "-o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg "
        "&& curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list "
        "| sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' "
        "| sudo -n tee /etc/apt/sources.list.d/nvidia-container-toolkit.list > /dev/null"
    )
    _, stderr, exit_code = await run.run_ssh(repo_cmd, timeout=INSTALL_TIMEOUT)
    if exit_code != 0:
        raise RuntimeError(
            f"NVIDIA-Repository konnte nicht eingetragen werden: "
            f"{stderr.strip() or f'exit {exit_code}'}"
        )

    await run.log("Führe aus: sudo -n apt-get update && apt-get install -y nvidia-container-toolkit")
    stdout, stderr, exit_code = await run.run_ssh(
        "sudo -n apt-get update -qq "
        "&& sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nvidia-container-toolkit 2>&1",
        timeout=INSTALL_TIMEOUT,
    )
    tail = "\n".join(stdout.splitlines()[-8:])
    if tail:
        await run.log(tail)
    if exit_code != 0:
        raise RuntimeError(
            f"nvidia-container-toolkit-Installation schlug fehl (exit {exit_code}): "
            f"{stderr.strip() or tail or 'keine Ausgabe'}"
        )

    # The toolkit is only usable once the daemon knows about the runtime.
    await run.log("Führe aus: sudo -n nvidia-ctk runtime configure --runtime=docker && systemctl restart docker")
    _, stderr, exit_code = await run.run_ssh(
        "sudo -n nvidia-ctk runtime configure --runtime=docker "
        "&& sudo -n systemctl restart docker",
        timeout=INSTALL_TIMEOUT,
    )
    if exit_code != 0:
        await run.log(
            "Toolkit installiert, aber die Docker-Konfiguration schlug fehl "
            f"({stderr.strip() or f'exit {exit_code}'}). Manuell: "
            "sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker",
            level="warn",
        )
        return
    run.actions.append("nvidia_toolkit_installed")
    await run.log("NVIDIA Container Toolkit installiert und in Docker eingetragen.")


async def _add_docker_group(run: _Run, user: str | None) -> None:
    await run.log(f"Führe aus: sudo -n usermod -aG docker {user or '$USER'}")
    target = user or "$(id -un)"
    _, stderr, exit_code = await run.run_ssh(
        f"sudo -n usermod -aG docker {target}", timeout=CHECK_TIMEOUT
    )
    if exit_code != 0:
        await run.log(
            f"Konnte den Benutzer nicht zur docker-Gruppe hinzufügen "
            f"({stderr.strip() or f'exit {exit_code}'}). MC funktioniert trotzdem, "
            f"solange docker-Kommandos per sudo laufen.",
            level="warn",
        )
        return
    run.actions.append("docker_group_added")
    await run.log(
        "Benutzer zur docker-Gruppe hinzugefügt — wirksam erst nach einer neuen "
        "SSH-Sitzung (die laufende Verbindung behält die alten Gruppen)."
    )


async def run_bootstrap(host_id: str, host: ResolvedHost) -> None:
    """The whole run. Writes its own progress; never raises to the caller.

    Re-probes the box itself instead of trusting whatever the wizard's step 1
    saw: between the probe and the click on "Bootstrap starten" someone may
    have installed docker by hand, and acting on a stale picture is how an
    "idempotent" script stops being idempotent.
    """
    run = _Run(host_id, host)
    try:
        await run.set_status(STATUS_RUNNING, phase="inventory")
        await run.log("Bootstrap gestartet — Inventar wird frisch geprüft.")

        try:
            stdout, stderr, _ = await run.run_ssh(PROBE_COMMAND, timeout=CHECK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            await run.log(f"SSH-Verbindung fehlgeschlagen: {exc}", level="error")
            await run.set_status(
                STATUS_FAILED, phase="inventory", message=f"SSH fehlgeschlagen: {exc}"
            )
            return

        inv = parse_probe_output(stdout)
        if inv["arch"] is None and inv["os"] is None:
            reason = stderr.strip() or "Box antwortet, liefert aber kein Inventar."
            await run.log(reason, level="error")
            await run.set_status(STATUS_FAILED, phase="inventory", message=reason)
            return

        has_gpu = bool(inv["gpus"])
        await run.log(
            f"Inventar: {inv['arch'] or '?'} / {inv['os'] or '?'}, "
            f"{len(inv['gpus'])} GPU(s), Docker "
            f"{'vorhanden' if inv['docker']['installed'] else 'fehlt'}."
        )

        # ── Docker ────────────────────────────────────────────────────────
        await run.set_status(STATUS_RUNNING, phase="docker")
        if inv["docker"]["installed"]:
            await run.log(f"Docker vorhanden: {inv['docker']['version']} — nichts zu tun.")
        else:
            if not await _ensure_sudo(run):
                hint = _sudo_hint("Docker")
                await run.log(hint, level="error")
                await run.set_status(STATUS_NEEDS_SUDO, phase="docker", message=hint)
                return
            await _install_docker(run)

        # ── NVIDIA Container Toolkit ──────────────────────────────────────
        await run.set_status(STATUS_RUNNING, phase="nvidia")
        if not has_gpu:
            if inv["nvidia_smi"]:
                await run.log(
                    "nvidia-smi ist da, meldet aber keine GPU — Treiber/Karte prüfen.",
                    level="warn",
                )
            else:
                await run.log(
                    "Keine NVIDIA-GPU erkannt (kein nvidia-smi). Die Box läuft im "
                    "CPU-Betrieb; Treiber installiert MC bewusst nicht — das ist "
                    "ein Kernel-Eingriff mit Reboot-Risiko und gehört in deine Hand."
                )
        elif inv["docker"]["nvidia_runtime"]:
            await run.log("NVIDIA-Runtime in Docker aktiv — nichts zu tun.")
        elif inv["docker"]["toolkit_installed"]:
            await run.log(
                "NVIDIA Container Toolkit ist installiert, Docker kennt die Runtime "
                "aber nicht. Führe aus: sudo -n nvidia-ctk runtime configure --runtime=docker"
            )
            if not await _ensure_sudo(run):
                hint = _sudo_hint("die Docker-NVIDIA-Konfiguration")
                await run.log(hint, level="error")
                await run.set_status(STATUS_NEEDS_SUDO, phase="nvidia", message=hint)
                return
            _, stderr, exit_code = await run.run_ssh(
                "sudo -n nvidia-ctk runtime configure --runtime=docker "
                "&& sudo -n systemctl restart docker",
                timeout=INSTALL_TIMEOUT,
            )
            if exit_code == 0:
                run.actions.append("nvidia_runtime_configured")
                await run.log("NVIDIA-Runtime in Docker eingetragen.")
            else:
                await run.log(
                    f"Konfiguration schlug fehl ({stderr.strip() or f'exit {exit_code}'}).",
                    level="warn",
                )
        else:
            if not await _ensure_sudo(run):
                hint = _sudo_hint("das NVIDIA Container Toolkit")
                await run.log(hint, level="error")
                await run.set_status(STATUS_NEEDS_SUDO, phase="nvidia", message=hint)
                return
            await _install_nvidia_toolkit(run, inv["pkg_manager"])

        # ── docker group ──────────────────────────────────────────────────
        await run.set_status(STATUS_RUNNING, phase="group")
        if inv["in_docker_group"]:
            await run.log(
                f"Benutzer '{inv['user'] or '?'}' ist bereits in der docker-Gruppe — nichts zu tun."
            )
        elif not await _ensure_sudo(run):
            await run.log(
                "Kein passwortloses sudo — der Benutzer bleibt ausserhalb der "
                "docker-Gruppe. Manuell: sudo usermod -aG docker $USER",
                level="warn",
            )
        else:
            await _add_docker_group(run, inv["user"])

        summary = (
            "Bootstrap abgeschlossen — nichts zu tun, die Box war schon bereit."
            if not run.actions
            else f"Bootstrap abgeschlossen — ausgeführt: {', '.join(run.actions)}."
        )
        await run.log(summary)
        await run.set_status(STATUS_DONE, phase="done", message=summary)

    except Exception as exc:  # noqa: BLE001 — the run reports, it never propagates
        logger.exception("bootstrap for host %s failed", host_id)
        await run.log(str(exc), level="error")
        await run.set_status(STATUS_FAILED, phase="failed", message=str(exc))


async def get_status(host_id: str) -> dict | None:
    """The status document, or None when no run was ever started (or it aged out)."""
    redis = await get_redis()
    raw = await redis.get(status_key(str(host_id)))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def read_log(host_id: str, cursor: int = 0) -> dict:
    """Log lines from ``cursor`` on, plus the current status.

    One response for the poller: status and lines come from the same read, so
    the UI can never show "done" while still missing the last lines (or the
    reverse). ``cursor`` is a plain index into the append-only list — the
    caller sends back what it got and gets exactly the new lines.
    """
    redis = await get_redis()
    hid = str(host_id)
    cursor = max(0, int(cursor))
    raw_lines = await redis.lrange(log_key(hid), cursor, -1)
    lines = []
    for raw in raw_lines:
        try:
            lines.append(json.loads(raw))
        except json.JSONDecodeError:
            lines.append({"ts": None, "level": "info", "text": str(raw)})

    status_doc = await get_status(hid)
    status = (status_doc or {}).get("status") or "idle"
    return {
        "host_id": hid,
        "status": status,
        "phase": (status_doc or {}).get("phase"),
        "message": (status_doc or {}).get("message"),
        "actions": (status_doc or {}).get("actions") or [],
        "running": status == STATUS_RUNNING,
        "lines": lines,
        "cursor": cursor + len(lines),
    }


async def start_bootstrap(host_id: str, host: ResolvedHost) -> None:
    """Clear the previous run's log and spawn the new one in the background.

    The caller (the router) is responsible for rejecting a start while another
    run is still ``running`` — see ``BootstrapAlreadyRunning`` there.
    """
    redis = await get_redis()
    hid = str(host_id)
    await redis.delete(log_key(hid))
    run = _Run(hid, host)
    await run.set_status(STATUS_RUNNING, phase="starting")
    asyncio.create_task(run_bootstrap(hid, host))
