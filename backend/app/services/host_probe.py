"""Host inventory probe — "what is actually on this box?" (Box-Wizard, PR 4).

One SSH round-trip, one composite shell command, a flat ``key=value`` answer.
Everything the add-a-box wizard needs to decide what still has to happen:
architecture, GPUs, docker + nvidia runtime, free disk, RAM, and whether the
login can sudo without a password.

Two rules this module lives by:

* **No second SSH implementation.** ``runtime_manager._ssh_run`` is the only
  primitive (lazy import, same pattern as ``sparkrun_manager``) — it owns the
  host resolution chain, the key handling and the timeouts.
* **Unreachable is an answer, not an error.** A box that is off, firewalled or
  has the wrong key returns ``reachable: false`` plus a readable reason. The
  wizard's whole first step exists to *show* that state; a 500 would turn a
  normal situation into a red toast with no information in it.

The probe is strictly read-only. Nothing here installs, starts or changes
anything — see ``host_bootstrap`` for the part that may write.
"""

from __future__ import annotations

import logging
import re

from app.services.host_resolver import ResolvedHost

logger = logging.getLogger("mc.host_probe")

# Bound one probe. 15s is generous for a handful of local commands and still
# short enough that an operator staring at the wizard doesn't think it hung.
PROBE_TIMEOUT = 15.0

# Markers around the payload: SSH banners / MOTD text land on stdout on plenty
# of boxes, and parsing "Welcome to Ubuntu" as inventory would be worse than
# parsing nothing.
_BEGIN = "MC_PROBE_BEGIN"
_END = "MC_PROBE_END"

# One command, deliberately one line per fact.
#
# Portability notes (this must survive a DGX Spark, a random x86 Linux box and
# the Debian test container):
#   * ``df -Pk /`` is POSIX — ``df -h --output=avail`` is GNU-only and dies on
#     BSD/macOS with a usage error.
#   * RAM is read twice: ``/proc/meminfo`` (Linux) and ``sysctl hw.memsize``
#     (BSD/macOS). The parser takes whichever line arrived.
#   * every command that may be absent is ``2>/dev/null``-ed and its line is
#     emitted anyway (empty value) — a missing docker must show up as
#     "docker= " and not as a missing line, so the parser can tell "asked and
#     it wasn't there" from "never asked".
PROBE_COMMAND = f"""
echo {_BEGIN}
echo "arch=$(uname -m 2>/dev/null)"
echo "os=$(uname -s 2>/dev/null)"
echo "kernel=$(uname -r 2>/dev/null)"
echo "user=$(id -un 2>/dev/null)"
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null \
  | while IFS= read -r line; do echo "gpu=$line"; done
echo "nvidia_smi=$(command -v nvidia-smi 2>/dev/null)"
echo "docker_version=$(docker --version 2>/dev/null)"
echo "docker_runtimes=$(docker info --format '{{{{.Runtimes}}}}' 2>/dev/null)"
echo "nvidia_ctk=$(command -v nvidia-ctk 2>/dev/null)"
echo "disk_free_kb=$(df -Pk / 2>/dev/null | awk 'NR==2 {{print $4}}')"
echo "ram_kb=$(awk '/MemTotal/ {{print $2}}' /proc/meminfo 2>/dev/null)"
echo "ram_bytes=$(sysctl -n hw.memsize 2>/dev/null)"
echo "in_docker_group=$(id -nG 2>/dev/null | tr ' ' '\\n' | grep -qx docker && echo yes || echo no)"
echo "sudo_nopasswd=$(sudo -n true 2>/dev/null && echo yes || echo no)"
echo "pkg_manager=$(command -v apt-get 2>/dev/null || command -v dnf 2>/dev/null || command -v yum 2>/dev/null)"
echo {_END}
""".strip()


def _payload_lines(stdout: str) -> list[str]:
    """The lines between the markers. Without a BEGIN marker we take the whole
    output — an old/edited command or a shell that swallowed the echo should
    degrade to "parse what you got", not to "found nothing"."""
    lines = stdout.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _BEGIN) + 1
    except StopIteration:
        start = 0
    try:
        end = next(i for i, ln in enumerate(lines) if ln.strip() == _END)
    except StopIteration:
        end = len(lines)
    return lines[start:end]


def _parse_gpu(value: str) -> dict | None:
    """``NVIDIA GB10, 131072 MiB`` → ``{"name": "NVIDIA GB10", "vram_gb": 128.0}``.

    Anything that doesn't look like that keeps its name and reports
    ``vram_gb: None`` — a GPU we can see but can't size is still a GPU, and
    dropping it would understate the box.
    """
    raw = value.strip()
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    name = parts[0]
    vram_gb: float | None = None
    if len(parts) > 1:
        m = re.match(r"(\d+(?:\.\d+)?)\s*(MiB|GiB|MB|GB)?", parts[1], re.IGNORECASE)
        if m:
            amount = float(m.group(1))
            unit = (m.group(2) or "MiB").lower()
            vram_gb = round(amount / 1024, 1) if unit in ("mib", "mb") else round(amount, 1)
    return {"name": name, "vram_gb": vram_gb}


def parse_probe_output(stdout: str) -> dict:
    """Turn the raw ``key=value`` block into the wizard's inventory shape.

    Pure function — this is where the tests live. Missing facts become ``None``
    / empty rather than raising: a partially answering box (no nvidia-smi, no
    docker) is the normal case for a fresh machine, and it is precisely what
    the wizard's step 2 wants to show.
    """
    gpus: list[dict] = []
    values: dict[str, str] = {}
    for line in _payload_lines(stdout):
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "gpu":
            gpu = _parse_gpu(value)
            if gpu:
                gpus.append(gpu)
        else:
            values[key] = value

    def _num(key: str, divisor: float) -> float | None:
        raw = values.get(key, "")
        if not raw:
            return None
        try:
            return round(float(raw) / divisor, 1)
        except ValueError:
            return None

    docker_version = values.get("docker_version", "")
    docker_runtimes = values.get("docker_runtimes", "")
    # Two independent signals for the nvidia container runtime: what docker
    # itself reports, and whether the toolkit's CLI exists. The first is the
    # truth for "can I run --gpus all today", the second catches the state
    # where the toolkit is installed but the daemon was never restarted.
    ram_gb = _num("ram_kb", 1024 * 1024)
    if ram_gb is None:
        ram_gb = _num("ram_bytes", 1024 ** 3)

    return {
        "arch": values.get("arch") or None,
        "os": values.get("os") or None,
        "kernel": values.get("kernel") or None,
        "user": values.get("user") or None,
        "gpus": gpus,
        "nvidia_smi": bool(values.get("nvidia_smi")),
        "docker": {
            "installed": bool(docker_version),
            "version": docker_version or None,
            "nvidia_runtime": "nvidia" in docker_runtimes.lower(),
            "runtimes": docker_runtimes or None,
            "toolkit_installed": bool(values.get("nvidia_ctk")),
        },
        "disk_free_gb": _num("disk_free_kb", 1024 * 1024),
        "ram_gb": ram_gb,
        "in_docker_group": values.get("in_docker_group") == "yes",
        "sudo_nopasswd": values.get("sudo_nopasswd") == "yes",
        "pkg_manager": values.get("pkg_manager") or None,
    }


def unreachable(reason: str) -> dict:
    """The "couldn't look" answer, in the same shape as a successful probe so
    the frontend never has to branch on which keys exist."""
    return {
        "reachable": False,
        "reason": reason,
        "arch": None,
        "os": None,
        "kernel": None,
        "user": None,
        "gpus": [],
        "nvidia_smi": False,
        "docker": {
            "installed": False,
            "version": None,
            "nvidia_runtime": False,
            "runtimes": None,
            "toolkit_installed": False,
        },
        "disk_free_gb": None,
        "ram_gb": None,
        "in_docker_group": False,
        "sudo_nopasswd": False,
        "pkg_manager": None,
        "raw": "",
    }


async def probe_host(host: ResolvedHost) -> dict:
    """Run the inventory against ``host``.

    Never raises for a host-side problem — connection refused, auth failure,
    timeout and a non-zero exit all come back as ``reachable: false`` with the
    reason attached.
    """
    # Lazy import: runtime_manager imports host_resolver and a fair amount of
    # the runtime stack; importing it at module scope would drag that into the
    # hosts router. Same pattern as sparkrun_manager.get_host_gpu_count.
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    try:
        stdout, stderr, exit_code = await _ssh_run(
            PROBE_COMMAND, host=host, timeout=PROBE_TIMEOUT
        )
    except Exception as exc:  # noqa: BLE001 — every failure is a UI state here
        logger.info("host probe failed for %s: %s", host.ssh_host, exc)
        return unreachable(f"SSH fehlgeschlagen: {exc}")

    inventory = parse_probe_output(stdout)
    # A non-zero exit is not automatically a failure: the command chain ends on
    # whichever sub-command ran last, and e.g. a missing `sudo` binary sets a
    # non-zero status while every fact before it arrived. We only call it a
    # failure when nothing usable came back.
    if inventory["arch"] is None and inventory["os"] is None:
        reason = stderr.strip() or f"Probe-Kommando lieferte keine Inventardaten (exit {exit_code})"
        return unreachable(reason)

    return {"reachable": True, "reason": None, **inventory, "raw": stdout}
