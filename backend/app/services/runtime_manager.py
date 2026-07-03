"""
RuntimeManager — manages local model runtimes via SSH + Docker / LM Studio CLI.

Supported runtime_type:
- vllm_docker: Docker container on DGX Spark, controllable via SSH docker commands
- lmstudio: single model in LM Studio, controllable via SSH lms load/unload
- unsloth: Unsloth Studio (FastAPI web UI) in a tmux session on the host,
  controllable via SSH tmux new-/kill-session. No Docker because no ARM64 image.

State detection for vllm_docker:
1. SSH: docker inspect --format='{{.State.Status}}' <container>
2. If running: HTTP probe → 200 = "ready", otherwise = "warming"

State detection for lmstudio:
1. SSH: lms ps | grep <lms_identifier> → loaded = "ready", otherwise = "stopped"

State detection for unsloth:
1. SSH: tmux has-session -t unsloth-studio → running
2. If tmux session exists: HTTP probe on endpoint → ready|warming
"""

import json
import logging
import re
from pathlib import Path
from shlex import quote as shlex_quote

import asyncssh
import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime import Runtime
from app.services.host_resolver import (
    ResolvedHost,
    resolve_host_from_runtime_fields,
    settings_fallback_host,
)

logger = logging.getLogger("mc.runtime_manager")

# Path to the registry file (relative to the backend root)
_REGISTRY_PATH = Path(__file__).parent.parent.parent / "config" / "runtimes.json"

# Valid runtime states
RuntimeState = str  # "stopped" | "starting" | "warming" | "ready" | "failed" | "unknown"


def load_registry() -> list[dict]:
    """Loads the runtime definitions from the JSON file."""
    if not _REGISTRY_PATH.exists():
        logger.warning("runtimes.json nicht gefunden: %s", _REGISTRY_PATH)
        return []
    with open(_REGISTRY_PATH, "r") as f:
        return json.load(f)


def get_runtime(runtime_id: str) -> dict | None:
    """Returns a single runtime definition, or None if not found."""
    for rt in load_registry():
        if rt["id"] == runtime_id:
            return rt
    return None


def save_registry(runtimes: list[dict]) -> None:
    """Writes the runtime list back to runtimes.json."""
    with open(_REGISTRY_PATH, "w") as f:
        json.dump(runtimes, f, indent=2, ensure_ascii=False)
        f.write("\n")


def add_lmstudio_runtime(lms_identifier: str, display_name: str, endpoint: str) -> dict:
    """Adds a new LM Studio runtime to runtimes.json."""
    registry = load_registry()
    # Check whether lms_identifier already exists
    for rt in registry:
        if rt.get("lms_identifier") == lms_identifier:
            return rt  # already present — return without duplicating
    # Derive ID from lms_identifier (e.g. "qwen/qwen3-coder-next" → "qwen3-coder-next")
    safe_id = lms_identifier.split("/")[-1].lower().replace(".", "-").replace("_", "-")
    # Ensure the ID is unique
    existing_ids = {rt["id"] for rt in registry}
    unique_id = safe_id
    counter = 2
    while unique_id in existing_ids:
        unique_id = f"{safe_id}-{counter}"
        counter += 1
    max_order = max((rt.get("ui_order", 0) for rt in registry), default=0)
    new_runtime = {
        "id": unique_id,
        "display_name": display_name,
        "runtime_type": "lmstudio",
        "provider": "local",
        "endpoint": endpoint,
        "healthcheck_path": "/v1/models",
        "container_name": None,
        "lms_identifier": lms_identifier,
        "lms_cli_path": "~/.lmstudio/bin/lms",
        "role_tags": [],
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_streaming": True,
        "preferred_context_len": 32768,
        "max_context_len": 131072,
        "gpu_profile": "dgx_spark_heavy",
        "memory_notes": "",
        "startup_notes": "",
        "ui_order": max_order + 1,
        "enabled": True,
    }
    registry.append(new_runtime)
    save_registry(registry)
    return new_runtime


def add_vllm_runtime(
    container_name: str,
    display_name: str,
    endpoint: str,
    role_tags: list[str] | None = None,
) -> dict:
    """Adds a new vLLM Docker runtime to runtimes.json. Idempotent."""
    registry = load_registry()
    for rt in registry:
        if rt.get("container_name") == container_name:
            return rt
    raw = container_name
    if raw.startswith("mc-"):
        raw = raw[3:]
    safe_id = raw.lower().replace(".", "-").replace("_", "-")
    existing_ids = {rt["id"] for rt in registry}
    unique_id = safe_id
    counter = 2
    while unique_id in existing_ids:
        unique_id = f"{safe_id}-{counter}"
        counter += 1
    max_order = max((rt.get("ui_order", 0) for rt in registry), default=0)
    new_runtime = {
        "id": unique_id,
        "display_name": display_name,
        "runtime_type": "vllm_docker",
        "provider": "local",
        "endpoint": endpoint,
        "healthcheck_path": "/v1/models",
        "container_name": container_name,
        "role_tags": role_tags or [],
        "supports_tools": True,
        "supports_reasoning": False,
        "supports_streaming": True,
        "preferred_context_len": 32768,
        "max_context_len": 65536,
        "gpu_profile": "dgx_spark_heavy",
        "memory_notes": "",
        "startup_notes": "",
        "ui_order": max_order + 1,
        "enabled": True,
    }
    registry.append(new_runtime)
    save_registry(registry)
    return new_runtime


def _host_ip(host: ResolvedHost | None) -> str:
    """IP/hostname for endpoint construction — the runtime's host or the
    classic settings fallback (ADR-048)."""
    if host is not None and host.ssh_host:
        return host.ssh_host
    return settings.dgx_ssh_host


def _derive_vllm_endpoint(ports_field: str, *, host: ResolvedHost | None = None) -> str:
    """Extract the first endpoint with internal port 8000 from Docker's 'Ports' field.

    Example: '0.0.0.0:8003->8000/tcp, [::]:8003->8000/tcp'
    Returns: 'http://{host_ip}:8003/v1' or '' if no match.
    """
    if not ports_field:
        return ""
    for part in ports_field.split(","):
        part = part.strip()
        m = re.match(r"^[\d\.\:\[\]]+:(\d+)->8000/tcp", part)
        if m:
            external = m.group(1)
            return f"http://{_host_ip(host)}:{external}/v1"
    return ""


async def _container_runs_vllm_server(
    container_name: str, *, host: ResolvedHost | None = None
) -> tuple[bool, str]:
    """Inspect a container's process list for an actual vllm OpenAI server.

    Needed for containers using ``network_mode: host`` (e.g. sparkrun wrappers)
    where ``docker ps`` reports ``Ports: ''`` and ``_derive_vllm_endpoint``
    can't infer the endpoint from the port mapping. We scan ``docker top``
    output for a ``vllm serve …`` command line and parse ``--port N`` to
    reconstruct the host endpoint. Containers without a matching process
    (CUDA sleepers, build images, etc.) are reported as non-vllm so the
    discovery list stays clean.

    Returns ``(is_vllm_server, endpoint)``. Endpoint is empty when the
    process is missing or the port can't be parsed.
    """
    try:
        stdout, _, exit_code = await _ssh_run(
            f"docker top {container_name} -o cmd 2>/dev/null", host=host
        )
    except Exception as e:
        logger.warning(
            "docker top %s fehlgeschlagen: %s", container_name, e
        )
        return False, ""
    if exit_code != 0:
        return False, ""
    for line in stdout.splitlines():
        if "vllm" not in line or "serve" not in line:
            continue
        port_match = re.search(r"--port\s+(\d+)", line)
        port = int(port_match.group(1)) if port_match else 8000
        return True, f"http://{_host_ip(host)}:{port}/v1"
    return False, ""


async def list_vllm_containers(host: ResolvedHost | None = None) -> list[dict]:
    """Lists running vLLM containers on a host (heuristic via image name).

    Filter order per container:
      1. Image contains ``vllm`` (cheap pre-filter).
      2. Port binding ``…->8000/tcp`` → fast endpoint path.
      3. Fallback: ``docker top`` shows a ``vllm serve …`` process →
         endpoint derived from the ``--port`` argument.
      4. Otherwise (CUDA wrappers like sparkrun, build-only images) → hidden.

    Returns: list of {container_name, image, endpoint, state, is_registered, registered_id}
    """
    cmd = "docker ps --format '{{json .}}' --filter status=running"
    try:
        stdout, _, exit_code = await _ssh_run(cmd, host=host)
    except Exception as e:
        logger.warning("SSH fehlgeschlagen für list_vllm_containers: %s", e)
        return []
    if exit_code != 0:
        return []
    containers = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        image = (data.get("Image") or "").lower()
        name = data.get("Names") or ""
        if "vllm" not in image:
            continue
        endpoint = _derive_vllm_endpoint(data.get("Ports") or "", host=host)
        if not endpoint:
            # Host-network or no port binding — probe processes inside.
            is_vllm, endpoint = await _container_runs_vllm_server(name, host=host)
            if not is_vllm:
                logger.info(
                    "Discovery skipped non-vllm container %s (image=%s, no vllm serve process)",
                    name, image,
                )
                continue
        containers.append({
            "container_name": name,
            "image": data.get("Image") or "",
            "endpoint": endpoint,
            "state": data.get("State") or "running",
        })
    registry = load_registry()
    by_container = {rt.get("container_name"): rt for rt in registry if rt.get("container_name")}
    for c in containers:
        rt = by_container.get(c["container_name"])
        c["is_registered"] = rt is not None
        c["registered_id"] = rt["id"] if rt else None
    return containers


# Default command-level timeout (seconds). Bounds a single SSH exec so a hung
# `docker` call on the Spark can't wedge the whole switch indefinitely (the
# connect_timeout only covers the TCP handshake, not the remote command).
_SSH_COMMAND_TIMEOUT = 60


async def _ssh_run(
    command: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float | None = None,
) -> tuple[str, str, int]:
    """Runs an SSH command on a host (ADR-048: host-aware).

    Args:
        command: remote shell command.
        host: resolved host of the respective runtime (host_resolver chain).
            None → settings fallback (settings.dgx_ssh_*, the classic
            single-box behavior). Without any configured host → a clear
            error instead of a cryptic connect failure against "".
        timeout: command-level timeout in seconds. Defaults to
            ``_SSH_COMMAND_TIMEOUT`` so a hanging remote process raises instead
            of blocking forever. Pass an explicit value for long-running calls.

    Returns: (stdout, stderr, exit_code)
    Raises: asyncssh.Error on connection problems, asyncssh.TimeoutError when
        the command timeout is exceeded, RuntimeError if no host could be
        resolved.
    """
    target = host or settings_fallback_host()
    if target is None or not target.ssh_host:
        raise RuntimeError(
            "Runtime hat keinen Host — kein Host in der Registry gebunden, kein "
            "Legacy-host-Feld und settings.dgx_ssh_host ist leer. Host unter "
            "/hosts anlegen und die Runtime binden."
        )
    async with asyncssh.connect(
        host=target.ssh_host,
        # Registry hosts without their own user/key inherit the settings values —
        # same semantics as the seeder (host_seeder.py).
        username=target.ssh_user or settings.dgx_ssh_user,
        client_keys=[target.ssh_key_path or settings.dgx_ssh_key_path],
        known_hosts=None,  # No known_hosts check on the local network
        connect_timeout=10,
    ) as conn:
        result = await conn.run(
            command,
            check=False,
            timeout=_SSH_COMMAND_TIMEOUT if timeout is None else timeout,
        )
        return (
            result.stdout.strip() if result.stdout else "",
            result.stderr.strip() if result.stderr else "",
            result.exit_status if result.exit_status is not None else -1,
        )


async def _probe_http(endpoint: str, healthcheck_path: str) -> bool:
    """Checks whether the runtime's HTTP endpoint responds."""
    url = endpoint.rstrip("/") + healthcheck_path
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


# ── DGX Spark container eviction + start-verification ────────────────────────
# sparkrun runs each model as a `sparkrun_<hash>_solo` container in `--solo`
# (single-model) mode and we tag it with `--label mc.runtime.slug=<slug>`. A
# recipe switch must free the GPU/RAM *completely* before launching the new
# model, otherwise the second model OOMs against a full box (the live failure:
# a CLI-started Ornith model was never stopped → RAM 105/122 GB → new model
# never came up). container_name is unreliable here — it's None right after a
# switch and CLI/externally-started containers carry a different name/label, so
# we evict by label AND by a full solo-container sweep.

# Module-level so tests can monkeypatch them to 0 for fast polling.
_evict_poll_interval = 1.0   # seconds between "is it free yet?" polls
_verify_poll_interval = 1.0  # seconds between "did it appear?" polls

# Matches sparkrun's single-model container naming: sparkrun_<hash>_solo.
_SOLO_NAME_FILTER = "name=sparkrun_.*_solo"


def _sanitize_slug(slug: str) -> str:
    """Collapse anything non-slug-ish to '_'. Defensive — slugs already pass DB
    constraints, but eviction commands interpolate the slug into a docker label
    filter, so we never want raw user input there."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(slug or ""))


def _running_solo_query(slug: str | None = None) -> str:
    """Build a single remote command that prints the ids of running Spark model
    containers — both label-matched (``mc.runtime.slug=<slug>``) and
    ``sparkrun_*_solo`` name-matched — deduplicated. One SSH round-trip.

    docker ANDs multiple ``--filter`` values, so label and name must be two
    separate ``docker ps`` calls unioned via ``{ ...; ...; } | sort -u``.

    Wrapped in ``bash -o pipefail`` so a ``docker ps`` error exits non-zero
    instead of being masked by ``sort``'s always-zero exit code.  Without
    pipefail the stop-command and the eviction-poll would silently treat a
    daemon failure as "no containers running" and give a false all-clear.
    """
    label_part = (
        f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(_sanitize_slug(slug))}; "
        if slug
        else ""
    )
    inner = (
        f"{{ {label_part}"
        f"docker ps -q --filter {shlex_quote(_SOLO_NAME_FILTER)}; }} | sort -u"
    )
    # shlex_quote produces a safely single-quoted argument for bash -c, handling
    # any single quotes embedded by the inner shlex_quote calls.
    return f"bash -o pipefail -c {shlex_quote(inner)}"


async def _running_solo_containers(
    slug: str | None = None, *, host: ResolvedHost | None = None
) -> list[str]:
    """Return the ids of running Spark model containers (label + solo sweep).

    A regex name filter is used (``--filter name=sparkrun_.*_solo``) which docker
    treats as a substring/regex match on the container name. One SSH call,
    scoped to the runtime's host (ADR-048).

    Raises ``RuntimeError`` when the query exits non-zero (docker daemon error,
    SSH failure, etc.) so the caller sees an *unknown* state rather than an
    empty list that could be mistaken for "no containers running".  The eviction
    poll already catches all exceptions and treats them as "still busy".
    """
    out, err, ec = await _ssh_run(_running_solo_query(slug), host=host, timeout=20)
    if ec != 0:
        raise RuntimeError(err or f"docker ps query failed (exit {ec})")
    return sorted({x for x in out.splitlines() if x.strip()})


async def evict_spark_runtime_containers(
    slug: str | None,
    *,
    host: ResolvedHost | None = None,
    timeout: float = 30.0,
) -> dict:
    """Stop ALL running Spark model containers, then wait until they're gone.

    Host-scoped (ADR-048): all docker commands run on ``host`` — the resolved
    host of the *starting* runtime — so an eviction for box A never stops
    models on box B. ``host=None`` keeps the classic settings.dgx_ssh_* box.

    P0: stops by label (``mc.runtime.slug=<slug>``) AND sweeps every
    ``sparkrun_*_solo`` container, so CLI- or externally-started models that MC
    never tracked are evicted too. Replaces the old ``docker stop {name}`` that
    ran ``docker stop`` with an empty arg whenever ``container_name`` was None.

    P1: after issuing the stop, polls until no Spark model container is left
    running (bounded by ``timeout``). Returns ``ok=False`` with an honest
    message if something is still running when the deadline passes — the caller
    must NOT launch a second model on top of an occupied GPU/RAM.

    Returns ``{"ok": bool, "message": str, "stopped": [ids]}``.
    """
    import asyncio

    safe = _sanitize_slug(slug) if slug else None
    # Single command stops label-matched + solo-name-matched containers. `xargs
    # -r` is the fix for the empty-arg bug: with no matches it runs nothing
    # instead of `docker stop ` (which errored and was silently swallowed).
    stop_cmd = f"{_running_solo_query(safe)} | xargs -r docker stop"
    try:
        stopped_out, stop_err, _ = await _ssh_run(stop_cmd, host=host, timeout=max(timeout, 30))
    except Exception as exc:  # noqa: BLE001 — surface as a clean failure
        logger.warning("evict: stop command raised for %s: %s", slug, exc)
        return {"ok": False, "message": f"Eviction-Stop fehlgeschlagen: {exc}", "stopped": []}

    stopped = [x for x in stopped_out.splitlines() if x.strip()]
    if stop_err:
        logger.info("evict: docker stop stderr for %s: %s", slug, stop_err)

    # P1 — poll until the box is actually free.
    deadline = asyncio.get_running_loop().time() + timeout
    remaining: list[str] = []
    while True:
        try:
            remaining = await _running_solo_containers(safe, host=host)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evict: poll raised for %s: %s — treating as still busy", slug, exc)
            remaining = ["<poll-error>"]
        if not remaining:
            logger.info("evict: Spark free for %s (stopped=%s)", slug, stopped)
            return {
                "ok": True,
                "message": f"Spark freigegeben (gestoppt: {stopped or 'nichts lief'}).",
                "stopped": stopped,
            }
        if asyncio.get_running_loop().time() >= deadline:
            break
        if _evict_poll_interval:
            await asyncio.sleep(_evict_poll_interval)

    logger.error(
        "evict: containers still running after %.0fs for %s: %s",
        timeout, slug, remaining,
    )
    return {
        "ok": False,
        "message": (
            f"Container laufen noch nach {timeout:.0f}s (still running): "
            f"{remaining}. GPU/RAM evtl. nicht frei — Start abgebrochen."
        ),
        "stopped": stopped,
    }


async def verify_spark_container_started(
    slug: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float = 12.0,
) -> bool:
    """Poll for a container carrying ``mc.runtime.slug=<slug>`` to appear.

    A nohup launch returns exit 0 instantly even when vLLM later OOM-crashes in
    the background, so a started=ok=True from the launch alone is a lie. This
    confirms the container actually materialised. Returns True as soon as one
    appears, False if none shows up before ``timeout``.

    Note: this only proves the *container* exists, not that vLLM finished
    loading (warmup is 2-5 min). It catches the immediate-crash / never-started
    case — the exact RC-3 failure mode — without blocking on full warmup.
    """
    import asyncio

    safe = _sanitize_slug(slug)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            out, _, ec = await _ssh_run(
                f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(safe)}",
                host=host,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify: poll raised for %s: %s", slug, exc)
            out, ec = "", -1
        if ec == 0 and any(x.strip() for x in out.splitlines()):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        if _verify_poll_interval:
            await asyncio.sleep(_verify_poll_interval)


# ── PORSCHE control plane (unsloth_porsche) ──────────────────────────────────
# The PORSCHE Windows box is NOT reachable via SSH/tmux like the DGX. It runs a
# Flask control server on :5555 (POST /powershell, GET /health) and sleeps when
# idle. These helpers are the unsloth_porsche analogue to _ssh_run / DGX checks.


async def _porsche_reachable(control_url: str) -> bool:
    """True if PORSCHE's :5555 control server answers — i.e. the box is awake and
    logged in (work-ready). Fails fast (3s) when the box is asleep."""
    url = control_url.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


async def _porsche_powershell(
    control_url: str, command: str, timeout: int = 60
) -> tuple[str, str, int]:
    """Run a PowerShell command on PORSCHE via its Flask :5555 control server.

    Mirrors _ssh_run (DGX): returns (stdout, stderr, returncode). returncode is
    -1 on any transport/HTTP failure so callers treat it like a failed exec.
    """
    url = control_url.rstrip("/") + "/powershell"
    try:
        async with httpx.AsyncClient(timeout=timeout + 10) as client:
            resp = await client.post(url, json={"command": command, "timeout": timeout})
            if resp.status_code != 200:
                return ("", f"control server HTTP {resp.status_code}", -1)
            data = resp.json()
            return (
                (data.get("stdout") or "").strip(),
                (data.get("stderr") or "").strip(),
                int(data.get("returncode", -1)),
            )
    except Exception as e:
        return ("", f"PORSCHE control error: {e}", -1)


def _porsche_port_from_endpoint(endpoint: str) -> int | None:
    """Extract the OpenAI server port from the runtime endpoint (e.g.
    http://192.0.2.20:8000/v1 → 8000). Used to build the default stop command."""
    m = re.search(r"://[^/:]+:(\d+)", endpoint or "")
    return int(m.group(1)) if m else None


def _porsche_default_stop_command(endpoint: str) -> str:
    """PowerShell that stops the unsloth OpenAI server by killing whatever
    process listens on its port (frees the VRAM). Best-effort; an operator can
    override via the runtime's launch_command sibling once a clean stop exists."""
    port = _porsche_port_from_endpoint(endpoint)
    if not port:
        return "'no-port'"
    return (
        f"$p = (Get-NetTCPConnection -LocalPort {port} -State Listen "
        f"-ErrorAction SilentlyContinue).OwningProcess; "
        f"if ($p) {{ Stop-Process -Id $p -Force; 'stopped' }} else {{ 'not-running' }}"
    )


async def get_runtime_state(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Determines the current state of a runtime.

    host: resolved host (host_resolver chain, ADR-048). None → legacy chain
    from the runtime fields (host field → settings.dgx_ssh_*). HTTP-only
    types (cloud/openai_compatible) don't need a host.

    Returns dict with: state, container_status (optional), http_reachable (optional)
    """
    runtime_type = runtime.get("runtime_type", "")
    endpoint = runtime.get("endpoint", "")
    healthcheck_path = runtime.get("healthcheck_path", "/v1/models")
    host = host or resolve_host_from_runtime_fields(runtime)

    if runtime_type == "lmstudio":
        lms_id = runtime.get("lms_identifier", "")
        lms_cli = runtime.get("lms_cli_path", "~/.lmstudio/bin/lms")
        if not lms_id:
            reachable = await _probe_http(endpoint, healthcheck_path)
            return {"state": "ready" if reachable else "stopped", "http_reachable": reachable, "container_status": None}
        try:
            stdout, _, _ = await _ssh_run(f"{lms_cli} ps 2>/dev/null", host=host)
            loaded = lms_id in stdout
        except Exception as e:
            logger.warning("SSH fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"state": "failed", "http_reachable": False, "container_status": "ssh_error"}
        reachable = await _probe_http(endpoint, healthcheck_path) if loaded else False
        return {
            "state": "ready" if loaded else "stopped",
            "http_reachable": reachable,
            "container_status": None,
        }

    if runtime_type == "vllm_docker":
        container_name = runtime.get("container_name", "")
        if not container_name:
            return {"state": "unknown", "http_reachable": False, "container_status": None}

        try:
            stdout, _, exit_code = await _ssh_run(
                f"docker inspect --format='{{{{.State.Status}}}}' {container_name} 2>/dev/null || echo 'not_found'",
                host=host,
            )
        except Exception as e:
            logger.warning("SSH fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"state": "failed", "http_reachable": False, "container_status": "ssh_error"}

        container_status = stdout.strip("'\"") if stdout else "not_found"

        if container_status in ("not_found", ""):
            return {"state": "stopped", "http_reachable": False, "container_status": container_status}

        if container_status == "restarting":
            return {"state": "starting", "http_reachable": False, "container_status": container_status}

        if container_status == "exited":
            return {"state": "stopped", "http_reachable": False, "container_status": container_status}

        if container_status == "running":
            reachable = await _probe_http(endpoint, healthcheck_path)
            return {
                "state": "ready" if reachable else "warming",
                "http_reachable": reachable,
                "container_status": container_status,
            }

        # created, paused, dead, etc.
        return {"state": "stopped", "http_reachable": False, "container_status": container_status}

    if runtime_type == "unsloth":
        tmux_session = runtime.get("tmux_session") or "unsloth-studio"
        try:
            _, _, exit_code = await _ssh_run(
                f"tmux has-session -t {tmux_session} 2>/dev/null", host=host
            )
        except Exception as e:
            logger.warning("SSH fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"state": "failed", "http_reachable": False, "container_status": "ssh_error"}

        if exit_code != 0:
            return {"state": "stopped", "http_reachable": False, "container_status": "no_session"}

        reachable = await _probe_http(endpoint, healthcheck_path or "/")
        return {
            "state": "ready" if reachable else "warming",
            "http_reachable": reachable,
            "container_status": "tmux_running",
        }

    if runtime_type == "unsloth_porsche":
        # Power-managed Windows host: box sleeps, woken via WoL, controlled via
        # Flask :5555. Two-tier check → honest UI states:
        #   :5555 down            → stopped / "asleep"        (UI: Wecken)
        #   :5555 up, /v1 200     → ready   / "serving"       (UI: Stop)
        #   :5555 up, /v1 not 200 → stopped / "booted_no_model" (UI: Start)
        # The model-load window (1-3 min after Start) briefly reads as
        # booted_no_model until /v1/models answers — acceptable for v1; the
        # start_runtime message tells the operator to expect the warmup.
        # Host registry (ADR-048) first, then legacy runtime field, then settings.
        control_url = (
            (host.control_url if host else None)
            or runtime.get("control_url")
            or settings.porsche_control_url
        )
        if not await _porsche_reachable(control_url):
            return {"state": "stopped", "http_reachable": False, "container_status": "asleep"}
        # Defensive: avoid a double "/v1" — endpoint ".../v1" + healthcheck
        # "/v1/models" would probe ".../v1/v1/models" (404). Mirror the
        # normalization in agent_runtime_switch.probe_runtime_model.
        hp = healthcheck_path or "/v1/models"
        if endpoint.rstrip("/").endswith("/v1") and hp.startswith("/v1"):
            hp = hp[len("/v1"):] or "/models"
        reachable = await _probe_http(endpoint, hp)
        return {
            "state": "ready" if reachable else "stopped",
            "http_reachable": reachable,
            "container_status": "serving" if reachable else "booted_no_model",
        }

    if runtime_type in ("openai_compatible", "cloud"):
        # Remote-hosted endpoint — we can't start/stop it, only probe.
        reachable = await _probe_http(endpoint, healthcheck_path or "/v1/models")
        return {
            "state": "ready" if reachable else "stopped",
            "http_reachable": reachable,
            "container_status": None,
        }

    return {"state": "unknown", "http_reachable": False, "container_status": None}


async def start_runtime(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Starts a runtime.

    vllm_docker: docker start via SSH
    lmstudio: lms load via SSH
    host: resolved host of the runtime (ADR-048); None → legacy chain.
    Returns: {"ok": bool, "message": str}
    """
    runtime_type = runtime["runtime_type"]
    host = host or resolve_host_from_runtime_fields(runtime)

    if runtime_type == "lmstudio":
        lms_id = runtime.get("lms_identifier", "")
        lms_cli = runtime.get("lms_cli_path", "~/.lmstudio/bin/lms")
        context_length = runtime.get("context_length")
        if not lms_id:
            return {"ok": False, "message": "lms_identifier nicht konfiguriert."}
        try:
            ctx_flag = f" --context-length {int(context_length)}" if context_length else ""
            # lms load runs in the foreground and can take >60s for large models
            # on cold storage — give it a generous timeout instead of the 60s default.
            _, stderr, exit_code = await _ssh_run(
                f"{lms_cli} load {lms_id} --yes{ctx_flag} 2>&1", host=host, timeout=300
            )
            if exit_code == 0:
                logger.info("LM Studio Modell geladen: %s (ctx=%s)", lms_id, context_length or "default")
                return {"ok": True, "message": f"{runtime['display_name']} wird geladen. Braucht ~1 Minute."}
            return {"ok": False, "message": stderr or f"lms load schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("LMS load fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "vllm_docker":
        container_name = (runtime.get("container_name") or "").strip()
        launch_command = (runtime.get("launch_command") or "").strip()
        try:
            # Path A — try `docker start` on a previously-known container.
            # Skipped when container_name is empty (sparkrun assigns random
            # IDs at launch — only launch_command is set in that case).
            if container_name:
                _, _, inspect_ec = await _ssh_run(
                    f"docker inspect -f '{{{{.State.Status}}}}' {container_name} 2>/dev/null",
                    host=host,
                )
                if inspect_ec == 0:
                    _, stderr, exit_code = await _ssh_run(
                        f"docker start {container_name}", host=host
                    )
                    if exit_code == 0:
                        logger.info(
                            "Runtime gestartet via docker start: %s (%s)",
                            runtime["id"], container_name,
                        )
                        return {
                            "ok": True,
                            "message": f"Container {container_name} wird gestartet. Warmup dauert 2-5 Minuten.",
                        }
                    return {
                        "ok": False,
                        "message": stderr or f"docker start schlug fehl (exit {exit_code})",
                    }
            # Path B — container is gone (auto-removed, never created, or
            # fresh runtime). Fall through to launch_command.
            if launch_command:
                # Run detached via nohup so the SSH session can close before the
                # warmup completes. Logs go to ~/.cache/mc/runtime-launch-<slug>.log
                # for forensics. The recipe is responsible for labelling the new
                # container (e.g. --label mc.runtime.slug=<slug>) so future
                # lifecycle calls can find it.
                slug_safe = "".join(
                    c if c.isalnum() or c in "-_" else "_"
                    for c in str(runtime.get("id") or runtime.get("slug") or "unknown")
                )
                detach_cmd = (
                    f"mkdir -p ~/.cache/mc && "
                    f"nohup bash -lc {shlex_quote(launch_command)} "
                    f"> ~/.cache/mc/runtime-launch-{slug_safe}.log 2>&1 &"
                )
                _, stderr, exit_code = await _ssh_run(detach_cmd, host=host)
                log_path = f"~/.cache/mc/runtime-launch-{slug_safe}.log"
                if exit_code != 0:
                    return {
                        "ok": False,
                        "message": stderr or f"launch_command schlug fehl (exit {exit_code}). Logs: {log_path}",
                    }
                # P2 — nohup returns exit 0 instantly even if vLLM OOM-crashes in
                # the background. Verify a labelled container actually appears
                # before reporting success. Skip only when we can't derive a
                # slug to poll for (no label to match → keep old optimistic ok).
                runtime_slug = runtime.get("slug") or runtime.get("id")
                if runtime_slug:
                    appeared = await verify_spark_container_started(str(runtime_slug), host=host)
                    if not appeared:
                        logger.error(
                            "Runtime %s: launch_command exited 0 but no labelled "
                            "container appeared (likely OOM/crash). Log: %s",
                            runtime["id"], log_path,
                        )
                        return {
                            "ok": False,
                            "message": (
                                f"{runtime['display_name']} gestartet, aber kein Container "
                                f"mit Label mc.runtime.slug={slug_safe} erschienen "
                                f"(wahrscheinlich OOM/Crash). Logs: {log_path}"
                            ),
                        }
                logger.info(
                    "Runtime gestartet via launch_command: %s (log %s)",
                    runtime["id"], log_path,
                )
                return {
                    "ok": True,
                    "message": (
                        f"{runtime['display_name']} wird via launch_command gestartet. "
                        f"Warmup dauert 2-5 Minuten. Logs: {log_path}"
                    ),
                }
            # No container, no launch_command → cannot start.
            return {
                "ok": False,
                "message": (
                    f"Container {container_name or '<none>'} existiert nicht "
                    f"und keine launch_command konfiguriert."
                ),
            }
        except Exception as e:
            logger.error("Start fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "unsloth":
        tmux_session = runtime.get("tmux_session") or "unsloth-studio"
        launch_cmd = runtime.get("launch_command") or (
            "cd ~ && unsloth studio -H 0.0.0.0 -p 8888"
        )
        try:
            # Kill any stale session, then start fresh
            await _ssh_run(f"tmux kill-session -t {tmux_session} 2>/dev/null; true", host=host)
            _, stderr, exit_code = await _ssh_run(
                f"tmux new-session -d -s {tmux_session} '{launch_cmd}' 2>&1", host=host
            )
            if exit_code == 0:
                logger.info("Unsloth Studio gestartet (tmux %s)", tmux_session)
                return {
                    "ok": True,
                    "message": f"{runtime['display_name']} wird gestartet (tmux session '{tmux_session}'). Warmup kann 1-3 Minuten dauern.",
                }
            return {"ok": False, "message": stderr or f"tmux new-session schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("Unsloth Start fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "unsloth_porsche":
        control_url = (
            (host.control_url if host else None)
            or runtime.get("control_url")
            or settings.porsche_control_url
        )
        launch_cmd = runtime.get("launch_command")
        if not launch_cmd or launch_cmd.strip().startswith("TODO"):
            return {
                "ok": False,
                "message": "launch_command nicht konfiguriert — echten PowerShell-Befehl "
                "eintragen, der den unsloth-OpenAI-Server auf PORSCHE startet (detached, z.B. Start-Process).",
            }
        if not await _porsche_reachable(control_url):
            return {
                "ok": False,
                "message": "PORSCHE nicht erreichbar (:5555). Box zuerst wecken (Wake-on-LAN).",
            }
        _, stderr, rc = await _porsche_powershell(control_url, launch_cmd, timeout=60)
        if rc == 0:
            logger.info("unsloth_porsche gestartet via %s", control_url)
            return {
                "ok": True,
                "message": f"{runtime['display_name']} wird gestartet. Modell-Warmup kann 1-3 Minuten dauern.",
            }
        return {"ok": False, "message": stderr or f"PowerShell-Start schlug fehl (rc {rc})"}

    if runtime_type in ("openai_compatible", "cloud"):
        return {
            "ok": False,
            "message": "Remote-hosted Runtime — Lifecycle wird vom Provider gesteuert.",
        }

    return {"ok": False, "message": f"Unbekannter runtime_type: {runtime_type}"}


async def stop_runtime(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Stops a runtime.

    vllm_docker: docker stop via SSH
    lmstudio: lms unload via SSH
    host: resolved host of the runtime (ADR-048); None → legacy chain.
    Returns: {"ok": bool, "message": str}
    """
    runtime_type = runtime["runtime_type"]
    host = host or resolve_host_from_runtime_fields(runtime)

    if runtime_type == "lmstudio":
        lms_id = runtime.get("lms_identifier", "")
        lms_cli = runtime.get("lms_cli_path", "~/.lmstudio/bin/lms")
        if not lms_id:
            return {"ok": False, "message": "lms_identifier nicht konfiguriert."}
        try:
            _, stderr, exit_code = await _ssh_run(f"{lms_cli} unload {lms_id} 2>&1", host=host)
            if exit_code == 0:
                logger.info("LM Studio Modell entladen: %s", lms_id)
                return {"ok": True, "message": f"{runtime['display_name']} wurde entladen. LM Studio läuft weiter."}
            return {"ok": False, "message": stderr or f"lms unload schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("LMS unload fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "vllm_docker":
        container_name = (runtime.get("container_name") or "").strip()
        # RC-1 fix: container_name is None right after a recipe switch (sparkrun
        # assigns a fresh random id each run). Running `docker stop ` with an
        # empty arg errors and was silently swallowed, leaving the old model up.
        # Fall back to label/solo eviction so the model is actually stopped.
        if not container_name:
            slug = runtime.get("slug") or runtime.get("id")
            logger.info(
                "stop_runtime: empty container_name for %s — evicting by label/solo",
                runtime.get("id"),
            )
            return await evict_spark_runtime_containers(slug, host=host)
        try:
            _, stderr, exit_code = await _ssh_run(
                f"docker stop {shlex_quote(container_name)}", host=host, timeout=120
            )
            if exit_code == 0:
                logger.info("Runtime gestoppt: %s (%s)", runtime["id"], container_name)
                return {"ok": True, "message": f"Container {container_name} wurde gestoppt."}
            return {"ok": False, "message": stderr or f"docker stop schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("Stop fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "unsloth":
        tmux_session = runtime.get("tmux_session") or "unsloth-studio"
        try:
            _, stderr, exit_code = await _ssh_run(
                f"tmux kill-session -t {tmux_session} 2>&1", host=host
            )
            # Exit code 1 with "can't find session" is fine — already stopped
            if exit_code == 0 or "can't find session" in stderr.lower():
                logger.info("Unsloth Studio gestoppt (tmux %s)", tmux_session)
                return {"ok": True, "message": f"{runtime['display_name']} wurde gestoppt."}
            return {"ok": False, "message": stderr or f"tmux kill-session schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("Unsloth Stop fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "unsloth_porsche":
        control_url = (
            (host.control_url if host else None)
            or runtime.get("control_url")
            or settings.porsche_control_url
        )
        if not await _porsche_reachable(control_url):
            # Box already asleep / unreachable → nothing to stop.
            return {"ok": True, "message": f"{runtime['display_name']} nicht erreichbar — gilt als gestoppt."}
        if not _porsche_port_from_endpoint(runtime.get("endpoint", "")):
            # No derivable port → we cannot build a real kill command. Fail loudly
            # instead of running a no-op that would falsely report VRAM freed.
            return {"ok": False, "message": "Kein Port aus endpoint ableitbar — Stop-Befehl manuell konfigurieren."}
        stop_cmd = _porsche_default_stop_command(runtime.get("endpoint", ""))
        _, stderr, rc = await _porsche_powershell(control_url, stop_cmd, timeout=30)
        if rc == 0:
            logger.info("unsloth_porsche gestoppt via %s", control_url)
            return {"ok": True, "message": f"{runtime['display_name']} wurde gestoppt (Modell aus VRAM entladen)."}
        return {"ok": False, "message": stderr or f"PowerShell-Stop schlug fehl (rc {rc})"}

    if runtime_type in ("openai_compatible", "cloud"):
        return {
            "ok": False,
            "message": "Remote-hosted Runtime — Lifecycle wird vom Provider gesteuert.",
        }

    return {"ok": False, "message": f"Unbekannter runtime_type: {runtime_type}"}


async def restart_runtime(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Restarts a runtime.

    vllm_docker: docker restart via SSH
    lmstudio: lms unload + lms load via SSH
    host: resolved host of the runtime (ADR-048); None → legacy chain.
    Returns: {"ok": bool, "message": str}
    """
    runtime_type = runtime["runtime_type"]
    host = host or resolve_host_from_runtime_fields(runtime)

    if runtime_type == "lmstudio":
        lms_id = runtime.get("lms_identifier", "")
        lms_cli = runtime.get("lms_cli_path", "~/.lmstudio/bin/lms")
        if not lms_id:
            return {"ok": False, "message": "lms_identifier nicht konfiguriert."}
        try:
            await _ssh_run(f"{lms_cli} unload {lms_id} 2>&1", host=host)
            # Same generous timeout as start_runtime — lms load blocks until
            # the model is fully in VRAM, which can exceed 60s for large models.
            _, stderr, exit_code = await _ssh_run(
                f"{lms_cli} load {lms_id} --yes 2>&1", host=host, timeout=300
            )
            if exit_code == 0:
                logger.info("LM Studio Modell neu geladen: %s", lms_id)
                return {"ok": True, "message": f"{runtime['display_name']} wird neu geladen."}
            return {"ok": False, "message": stderr or f"lms load schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("LMS restart fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "vllm_docker":
        container_name = runtime.get("container_name") or None
        # container_name is None after every recipe-switch (the DB field is cleared
        # and only re-populated once the new container appears). Running
        # `docker restart None` against that value caused a 400 (live incident
        # 2026-06-27).  Discover the actual running container via the label+solo
        # sweep that evict_spark_runtime_containers already uses, then restart it.
        if not container_name:
            slug = runtime.get("slug") or runtime.get("id")
            try:
                discovered = await _running_solo_containers(slug, host=host)
            except Exception as e:  # noqa: BLE001
                logger.error("Restart: container-discovery fehlgeschlagen für %s: %s", runtime["id"], e)
                return {
                    "ok": False,
                    "message": (
                        f"container_name ist nicht gesetzt und die Container-Suche "
                        f"schlug fehl: {e}. Logs: ~/.cache/mc/runtime-launch-{slug}.log"
                    ),
                }
            if not discovered:
                logger.warning("Restart: kein laufender Spark-Container für %s gefunden", runtime["id"])
                return {
                    "ok": False,
                    "message": (
                        f"Kein laufender Spark-Container für Runtime '{runtime['id']}' gefunden "
                        f"(container_name nicht gesetzt, kein sparkrun_*_solo aktiv). "
                        f"Start-Log: ~/.cache/mc/runtime-launch-{slug}.log"
                    ),
                }
            if len(discovered) > 1:
                logger.warning("Restart: mehrere Spark-Container für %s: %s", runtime["id"], discovered)
                return {
                    "ok": False,
                    "message": (
                        f"Mehrdeutig: {len(discovered)} Spark-Container laufen für "
                        f"'{runtime['id']}' ({discovered}). Manuelles Eingreifen nötig."
                    ),
                }
            container_name = discovered[0]
            logger.info("Restart: container_name nicht gesetzt, per Sweep gefunden: %s", container_name)
        try:
            _, stderr, exit_code = await _ssh_run(
                f"docker restart {container_name}", host=host
            )
            if exit_code == 0:
                logger.info("Runtime neugestartet: %s (%s)", runtime["id"], container_name)
                return {"ok": True, "message": f"Container {container_name} wird neugestartet. Warmup dauert 2-5 Minuten."}
            return {"ok": False, "message": stderr or f"docker restart schlug fehl (exit {exit_code})"}
        except Exception as e:
            logger.error("Restart fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"ok": False, "message": f"SSH-Fehler: {e}"}

    if runtime_type == "unsloth":
        # Unsloth restart: stop + start via the same tmux-session helpers above.
        stop_result = await stop_runtime(runtime, host=host)
        if not stop_result["ok"]:
            return stop_result
        return await start_runtime(runtime, host=host)

    if runtime_type == "unsloth_porsche":
        stop_result = await stop_runtime(runtime, host=host)
        if not stop_result["ok"]:
            return stop_result
        return await start_runtime(runtime, host=host)

    return {"ok": False, "message": f"Unbekannter runtime_type: {runtime_type}"}


async def wake_runtime(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Wake a power_managed runtime's host via Wake-on-LAN.

    The backend runs in Docker and cannot send an L2 broadcast magic packet, so
    it drops a trigger file into settings.wake_request_dir (under the ~/.mc host
    bind-mount). A launchd watcher on the Mac host picks it up and runs the wake
    script (skills/wake-porsche/wake_porsche.py). See the WoL host-helper docs.

    host: resolved host (ADR-048) — registry values (power_managed, MAC, IP)
    take precedence, legacy runtime fields + settings remain as fallback.

    Returns {"ok": bool, "message": str}.
    """
    import datetime as _dt

    if not (runtime.get("power_managed") or (host is not None and host.power_managed)):
        return {"ok": False, "message": "Runtime ist nicht power_managed — kein Wake-on-LAN."}
    mac = (
        (host.wol_mac_address if host else None)
        or runtime.get("wol_mac_address")
        or settings.porsche_mac
    )
    if not mac:
        return {"ok": False, "message": "Keine wol_mac_address konfiguriert."}

    slug = runtime.get("slug") or runtime.get("id") or "runtime"
    safe_slug = re.sub(r"[^A-Za-z0-9._-]", "_", str(slug))
    payload = {
        "slug": slug,
        "mac": mac,
        # Registry-first like mac above — the legacy runtime.host field is
        # still set for bound runtimes and must not override a host-row edit
        # (box moved, IP maintained in the hosts UI).
        "ip": (host.ssh_host if host else None)
        or runtime.get("host")
        or settings.porsche_lan_ip,
        "broadcast": settings.porsche_broadcast,
        "requested_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    try:
        wake_dir = Path(settings.wake_request_dir)
        wake_dir.mkdir(parents=True, exist_ok=True)
        target = wake_dir / f"{safe_slug}.request.json"
        target.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Wake-on-LAN Trigger geschrieben: %s (mac=%s)", target, mac)
        return {
            "ok": True,
            "message": f"Wake-on-LAN ausgelöst für {runtime.get('display_name', slug)}. "
            "Box braucht ~1-2 Min bis erreichbar.",
        }
    except Exception as e:
        logger.error("Wake-Trigger schreiben fehlgeschlagen für %s: %s", slug, e)
        return {"ok": False, "message": f"Konnte Wake-Trigger nicht schreiben: {e}"}


# ── LM Studio Dynamic Model Discovery ────────────────────────────────────────

_LMS_CLI = "~/.lmstudio/bin/lms"


async def lms_unload_all(host: ResolvedHost | None = None) -> dict:
    """Unloads all models in LM Studio (lms unload --all).

    Returns: {"ok": bool, "message": str}
    """
    try:
        _, stderr, exit_code = await _ssh_run(f"{_LMS_CLI} unload --all 2>&1", host=host)
        if exit_code == 0:
            logger.info("Alle LM Studio Modelle entladen")
            return {"ok": True, "message": "Alle Modelle entladen."}
        return {"ok": False, "message": stderr or f"lms unload --all schlug fehl (exit {exit_code})"}
    except Exception as e:
        logger.error("lms_unload_all fehlgeschlagen: %s", e)
        return {"ok": False, "message": f"SSH-Fehler: {e}"}


async def lms_get_loaded_models(host: ResolvedHost | None = None) -> list[str]:
    """Returns the IDs of all models currently loaded in LM Studio (via lms ps --json).

    Returns: list of model IDs, e.g. ["nvidia/nemotron-3-super", "text-embedding-nomic-embed-text-v1.5"]
    """
    import json as _json
    try:
        stdout, _, exit_code = await _ssh_run(f"{_LMS_CLI} ps --json 2>/dev/null", host=host)
        raw = stdout.strip()
        if exit_code != 0 or not raw:
            return []
        # Parse JSON
        data = _json.loads(raw)
        if not isinstance(data, list):
            return []
        models = [item["modelKey"] for item in data if isinstance(item, dict) and "modelKey" in item]
        logger.info("Aktuell geladene LMS Modelle: %s", models)
        return models
    except _json.JSONDecodeError:
        # Fallback: parse text table (first column = identifier, skip header)
        logger.warning("lms ps --json nicht verfügbar, parse Text-Output")
        models = []
        for line in stdout.splitlines():
            parts = line.split()
            if parts and "/" in parts[0] and parts[0] != "IDENTIFIER":
                models.append(parts[0])
        logger.info("Aktuell geladene LMS Modelle (text): %s", models)
        return models
    except Exception as e:
        logger.error("lms_get_loaded_models fehlgeschlagen: %s", e)
        return []


async def lms_load_by_id(
    model_id: str,
    context_length: int | None = None,
    host: ResolvedHost | None = None,
) -> dict:
    """Loads a model in LM Studio by ID (not via runtime configuration).

    Returns: {"ok": bool, "message": str}
    """
    try:
        ctx_flag = f" --context-length {int(context_length)}" if context_length else ""
        # lms load blocks until the model is in VRAM — large models can take >60s.
        _, stderr, exit_code = await _ssh_run(
            f"{_LMS_CLI} load {model_id} --yes{ctx_flag} 2>&1", host=host, timeout=300
        )
        if exit_code == 0:
            logger.info("LM Studio Modell geladen (by ID): %s (ctx=%s)", model_id, context_length or "default")
            return {"ok": True, "message": f"{model_id} geladen."}
        return {"ok": False, "message": stderr or f"lms load schlug fehl (exit {exit_code})"}
    except Exception as e:
        logger.error("lms_load_by_id fehlgeschlagen für %s: %s", model_id, e)
        return {"ok": False, "message": f"SSH-Fehler: {e}"}


def _parse_lms_ls(stdout: str) -> list[dict]:
    """Parses the output of `lms ls` — returns LLM and embedding models."""
    models = []
    current_section: str | None = None  # "llm" | "embedding" | None

    for line in stdout.splitlines():
        stripped = line.strip()

        if not stripped or stripped.startswith("You have"):
            continue
        if stripped.startswith("LLM"):
            current_section = "llm"
            continue
        if stripped.startswith("EMBEDDING"):
            current_section = "embedding"
            continue
        # Skip column header line
        if stripped.startswith("PARAMS") or stripped.startswith("SIZE"):
            continue

        if current_section is None:
            continue

        is_loaded = "✓ LOADED" in line

        # Size: MB or GB
        size_gb = 0.0
        size_match = re.search(r"([\d.]+)\s+GB", line)
        if size_match:
            size_gb = float(size_match.group(1))
        else:
            mb_match = re.search(r"([\d.]+)\s+MB", line)
            if mb_match:
                size_gb = float(mb_match.group(1)) / 1024

        # Model name: everything up to the first block of 3+ spaces
        name_match = re.match(r"^(\S.*?)\s{3,}", line)
        if not name_match:
            continue
        raw_name = name_match.group(1).strip()

        # Remove "(X variant(s))" suffix
        model_id = re.sub(r"\s+\(\d+\s+variants?\)\s*$", "", raw_name).strip()

        models.append({
            "id": model_id,
            "display_name": model_id,
            "size_gb": size_gb,
            "is_loaded": is_loaded,
            "is_embedding": current_section == "embedding",
        })

    return models


async def list_lms_models(host: ResolvedHost | None = None) -> list[dict]:
    """Returns all LLM models installed in LM Studio."""
    try:
        stdout, _, _ = await _ssh_run(f"{_LMS_CLI} ls 2>/dev/null", host=host)
        return _parse_lms_ls(stdout)
    except Exception as e:
        logger.warning("lms ls fehlgeschlagen: %s", e)
        return []


async def lms_download_model(
    model_id: str,
    quantization: str | None = None,
    host: ResolvedHost | None = None,
) -> dict:
    """Starts an LM Studio model download in the background.

    model_id: HuggingFace model ID (e.g. lmstudio-community/gemma-4-31b-it-gguf)
    quantization: optional quantization (e.g. q4_k_m) → lms get name@quant
    """
    # lms get expects short names, not HuggingFace paths.
    short_name = model_id.split("/")[-1]
    short_name = re.sub(r"-gguf$", "", short_name, flags=re.IGNORECASE)
    if quantization:
        short_name = f"{short_name}@{quantization.lower()}"
    safe_id = (model_id + (f"_{quantization}" if quantization else "")).replace("/", "_").replace(" ", "_")
    log_path = f"/tmp/lms-download-{safe_id}.log"
    command = f"nohup {_LMS_CLI} get '{short_name}' --yes > {log_path} 2>&1 &"
    try:
        await _ssh_run(command, host=host)
        logger.info("LMS Download gestartet: %s", model_id)
        return {
            "ok": True,
            "message": f"Download gestartet. '{model_id}' erscheint in der Liste wenn fertig.",
        }
    except Exception as e:
        logger.error("LMS Download fehlgeschlagen für %s: %s", model_id, e)
        return {"ok": False, "message": f"SSH-Fehler: {e}"}


async def lms_delete_model(model_id: str, host: ResolvedHost | None = None) -> dict:
    """Deletes a model from LM Studio (via rm -rf on the model directory)."""
    try:
        model_name = model_id.split("/")[-1]
        find_out, _, _ = await _ssh_run(
            f"find ~/.lmstudio/models -maxdepth 2 -type d -iname '*{model_name}*' 2>/dev/null",
            host=host,
        )
        dirs = [d.strip() for d in find_out.strip().splitlines() if d.strip()]
        if not dirs:
            return {"ok": False, "message": f"Modell '{model_id}' nicht gefunden."}
        for d in dirs:
            await _ssh_run(f"rm -rf '{d}'", host=host)
        logger.info("LMS Modell gelöscht: %s → %s", model_id, dirs)
        return {"ok": True, "message": f"'{model_id}' wurde gelöscht."}
    except Exception as e:
        logger.error("LMS Delete fehlgeschlagen für %s: %s", model_id, e)
        return {"ok": False, "message": f"SSH-Fehler: {e}"}


# ── DGX Spark Hardware Metrics ────────────────────────────────────────────────

_SPARK_METRICS_CMD = (
    "nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu"
    " --format=csv,noheader,nounits && echo '---' && free -m"
)

_SPARK_UNREACHABLE: dict = {
    "reachable": False,
    "gpu_util_pct": None,
    "vram_used_mb": None,
    "vram_total_mb": None,
    "gpu_temp_c": None,
    "ram_used_mb": None,
    "ram_total_mb": None,
}


def _parse_spark_metrics(stdout: str) -> dict:
    """Parses combined nvidia-smi + free -m output."""
    try:
        parts = stdout.split("---", 1)
        if len(parts) != 2:
            return dict(_SPARK_UNREACHABLE)

        # nvidia-smi: "47, 88064, 131072, 62" — unified memory devices (e.g. GB10) return "[N/A]"
        def _int_or_none(s: str) -> int | None:
            s = s.strip().strip("[]")
            return int(s) if s.lstrip("-").isdigit() else None

        gpu_parts = [p.strip() for p in parts[0].strip().split(",")]
        gpu_util_pct = _int_or_none(gpu_parts[0])
        vram_used_mb = _int_or_none(gpu_parts[1])
        vram_total_mb = _int_or_none(gpu_parts[2])
        gpu_temp_c = _int_or_none(gpu_parts[3])

        # free -m: line starting with "Mem:"
        ram_used_mb = None
        ram_total_mb = None
        for line in parts[1].splitlines():
            if line.startswith("Mem:"):
                cols = line.split()
                ram_total_mb = int(cols[1])
                ram_used_mb = int(cols[2])
                break

        if ram_total_mb is None:
            return dict(_SPARK_UNREACHABLE)

        return {
            "reachable": True,
            "gpu_util_pct": gpu_util_pct,
            "vram_used_mb": vram_used_mb,
            "vram_total_mb": vram_total_mb,
            "gpu_temp_c": gpu_temp_c,
            "ram_used_mb": ram_used_mb,
            "ram_total_mb": ram_total_mb,
        }
    except (ValueError, IndexError):
        return dict(_SPARK_UNREACHABLE)


async def get_host_metrics(host: ResolvedHost | None) -> dict:
    """Fetches live hardware metrics for a host (ADR-048, generic).

    - kind ``ssh``      → nvidia-smi + free -m via SSH (same parsing logic
      as the old get_spark_metrics — now per host instead of hardcoded DGX).
    - kind ``flask_wol`` → no SSH channel: health status of the control server
      instead of GPU metrics (reachable = box awake + :5555 responds).
    - kind ``local``    → no metrics (the MC host doesn't measure itself).
    - host=None         → settings fallback in _ssh_run (classic single-box).
    """
    if host is not None and host.kind == "flask_wol":
        reachable = bool(host.control_url) and await _porsche_reachable(host.control_url)
        return {**dict(_SPARK_UNREACHABLE), "reachable": reachable}
    if host is not None and host.kind == "local":
        return dict(_SPARK_UNREACHABLE)
    try:
        stdout, _, _ = await _ssh_run(_SPARK_METRICS_CMD, host=host)
        return _parse_spark_metrics(stdout)
    except Exception as e:
        logger.warning(
            "Host-Metriken nicht abrufbar (%s): %s",
            (host.slug or host.ssh_host) if host else "settings-fallback", e,
        )
        return dict(_SPARK_UNREACHABLE)


async def get_spark_metrics() -> dict:
    """Fetches live hardware metrics from the DGX Spark via SSH.

    Back-compat wrapper (ADR-048): delegates to get_host_metrics() with the
    settings fallback host — the seeded `dgx-spark` registry host carries
    the same values. New callers use get_host_metrics(resolved_host).
    """
    return await get_host_metrics(settings_fallback_host())


# ── Model Catalog — HuggingFace API ──────────────────────────────────────────


async def search_lmstudio_catalog(query: str) -> list[dict]:
    """Searches the LM Studio catalog (lmstudio-community on HuggingFace)."""
    if not query.strip():
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://huggingface.co/api/models",
                params={"search": query, "filter": "gguf", "author": "lmstudio-community", "limit": 20, "blobs": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
        results = []
        for m in data:
            model_id = m.get("modelId", "")
            name = model_id.split("/")[-1] if "/" in model_id else model_id
            params_val = next(
                (tag for tag in m.get("tags", []) if tag.endswith("B") and tag[:-1].replace(".", "").isdigit()),
                None,
            )
            gguf_sizes = [
                s.get("size", 0)
                for s in m.get("siblings", [])
                if s.get("rfilename", "").endswith(".gguf")
            ]
            size_gb = round(min(gguf_sizes) / 1024**3, 1) if gguf_sizes else None
            results.append({"model_id": model_id, "name": name, "params": params_val, "size_gb": size_gb})
        return results
    except Exception as e:
        logger.warning("LM Studio Catalog Suche fehlgeschlagen: %s", e)
        return []


async def get_hf_repo_files(repo_id: str) -> dict:
    """Fetches all GGUF files of a HuggingFace repo."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"https://huggingface.co/api/models/{repo_id}?blobs=true")
            if resp.status_code == 404:
                return {"error": "Repo nicht gefunden"}
            resp.raise_for_status()
            data = resp.json()
        files = [
            {"filename": s["rfilename"], "size_gb": round(s.get("size", 0) / 1024**3, 1)}
            for s in data.get("siblings", [])
            if s.get("rfilename", "").endswith(".gguf")
        ]
        if not files:
            return {"error": "Keine GGUF-Dateien in diesem Repo gefunden"}
        name = repo_id.split("/")[-1] if "/" in repo_id else repo_id
        return {"repo_id": repo_id, "name": name, "files": files}
    except Exception as e:
        logger.warning("HF Repo Abfrage fehlgeschlagen für %s: %s", repo_id, e)
        return {"error": f"Fehler: {e}"}


async def download_hf_file(
    repo_id: str, filename: str, host: ResolvedHost | None = None
) -> dict:
    """Downloads a GGUF file from HuggingFace directly into the LM Studio models directory."""
    author, _, model_name = repo_id.partition("/")
    dest_dir = f"~/.lmstudio/models/{author}/{model_name}"
    safe_name = (repo_id + "_" + filename).replace("/", "_").replace(" ", "_")
    log_path = f"/tmp/hf-download-{safe_name}.log"
    command = (
        f"mkdir -p {dest_dir} && "
        f"nohup curl -L 'https://huggingface.co/{repo_id}/resolve/main/{filename}' "
        f"-o '{dest_dir}/{filename}' "
        f"> {log_path} 2>&1 &"
    )
    try:
        await _ssh_run(command, host=host)
        logger.info("HF Download gestartet: %s / %s", repo_id, filename)
        return {"ok": True, "message": f"Download gestartet. '{filename}' erscheint in LM Studio wenn fertig."}
    except Exception as e:
        logger.error("HF Download fehlgeschlagen %s/%s: %s", repo_id, filename, e)
        return {"ok": False, "message": f"SSH-Fehler: {e}"}


async def get_active_downloads(host: ResolvedHost | None = None) -> list[dict]:
    """Returns all active downloads (lms get + HF curl), deduplicated."""
    results: list[dict] = []
    seen_ids: set[str] = set()

    # ── lms get processes — only real lms binaries, not bash wrappers ──
    try:
        ps_out, _, _ = await _ssh_run(
            "ps aux | grep '[l]ms get' | grep -v 'bash -c' 2>/dev/null", host=host
        )
        for line in ps_out.strip().splitlines():
            m = re.search(r"lms get\s+['\"]?([^\s'\"]+)['\"]?", line)
            if not m:
                continue
            model_id = m.group(1)
            entry_id = f"lms-{model_id}"
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            safe_id = model_id.replace("/", "_").replace(" ", "_")
            log_path = f"/tmp/lms-download-{safe_id}.log"
            log_out, _, _ = await _ssh_run(f"tail -1 '{log_path}' 2>/dev/null", host=host)
            last_line = log_out.strip()
            pct_match = re.search(r"(\d+)%", last_line)
            pct = int(pct_match.group(1)) if pct_match else None
            results.append({
                "id": entry_id,
                "name": model_id,
                "type": "lmstudio",
                "progress_pct": pct,
                "progress_text": last_line or "Lädt...",
            })
    except Exception as e:
        logger.warning("Download-Check (lms) fehlgeschlagen: %s", e)

    # ── HF curl processes ──
    try:
        ps_out, _, _ = await _ssh_run("ps aux | grep '[c]url' | grep 'huggingface' 2>/dev/null", host=host)
        for line in ps_out.strip().splitlines():
            m_dest = re.search(r"-o\s+'([^']+)'", line)
            m_repo = re.search(r"huggingface\.co/([^/]+/[^/]+)/resolve", line)
            if not m_dest:
                continue
            dest = m_dest.group(1)
            filename = dest.split("/")[-1]
            entry_id = f"hf-{filename}"
            if entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            repo = m_repo.group(1) if m_repo else ""
            size_out, _, _ = await _ssh_run(f"stat -c%s '{dest}' 2>/dev/null || echo 0", host=host)
            size_bytes = int(size_out.strip() or 0)
            size_gb = round(size_bytes / 1024**3, 2)
            results.append({
                "id": entry_id,
                "name": filename,
                "type": "huggingface",
                "repo": repo,
                "progress_pct": None,
                "progress_text": f"{size_gb} GB geladen" if size_bytes > 0 else "Verbinde...",
            })
    except Exception as e:
        logger.warning("Download-Check (HF) fehlgeschlagen: %s", e)

    return results


async def list_db_runtimes(session: AsyncSession) -> list[Runtime]:
    """Returns all runtime rows from the DB, sorted by ui_order.

    Phase 16 (D-03): replaces load_registry() as the primary data path for
    GET /runtimes. load_registry() is kept for the bootstrap seed (D-02)
    and is called by the main.py lifespan + migration 0094.
    """
    result = await session.exec(select(Runtime).order_by(Runtime.ui_order))
    return list(result.all())


async def cancel_download(model_name: str, host: ResolvedHost | None = None) -> dict:
    """Cancels a running download (pkill + clean up log)."""
    try:
        # Kill all processes that have this model name in the lms get command
        await _ssh_run(f"pkill -f \"lms get '?{re.escape(model_name)}'?\" 2>/dev/null; true", host=host)
        # Remove log file
        safe_id = model_name.replace("/", "_").replace(" ", "_")
        await _ssh_run(f"rm -f /tmp/lms-download-{safe_id}.log 2>/dev/null; true", host=host)
        logger.info("Download abgebrochen: %s", model_name)
        return {"ok": True, "message": f"Download '{model_name}' abgebrochen."}
    except Exception as e:
        logger.error("Cancel fehlgeschlagen für %s: %s", model_name, e)
        return {"ok": False, "message": f"Fehler: {e}"}
