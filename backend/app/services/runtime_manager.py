"""
RuntimeManager — manages local model runtimes via SSH + Docker / LM Studio CLI.

Supported runtime_type:
- vllm_docker: Docker container on DGX Spark, controllable via SSH docker commands
- llamacpp_docker: llama.cpp's `llama-server` in the official ggml-org image —
  same lifecycle as vllm_docker (SSH + docker, OpenAI-compatible endpoint), but
  for small GGUF models and with `/health` instead of `/v1/models` as the
  default probe. The image is multi-arch, so the identical tag runs on the
  ARM64 DGX Spark and on an x86 box.
- lmstudio: single model in LM Studio, controllable via SSH lms load/unload
- unsloth: Unsloth Studio (FastAPI web UI) in a tmux session on the host,
  controllable via SSH tmux new-/kill-session. No Docker because no ARM64 image.

State detection for vllm_docker / llamacpp_docker:
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
from app.services import address_classify, host_memory_prep, runtime_grace, runtime_ownership
from app.utils import ensure_aware, utcnow
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

# runtime_types that are "a docker container on a host, reachable over SSH,
# serving an OpenAI-compatible endpoint". They share all four lifecycle chains
# (state/start/stop/restart) — only the launch-verification and the default
# healthcheck path differ per engine.
DOCKER_ENGINE_TYPES = ("vllm_docker", "llamacpp_docker")

# A plain host process behind an OpenAI-compatible port, managed over SSH
# (PR 6). Not every engine is a container: ds4-server (DwarfStar 4, C/CUDA)
# ships start.sh/stop.sh and reads an asymmetric GGUF that vLLM cannot load at
# all. Kept generic instead of hardcoding that one engine — the next community
# server that installs itself and serves /v1 is the same shape.
#
# The handle is ``process_name`` (pgrep/pkill) the way ``container_name`` is
# the handle for the docker types.
SSH_PROCESS_TYPE = "ssh_process"

# Probe path used when a runtime row leaves healthcheck_path unset. vLLM and
# LM Studio answer /v1/models; llama-server has a dedicated /health that is 200
# only once the model is actually loaded (503 "Loading model" before that), so
# it distinguishes ready from warming more precisely than /v1/models would.
_DEFAULT_HEALTHCHECK_PATHS = {"llamacpp_docker": "/health"}
_FALLBACK_HEALTHCHECK_PATH = "/v1/models"


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
    classic settings fallback (ADR-048).

    Prefers the host's Tailscale address over ``ssh_host`` when one is on
    file (address_classify.preferred_endpoint_host) — the live incident this
    guards against: an endpoint built from a box's LAN IP answers SSH from
    the backend container fine but silently fails HTTP calls from a host
    agent when a Tailscale route hijacks that LAN IP on the Mac
    (spark-tailscale-route-hijack-host-agents).
    """
    if host is not None and (host.ssh_host or host.tailscale_host):
        return address_classify.preferred_endpoint_host(
            host.ssh_host, host.tailscale_host
        ) or ""
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

    Raises when the SSH transport itself fails (Paket 2): "I could not look"
    must stay distinguishable from "I looked and there is no process" — the
    launch verification treats the former as *unknown*, never as a confirmed
    failure. Callers that only need a best-effort answer wrap the call.
    """
    stdout, _, exit_code = await _ssh_run(
        f"docker top {container_name} -o cmd 2>/dev/null", host=host
    )
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
            try:
                is_vllm, endpoint = await _container_runs_vllm_server(name, host=host)
            except Exception as exc:  # noqa: BLE001 — best-effort discovery:
                # an unreadable container is simply not listed this round.
                logger.warning("docker top %s fehlgeschlagen: %s", name, exc)
                is_vllm, endpoint = False, ""
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


async def _load_vault_ssh_private_key(credential_id) -> str | None:
    """Decrypts a Credential(credential_type='ssh_key')'s private_key_pem.

    Opens its own short-lived session (Phase 2 Auto-Onboarding) — ResolvedHost
    stays session-free by design (host_resolver.py docstring), and _ssh_run is
    called from background jobs as often as from request handlers, so there is
    no request-scoped session to reuse here. Same pattern as other services
    managing their own session lifecycle (app.database.async_session_maker).
    Returns None on any problem (missing row, undecryptable data, wrong
    shape) — the caller falls back to ssh_key_path/settings, never crashes.
    """
    import json

    from app.database import engine
    from app.models.credential import Credential
    from app.services.encryption import safe_decrypt

    async with AsyncSession(engine, expire_on_commit=False) as session:
        credential = await session.get(Credential, credential_id)
    if not credential:
        return None
    decrypted = safe_decrypt(credential.encrypted_data)
    if not decrypted:
        return None
    try:
        data = json.loads(decrypted)
    except (json.JSONDecodeError, TypeError):
        return None
    pem = data.get("private_key_pem")
    return pem if isinstance(pem, str) and pem.strip() else None


async def _resolve_ssh_client_keys(target: ResolvedHost) -> list:
    """Fallback chain (Phase 2): Vault credential → ssh_key_path → settings.

    The credential's key is imported straight into memory
    (asyncssh.import_private_key) — it never touches disk, unlike a tempfile.
    """
    if target.ssh_credential_id is not None:
        pem = await _load_vault_ssh_private_key(target.ssh_credential_id)
        if pem:
            try:
                return [asyncssh.import_private_key(pem)]
            except asyncssh.KeyImportError as e:
                logger.warning(
                    "Host '%s': Vault-Key (Credential %s) unlesbar (%s) — falle auf ssh_key_path zurück.",
                    target.slug or target.ssh_host, target.ssh_credential_id, e,
                )
    return [target.ssh_key_path or settings.dgx_ssh_key_path]


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

    Key resolution (Phase 2 Auto-Onboarding): host.ssh_credential_id (Vault,
    Fernet-encrypted) → host.ssh_key_path → settings.dgx_ssh_key_path. Hosts
    onboarded before Phase 2 have no credential and are unaffected — this
    only ever ADDS a first-choice source, the old chain stays the fallback.
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
        client_keys=await _resolve_ssh_client_keys(target),
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


def join_probe_url(endpoint: str, healthcheck_path: str) -> str:
    """Joins runtime endpoint + healthcheck path without duplicating "/v1".

    Registry endpoints are OpenAI-compatible base URLs and are conventionally
    stored *with* the version segment (".../v1"), while the default healthcheck
    path is "/v1/models". Concatenating them yields ".../v1/v1/models" — a 404
    that makes a perfectly healthy runtime report as stopped. Observed live on
    the Spark runtime, whose engine logged a steady stream of
    `GET /v1/v1/models 404` while serving fine.

    Mirrors the normalization in agent_runtime_switch.probe_runtime_model.
    """
    base = endpoint.rstrip("/")
    path = healthcheck_path or "/v1/models"
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith("/v1") and (path == "/v1" or path.startswith("/v1/")):
        path = path[len("/v1"):] or "/models"
    return base + path


async def _probe_http(endpoint: str, healthcheck_path: str) -> bool:
    """Checks whether the runtime's HTTP endpoint responds."""
    url = join_probe_url(endpoint, healthcheck_path)
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
#
# Task #22 (live 08./09.08.2026): a qwen sparkrun wrapper kept running,
# invisible to every matcher above, while DeepSeek started — "box already
# free" was a false all-clear. Root cause: a compose service with no
# `container_name:` gets a docker-generated name that matches neither the
# label nor either name filter. Two independent hardenings close this:
#   - discovery additionally sweeps by `com.docker.compose.project` (every
#     compose-managed container carries this regardless of container_name —
#     see `runtime_ownership.COMPOSE_PROJECT_NAME_PATTERN` for the exact,
#     necessarily heuristic, project-name match and why it can't be
#     precise — sparkrun's internal compose invocation is not something MC's
#     registry knows the project name of).
#   - before any of the found containers are actually stopped, ownership is
#     verified via `runtime_ownership.partition_by_ownership` (the
#     mc.runtime.nonce label, stamped at launch — see that module's
#     docstring). A container that claims MC's slug label but doesn't carry
#     (or doesn't match) the nonce MC expects for that slug was not created
#     by this MC instance — most likely hand-recreated by an operator under
#     the same name/label — and is left running with a warning event instead
#     of being force-stopped.
#   - every eviction call now logs which matcher found what, and how many
#     containers were running on the box in total, so a false "box already
#     free" is diagnosable from the logs alone (P3/visibility).

# Module-level so tests can monkeypatch them to 0 for fast polling.
_evict_poll_interval = 1.0   # seconds between "is it free yet?" polls
_verify_poll_interval = 1.0  # seconds between "did it appear?" polls

# Matches sparkrun's single-model container naming: sparkrun_<hash>_solo.
_SOLO_NAME_FILTER = "name=sparkrun_.*_solo"
# The canonical name used by MANUAL engine starts on the Spark
# (spark-vllm-docker/run-recipe.py names its container `vllm_node`). Without
# this sweep the cockpit stop button reports success but stops nothing when
# the engine was started by hand (live failure 2026-07-05: manual PrismaQuant
# survived both the stop button and a recipe switch to DeepSeek).
_MANUAL_NAME_FILTER = "name=vllm_node"
# Every container docker compose creates carries this label, independent of
# whether the service set `container_name:` — the discovery net for Task #22.
_COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def _sanitize_slug(slug: str) -> str:
    """Collapse anything non-slug-ish to '_'. Defensive — slugs already pass DB
    constraints, but eviction commands interpolate the slug into a docker label
    filter, so we never want raw user input there."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(slug or ""))


def _running_solo_query(slug: str | None = None) -> str:
    """Build a single remote command that prints the ids of running Spark model
    containers — both label-matched (``mc.runtime.slug=<slug>``) and
    ``sparkrun_*_solo`` name-matched, plus the manual-start name
    ``vllm_node`` — deduplicated. One SSH round-trip.

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
        f"docker ps -q --filter {shlex_quote(_SOLO_NAME_FILTER)}; "
        f"docker ps -q --filter {shlex_quote(_MANUAL_NAME_FILTER)}; }} | sort -u"
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


async def _labelled_containers(
    slug: str | None, *, host: ResolvedHost | None = None
) -> list[str]:
    """Ids of running containers carrying ``mc.runtime.slug=<slug>``, nothing else.

    The narrow counterpart to ``_running_solo_containers`` for engines that
    share a box instead of owning it (llamacpp_docker). Raises like that one so
    a docker/SSH failure reads as *unknown*, never as "nothing is running".
    """
    safe = _sanitize_slug(slug) if slug else None
    if not safe:
        return []
    out, err, ec = await _ssh_run(
        f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(safe)}",
        host=host,
        timeout=20,
    )
    if ec != 0:
        raise RuntimeError(err or f"docker ps query failed (exit {ec})")
    return sorted({x for x in out.splitlines() if x.strip()})


def _eviction_discovery_script(slug: str | None, container_name: str | None = None) -> str:
    """One remote script reporting, in marked sections: how many containers
    are running on the box in total, and the ids each matcher found — label,
    solo-name, manual-name, compose-project. One SSH round trip for all four,
    so the P3 diagnostics (which matcher ran, how many it found) cost nothing
    extra over the discovery itself.

    The compose-project section prints ``id|project`` pairs (not just ids):
    the project-name pattern match happens client-side in
    ``_parse_eviction_discovery`` (see ``runtime_ownership.
    COMPOSE_PROJECT_NAME_PATTERN`` for why an unscoped
    ``--filter label=com.docker.compose.project`` would be too broad — it
    would match every compose stack on the box, not just Spark model ones).
    """
    label_cmd = (
        f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(_sanitize_slug(slug))}"
        if slug
        else "true"
    )
    project_fmt = '{{.ID}}|{{.Label "' + _COMPOSE_PROJECT_LABEL + '"}}'
    # Task #29 (2× live gebissen): a recipe container without the
    # ``mc.runtime.slug`` label patch is invisible to the label matcher, but
    # the runtime ROW knows its exact name — list id|name pairs and match the
    # exact name client-side (`docker ps --filter name=` is substring-ish and
    # would over-match e.g. `my-engine-2` for `my-engine`).
    script = (
        "echo __TOTAL__; docker ps -q | wc -l; "
        f"echo __LABEL__; {label_cmd}; "
        f"echo __SOLO__; docker ps -q --filter {shlex_quote(_SOLO_NAME_FILTER)}; "
        f"echo __MANUAL__; docker ps -q --filter {shlex_quote(_MANUAL_NAME_FILTER)}; "
        f"echo __PROJECT__; docker ps --filter label={_COMPOSE_PROJECT_LABEL} "
        f"--format {shlex_quote(project_fmt)}; "
        "echo __NAME__; docker ps --format '{{.ID}}|{{.Names}}'"
    )
    return f"bash -o pipefail -c {shlex_quote(script)}"


def _parse_eviction_discovery(out: str, container_name: str | None = None) -> dict:
    """Parses ``_eviction_discovery_script`` output into per-matcher id lists
    plus the box-wide total — the raw material for both the stop decision and
    the P3 diagnostics logging."""
    markers = ("__TOTAL__", "__LABEL__", "__SOLO__", "__MANUAL__", "__PROJECT__",
               "__NAME__")
    sections: dict[str, list[str]] = {m: [] for m in markers}
    current: str | None = None
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line in sections:
            current = line
            continue
        if current is not None:
            sections[current].append(line)

    total = 0
    if sections["__TOTAL__"]:
        try:
            total = int(sections["__TOTAL__"][0])
        except ValueError:
            total = 0

    project_ids: list[str] = []
    for entry in sections["__PROJECT__"]:
        cid, _, project = entry.partition("|")
        cid = cid.strip()
        if cid and runtime_ownership.COMPOSE_PROJECT_NAME_PATTERN.search(project):
            project_ids.append(cid)

    name_ids: list[str] = []
    wanted = (container_name or "").strip()
    if wanted:
        for entry in sections["__NAME__"]:
            cid, _, cname = entry.partition("|")
            cid, cname = cid.strip(), cname.strip()
            # Exact match only — `my-engine` must not sweep `my-engine-2`.
            if cid and cname == wanted:
                name_ids.append(cid)

    label_ids = sorted({x for x in sections["__LABEL__"] if x})
    solo_ids = sorted({x for x in sections["__SOLO__"] if x})
    manual_ids = sorted({x for x in sections["__MANUAL__"] if x})
    project_ids = sorted(set(project_ids))
    name_ids = sorted(set(name_ids))

    return {
        "total": total,
        "label": label_ids,
        "solo": solo_ids,
        "manual": manual_ids,
        "project": project_ids,
        "name": name_ids,
        "all": sorted(
            set(label_ids) | set(solo_ids) | set(manual_ids)
            | set(project_ids) | set(name_ids)
        ),
    }


async def _still_running(container_ids: list[str], *, host: ResolvedHost | None = None) -> list[str]:
    """Which of ``container_ids`` are still running — used to poll the
    specific containers eviction told docker to stop, not the whole box (a
    container eviction deliberately left alone, see ownership below, must
    not make the poll spin to a false timeout)."""
    ids = [c for c in container_ids if c and c.strip()]
    if not ids:
        return []
    filters = " ".join(f"--filter id={shlex_quote(c)}" for c in ids)
    out, err, ec = await _ssh_run(f"docker ps -q {filters}", host=host, timeout=20)
    if ec != 0:
        raise RuntimeError(err or f"docker ps query failed (exit {ec})")
    return sorted({x for x in out.splitlines() if x.strip()})


async def _emit_ownership_blocked_event(slug: str | None, blocked: list[dict]) -> None:
    """Records that eviction found containers it refused to stop. Best-effort
    — a failing event must never hide the (already-returned) block itself."""
    try:
        from app.services.activity import emit_event
        from app.services.runtime_model_resolver import session_scope

        async with session_scope() as session:
            await emit_event(
                session,
                "runtime.eviction_ownership_blocked",
                f"{slug}: {len(blocked)} Container nicht gestoppt (Besitz nicht bewiesen) — "
                + "; ".join(f"{b['container_id'][:12]} ({b['reason']})" for b in blocked),
                severity="warning",
                detail={"slug": slug, "blocked": blocked},
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ownership-blocked event emit failed for %s: %s", slug, exc)


async def evict_spark_runtime_containers(
    slug: str | None,
    *,
    container_name: str | None = None,
    host: ResolvedHost | None = None,
    timeout: float = 30.0,
) -> dict:
    """Stop every running Spark model container MC can prove is its own,
    then wait until those are gone.

    Host-scoped (ADR-048): all docker commands run on ``host`` — the resolved
    host of the *starting* runtime — so an eviction for box A never stops
    models on box B. ``host=None`` keeps the classic settings.dgx_ssh_* box.

    Three phases (Task #22 hardening on top of the original P0/P1):

    1. **Discovery** (``_eviction_discovery_script``) — every matcher in one
       SSH round trip: label (``mc.runtime.slug=<slug>``), the
       ``sparkrun_*_solo`` name sweep, the manual ``vllm_node`` name, and now
       a ``com.docker.compose.project`` sweep so a compose service with no
       ``container_name:`` (previously invisible — the live incident) is
       found too. The matcher breakdown and the box-wide container total are
       always logged, so a "nothing found" result is diagnosable from the
       logs alone instead of just trusted.
    2. **Ownership** (``runtime_ownership.partition_by_ownership``) — a
       container that carries MC's own ``mc.runtime.slug`` label is only
       stopped if its ``mc.runtime.nonce`` label matches what MC recorded at
       launch time. A mismatch means the container was not created by this
       MC instance (most likely hand-recreated under the same name/label)
       and is left running with a ``runtime.eviction_ownership_blocked``
       event instead of being force-stopped — "never stop what we cannot
       prove is ours". Containers with NO slug label at all (the whole
       reason the solo/manual/project sweeps exist) carry no ownership claim
       to disprove and are always eligible to stop.
    3. **Stop + poll** — only the containers cleared in step 2 are stopped;
       the poll then waits for exactly those ids to disappear (bounded by
       ``timeout``), not the whole box, so a container eviction deliberately
       left alone doesn't spin the poll to a false timeout.

    A non-empty ``blocked`` list makes the overall result ``ok=False`` — an
    unresolved, still-running container means the box is not actually free,
    and the caller (``ensure_exclusive_host`` / ``switch_recipe``) must not
    launch a second model on top of it.

    Returns ``{"ok": bool, "message": str, "stopped": [ids], "blocked": [...]}``.
    """
    import asyncio

    safe = _sanitize_slug(slug) if slug else None

    try:
        discovery_out, discovery_err, discovery_ec = await _ssh_run(
            _eviction_discovery_script(safe, container_name), host=host, timeout=20
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("evict: discovery raised for %s: %s", slug, exc)
        return {"ok": False, "message": f"Eviction-Discovery fehlgeschlagen: {exc}", "stopped": []}
    if discovery_ec != 0:
        logger.warning("evict: discovery exited %s for %s: %s", discovery_ec, slug, discovery_err)
        return {
            "ok": False,
            "message": f"Eviction-Discovery fehlgeschlagen (exit {discovery_ec}): {discovery_err}",
            "stopped": [],
        }

    found = _parse_eviction_discovery(discovery_out, container_name)
    logger.info(
        "evict: discovery for %s — total_on_box=%s label=%s solo=%s manual=%s "
        "compose_project=%s name=%s",
        slug, found["total"], found["label"], found["solo"], found["manual"],
        found["project"], found["name"],
    )

    if not found["all"]:
        message = (
            f"Spark freigegeben (nichts lief; {found['total']} Container insgesamt auf der Box, "
            f"keiner passte zu Label/Solo-Sweep/Manual-Sweep/Compose-Projekt)."
        )
        logger.info("evict: nothing found for %s (%s)", slug, message)
        return {"ok": True, "message": message, "stopped": [], "blocked": []}

    safe_to_stop, blocked = await runtime_ownership.partition_by_ownership(
        found["all"], host=host, ssh_run=_ssh_run
    )
    if blocked:
        for b in blocked:
            logger.warning(
                "evict: NOT stopping %s (slug=%s) for %s — %s",
                b["container_id"], b["slug"], slug, b["reason"],
            )
        await _emit_ownership_blocked_event(slug, blocked)

    stopped: list[str] = []
    if safe_to_stop:
        # `xargs -r`-equivalent via an explicit id list: empty is impossible
        # here (safe_to_stop is non-empty in this branch), but the ids are
        # still individually quoted — same injection posture as before.
        stop_cmd = "docker stop " + " ".join(shlex_quote(c) for c in safe_to_stop)
        try:
            stopped_out, stop_err, _ = await _ssh_run(stop_cmd, host=host, timeout=max(timeout, 30))
        except Exception as exc:  # noqa: BLE001 — surface as a clean failure
            logger.warning("evict: stop command raised for %s: %s", slug, exc)
            return {
                "ok": False,
                "message": f"Eviction-Stop fehlgeschlagen: {exc}",
                "stopped": [],
                "blocked": blocked,
            }
        stopped = [x for x in stopped_out.splitlines() if x.strip()]
        if stop_err:
            logger.info("evict: docker stop stderr for %s: %s", slug, stop_err)

    if blocked and not stopped:
        return {
            "ok": False,
            "message": (
                f"{len(blocked)} Container gefunden, aber keiner konnte als eigener bewiesen "
                f"werden (Nonce fehlt/stimmt nicht) — Stop verweigert, Box gilt als NICHT frei. "
                f"Details im Event-Feed (runtime.eviction_ownership_blocked)."
            ),
            "stopped": [],
            "blocked": blocked,
        }

    # P1 — poll until the containers we told docker to stop are actually gone.
    # Scoped to `safe_to_stop`, not the whole box: a blocked container stays
    # running on purpose and must not make this loop spin to a false timeout.
    deadline = asyncio.get_running_loop().time() + timeout
    remaining: list[str] = []
    while True:
        try:
            remaining = await _still_running(safe_to_stop, host=host)
        except Exception as exc:  # noqa: BLE001
            logger.warning("evict: poll raised for %s: %s — treating as still busy", slug, exc)
            remaining = ["<poll-error>"]
        if not remaining:
            if blocked:
                logger.error(
                    "evict: %s stopped, but %s container(s) blocked by ownership check for %s",
                    stopped, len(blocked), slug,
                )
                return {
                    "ok": False,
                    "message": (
                        f"Gestoppt: {stopped or 'nichts lief'}. Aber {len(blocked)} Container "
                        f"konnten nicht als eigener bewiesen werden und laufen weiter — Box gilt "
                        f"als NICHT frei. Details im Event-Feed."
                    ),
                    "stopped": stopped,
                    "blocked": blocked,
                }
            logger.info("evict: Spark free for %s (stopped=%s)", slug, stopped)
            return {
                "ok": True,
                "message": f"Spark freigegeben (gestoppt: {stopped or 'nichts lief'}).",
                "stopped": stopped,
                "blocked": [],
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
        "blocked": blocked,
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


async def verify_spark_vllm_process_started(
    slug: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float = 90.0,
) -> str:
    """Poll for an actual ``vllm serve`` process inside the labelled container.

    ``verify_spark_container_started`` only proves the container exists —
    some launches (sparkrun's solo-mode wrapper, or a manually-started
    container) keep a ``sleep infinity`` PID1 while vLLM runs as a separate
    process inside, injected out-of-band. A wrong ``--tensor-parallel``
    value, a bad recipe flag, or an immediate OOM can kill that process while
    the container itself stays "running" — the exact silent-failure mode
    behind ADR-059 (a recipe switch reported success while nothing was
    actually serving). This is the second half of the launch-verification: it
    reuses the same ``docker top`` process-scan ``_container_runs_vllm_server``
    already does for discovery, but polls briefly (not the full 2-5 min model
    warmup) right after launch so a dead-on-arrival process is caught early.

    Returns a tri-state instead of a bool (Paket 2, live-belegt 15.08.26 —
    beide Launches meldeten "kein vllm-Prozess", während die Box unter Last
    stand und SSH nur stockte):

    - ``"serving"`` — the process was seen,
    - ``"absent"``  — at least one CLEAN read happened (SSH answered, docker
      answered) and the process never showed inside ``timeout``: the
      confirmed ADR-059 failure,
    - ``"unknown"`` — every read inside the window errored (SSH reset, box
      stalling): *not verifiable* is not the same claim as *dead*. Callers
      report the start optimistically and leave the verdict to the watcher.
    """
    import asyncio

    safe = _sanitize_slug(slug)
    deadline = asyncio.get_running_loop().time() + timeout
    saw_clean_read = False
    while True:
        try:
            out, _, ec = await _ssh_run(
                f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(safe)}",
                host=host,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify-process: container lookup raised for %s: %s", slug, exc)
            out, ec = "", -1
        container_id = next((x for x in out.splitlines() if x.strip()), None)
        if ec == 0:
            if container_id:
                try:
                    is_vllm, _ = await _container_runs_vllm_server(container_id, host=host)
                    saw_clean_read = True
                    if is_vllm:
                        return "serving"
                except Exception as exc:  # noqa: BLE001
                    logger.warning("verify-process: docker top raised for %s: %s", slug, exc)
            else:
                # The box answered and there is (still/yet) no labelled
                # container — a clean observation, it just isn't a process hit.
                saw_clean_read = True
        if asyncio.get_running_loop().time() >= deadline:
            return "absent" if saw_clean_read else "unknown"
        if _verify_poll_interval:
            await asyncio.sleep(_verify_poll_interval)


async def _container_runs_llamacpp_server(
    container_name: str, *, host: ResolvedHost | None = None
) -> bool:
    """True when ``llama-server`` shows up in the container's process list.

    The llama.cpp analogue of ``_container_runs_vllm_server``, minus the
    endpoint reconstruction: llama-server is always started with an explicit
    ``--port`` that the runtime row already records in its endpoint, so there
    is nothing to infer.

    Uses plain ``docker top`` rather than ``docker top -o cmd``: the ``-o``
    form is rejected by Docker Desktop's ps shim ("Couldn't find PID field in
    ps output", reproduced 2026-08-05 on macOS/ARM64) while the bare form
    prints a CMD column on every platform. Matching the substring on any
    output line is safe — the header row is ``UID PID PPID …  CMD``.

    Raises on SSH transport failure (Paket 2) — same contract as
    ``_container_runs_vllm_server``: "could not look" must not read as
    "looked, nothing there".
    """
    stdout, _, exit_code = await _ssh_run(
        f"docker top {shlex_quote(container_name)} 2>/dev/null", host=host
    )
    if exit_code != 0:
        return False
    return any("llama-server" in line for line in stdout.splitlines())


async def verify_llamacpp_process_started(
    slug: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float = 90.0,
) -> str:
    """Poll for a live ``llama-server`` process inside the labelled container.

    The llamacpp counterpart to ``verify_spark_vllm_process_started`` (which
    matches on ``vllm serve`` and therefore never fires here).

    Why SSH/``docker top`` and NOT an HTTP poll on ``/health``: measured on
    2026-08-05 against ghcr.io/ggml-org/llama.cpp:server (b10276), llama-server
    binds its HTTP port only *after* the GGUF is fetched. For a 400 MB model
    the port was flatly refusing connections for the first ~9 s, answered 503
    "Loading model" at t+10 s and 200 at t+11 s. A weights fetch is unbounded
    (tens of GB over a slow link), so a bounded HTTP poll cannot tell "still
    downloading" from "crashed on startup" and would report a false failure for
    every genuinely slow launch. The process check is true throughout download
    AND load, and false exactly in the case worth catching: the container is up
    but the server died (bad flags, missing GGUF, OOM) — the same 0.4 s
    exit-on-bad-quant we reproduced locally.

    Same tri-state contract as ``verify_spark_vllm_process_started`` (Paket
    2): ``"serving"`` / ``"absent"`` (confirmed by clean reads) /
    ``"unknown"`` (every read inside the window errored).
    """
    import asyncio

    safe = _sanitize_slug(slug)
    deadline = asyncio.get_running_loop().time() + timeout
    saw_clean_read = False
    while True:
        try:
            out, _, ec = await _ssh_run(
                f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(safe)}",
                host=host,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify-llamacpp: container lookup raised for %s: %s", slug, exc)
            out, ec = "", -1
        container_id = next((x for x in out.splitlines() if x.strip()), None)
        if ec == 0:
            if container_id:
                try:
                    if await _container_runs_llamacpp_server(container_id, host=host):
                        return "serving"
                    saw_clean_read = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "verify-llamacpp: docker top raised for %s: %s", slug, exc
                    )
            else:
                saw_clean_read = True
        if asyncio.get_running_loop().time() >= deadline:
            return "absent" if saw_clean_read else "unknown"
        if _verify_poll_interval:
            await asyncio.sleep(_verify_poll_interval)


async def stop_llamacpp_containers_by_label(
    slug: str | None, *, host: ResolvedHost | None = None
) -> dict:
    """Stop only the containers labelled ``mc.runtime.slug=<slug>``.

    Deliberately NOT ``evict_spark_runtime_containers``: that one also sweeps
    every ``sparkrun_*_solo`` and ``vllm_node`` container on the box, because a
    vLLM recipe switch needs the whole GPU free before the next model starts.
    llama.cpp runtimes are the opposite case — small GGUF models that exist
    precisely to run *alongside* a big model. Reusing the eviction sweep would
    make "stop the little llama.cpp helper" silently kill the vLLM next to it.
    """
    safe = _sanitize_slug(slug) if slug else None
    if not safe:
        return {"ok": False, "message": "Kein Slug/Label — Container nicht auffindbar.", "stopped": []}
    stop_cmd = (
        f"docker ps -q --filter label=mc.runtime.slug={shlex_quote(safe)} "
        f"| xargs -r docker stop"
    )
    try:
        out, err, ec = await _ssh_run(stop_cmd, host=host, timeout=120)
    except Exception as exc:  # noqa: BLE001
        logger.warning("llamacpp stop raised for %s: %s", slug, exc)
        return {"ok": False, "message": f"Stop fehlgeschlagen: {exc}", "stopped": []}
    if ec != 0:
        return {"ok": False, "message": err or f"docker stop schlug fehl (exit {ec})", "stopped": []}
    stopped = [x for x in out.splitlines() if x.strip()]
    return {
        "ok": True,
        "message": (
            f"Container mit Label mc.runtime.slug={safe} gestoppt: {stopped}"
            if stopped
            else f"Kein laufender Container mit Label mc.runtime.slug={safe} — gilt als gestoppt."
        ),
        "stopped": stopped,
    }


# ── ssh_process: host processes instead of containers ────────────────────────
#
# The docker types can ask the daemon what exists. Here there is no daemon —
# the only observable is the process table, so every lifecycle op is built on
# ``pgrep -x <process_name>``:
#
#   process + HTTP  → ready        the engine serves
#   process, no HTTP→ warming      loading weights (110 GiB takes a while)
#   no process      → stopped
#   SSH broken      → unknown      we genuinely do not know
#
# ``-x`` (exact name match) rather than a pattern: a pattern would match the
# `bash -lc "… start.sh"` wrapper, the SSH command itself, and any editor with
# the name in its window title — and ``pkill`` on that set is how you kill a
# colleague's shell.


def _process_name(runtime: dict) -> str:
    return (runtime.get("process_name") or "").strip()


def _ssh_probe_path(endpoint: str, healthcheck_path: str | None) -> str:
    """Health path that doesn't double the ``/v1`` segment.

    Endpoints for these engines are written as ``http://box:8888/v1`` while the
    default health path is ``/v1/models`` — concatenated naively that probes
    ``/v1/v1/models`` and every healthy engine reads as down. Same
    normalization as the unsloth_porsche arm and probe_runtime_model.
    """
    path = healthcheck_path or _FALLBACK_HEALTHCHECK_PATH
    if endpoint.rstrip("/").endswith("/v1") and path.startswith("/v1"):
        return path[len("/v1"):] or "/models"
    return path


async def _ssh_process_running(
    process_name: str, *, host: ResolvedHost | None = None
) -> bool:
    """True when ``pgrep -x <name>`` finds a process. Raises on SSH failure.

    Raising (instead of returning False) is deliberate: "SSH is broken" and
    "the engine is not running" are different answers, and the callers that
    care — state detection, stop verification — must not confuse the two.
    """
    _, _, exit_code = await _ssh_run(
        f"pgrep -x {shlex_quote(process_name)} > /dev/null 2>&1", host=host, timeout=20
    )
    # pgrep: 0 = found, 1 = nothing matched, >1 = usage/error.
    if exit_code > 1:
        raise RuntimeError(f"pgrep schlug fehl (exit {exit_code})")
    return exit_code == 0


# Module-level so tests can shrink them (mirrors _verify_poll_interval).
_ssh_process_start_timeout = 25.0
_ssh_process_stop_timeout = 20.0


async def verify_ssh_process_started(
    process_name: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float | None = None,
) -> bool:
    """Poll until the process appears. The ssh_process counterpart to
    ``verify_llamacpp_process_started``.

    ``nohup … &`` returns exit 0 the instant the shell forks, whether or not
    the engine survived its first second (missing weights, wrong CUDA, a typo
    in the launch command). Without this poll a start reports success for a
    process that never existed — the exact ADR-059 failure mode, one layer
    down. Weight loading is NOT waited for; that is what the ``warming`` state
    and the switch-grace window are for.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + (
        _ssh_process_start_timeout if timeout is None else timeout
    )
    while True:
        try:
            if await _ssh_process_running(process_name, host=host):
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("verify-ssh-process: pgrep raised for %s: %s", process_name, exc)
        if asyncio.get_running_loop().time() >= deadline:
            return False
        if _verify_poll_interval:
            await asyncio.sleep(_verify_poll_interval)


async def verify_ssh_process_stopped(
    process_name: str,
    *,
    host: ResolvedHost | None = None,
    timeout: float | None = None,
) -> bool:
    """Poll until no such process is left.

    A stop that reports success while 110 GiB are still resident is worse than
    a stop that fails: the next model is launched onto a full box and OOMs —
    the documented vLLM incident, in host-process form.
    """
    import asyncio

    deadline = asyncio.get_running_loop().time() + (
        _ssh_process_stop_timeout if timeout is None else timeout
    )
    while True:
        try:
            still_running = await _ssh_process_running(process_name, host=host)
        except Exception as exc:  # noqa: BLE001 — unknown counts as still busy
            logger.warning("verify-ssh-stopped: pgrep raised for %s: %s", process_name, exc)
            still_running = True
        if not still_running:
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        if _verify_poll_interval:
            await asyncio.sleep(_verify_poll_interval)


async def stop_ssh_process(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Stop an ssh_process runtime: its own ``stop_command``, else ``pkill -x``.

    The engine's script wins when it exists because it usually knows more than
    we do (ds4's stop.sh waits for the port to be released, which pkill does
    not). Either way the result is verified against the process table — a
    stop_command that exits 0 without stopping anything is a lie we can catch.
    """
    process_name = _process_name(runtime)
    stop_command = (runtime.get("stop_command") or "").strip()
    if not process_name and not stop_command:
        return {
            "ok": False,
            "message": (
                "ssh_process-Runtime ohne process_name und ohne stop_command — "
                "MC hat keinen Weg, den Prozess zu beenden."
            ),
        }

    if stop_command:
        command = f"bash -lc {shlex_quote(stop_command)}"
    else:
        # pkill exits 1 when nothing matched — already stopped, not an error.
        command = f"pkill -x {shlex_quote(process_name)} || true"
    try:
        _, stderr, exit_code = await _ssh_run(command, host=host, timeout=180)
    except Exception as exc:  # noqa: BLE001
        logger.error("ssh_process stop raised for %s: %s", runtime.get("id"), exc)
        return {"ok": False, "message": f"SSH-Fehler beim Stoppen: {exc}"}

    if exit_code != 0 and stop_command:
        return {
            "ok": False,
            "message": stderr or f"stop_command schlug fehl (exit {exit_code})",
        }

    if not process_name:
        # Nothing to verify against — the stop_command is all we have.
        return {
            "ok": True,
            "message": f"{runtime.get('display_name') or runtime.get('id')}: stop_command ausgeführt.",
        }

    gone = await verify_ssh_process_stopped(process_name, host=host)
    if not gone:
        return {
            "ok": False,
            "message": (
                f"Prozess '{process_name}' läuft nach dem Stop-Befehl weiter — "
                f"Speicher ist nicht frei. Auf der Box prüfen: pgrep -x {process_name}"
            ),
        }
    await runtime_grace.clear_switching(_grace_slug(runtime))
    return {
        "ok": True,
        "message": f"{runtime.get('display_name') or runtime.get('id')} gestoppt (Prozess '{process_name}' beendet).",
    }


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
    healthcheck_path = runtime.get("healthcheck_path", _FALLBACK_HEALTHCHECK_PATH)
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

    if runtime_type in DOCKER_ENGINE_TYPES:
        container_name = runtime.get("container_name", "")
        if not container_name:
            return {"state": "unknown", "http_reachable": False, "container_status": None}
        # Engine-specific probe path only when the row leaves it unset; an
        # explicit healthcheck_path on the runtime always wins.
        if not healthcheck_path:
            healthcheck_path = _DEFAULT_HEALTHCHECK_PATHS.get(
                runtime_type, _FALLBACK_HEALTHCHECK_PATH
            )

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

    if runtime_type == SSH_PROCESS_TYPE:
        process_name = _process_name(runtime)
        if not process_name:
            # Without a process name there is nothing to observe. "unknown" is
            # the honest answer — "stopped" would invite a start that then
            # cannot be stopped again.
            return {"state": "unknown", "http_reachable": False, "container_status": "no_process_name"}
        try:
            running = await _ssh_process_running(process_name, host=host)
        except Exception as e:  # noqa: BLE001
            logger.warning("SSH fehlgeschlagen für %s: %s", runtime["id"], e)
            return {"state": "unknown", "http_reachable": False, "container_status": "ssh_error"}

        if not running:
            return {"state": "stopped", "http_reachable": False, "container_status": "no_process"}

        reachable = await _probe_http(endpoint, _ssh_probe_path(endpoint, healthcheck_path))
        return {
            # Process up but the port silent means the engine is still reading
            # weights — for a 110 GiB GGUF that window is minutes, not seconds.
            "state": "ready" if reachable else "warming",
            "http_reachable": reachable,
            "container_status": "process_running",
        }

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
        # The double-"/v1" guard now lives in join_probe_url(), applied by
        # _probe_http() for every runtime type — not just this branch.
        reachable = await _probe_http(endpoint, healthcheck_path)
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


def _grace_slug(runtime: dict) -> str | None:
    """Key a runtime is tracked under in runtime_grace. Registry dicts predate
    the slug column, so fall back to the id the same way start/restart do."""
    value = runtime.get("slug") or runtime.get("id")
    return str(value) if value else None


# ── Memory exclusivity across engines ────────────────────────────────────────


async def ensure_exclusive_host(
    runtime: dict,
    *,
    host: ResolvedHost | None = None,
    session: AsyncSession | None = None,
) -> dict:
    """Free the box before an ``exclusive_memory`` runtime starts.

    A GB10 box holds ONE ~110 GB model. Two of them do not coexist, and the
    failure is not a clean error — it is the second engine OOMing minutes into
    a load while the first one silently keeps serving. The vLLM side of this
    was learned the hard way (``evict_spark_runtime_containers``); this is the
    same rule one level up, across engine types: before an exclusive runtime
    starts, every OTHER enabled exclusive runtime on the SAME host is stopped,
    each through its own stop path (docker → eviction sweep, ssh_process →
    stop_command/pkill).

    Returns ``{"ok", "message", "stopped": [slugs]}``. ``ok=False`` must abort
    the start: launching onto a box that is not actually free is the exact
    situation this exists to prevent.

    ``session`` may be passed by a caller that already holds one; otherwise a
    short-lived one is opened. The lifecycle API speaks registry dicts, not
    rows, and the host binding lives in the DB — so this has to look.
    """
    if not runtime.get("exclusive_memory"):
        return {"ok": True, "message": "Runtime beansprucht die Box nicht exklusiv.", "stopped": []}

    slug = _grace_slug(runtime)
    if session is not None:
        return await _ensure_exclusive_host(session, runtime, slug, host=host)

    from app.services.runtime_model_resolver import session_scope

    async with session_scope() as own_session:
        return await _ensure_exclusive_host(own_session, runtime, slug, host=host)


async def _ensure_exclusive_host(
    session: AsyncSession,
    runtime: dict,
    slug: str | None,
    *,
    host: ResolvedHost | None = None,
) -> dict:
    from app.services.host_resolver import resolve_host_for_runtime

    row = (await session.exec(select(Runtime).where(Runtime.slug == slug))).first() if slug else None

    statement = select(Runtime).where(
        Runtime.enabled == True,  # noqa: E712
        Runtime.exclusive_memory == True,  # noqa: E712
    )
    # Same host only. A NULL host_id means "the settings fallback box" — two
    # NULLs are the same box, which is why this is an explicit IS NULL rather
    # than an equality that would never match.
    host_id = row.host_id if row is not None else None
    statement = (
        statement.where(Runtime.host_id.is_(None))
        if host_id is None
        else statement.where(Runtime.host_id == host_id)
    )
    others = [rt for rt in (await session.exec(statement)).all() if rt.slug != slug]

    stopped: list[str] = []
    for other in others:
        other_dict = other.to_registry_dict()
        try:
            other_host = await resolve_host_for_runtime(session, other) or host
        except Exception as exc:  # noqa: BLE001
            logger.warning("exclusive: host resolution failed for %s: %s", other.slug, exc)
            other_host = host

        try:
            state = await get_runtime_state(other_dict, host=other_host)
        except Exception as exc:  # noqa: BLE001
            state = {"state": "unknown", "reason": str(exc)}
        if state.get("state") == "stopped":
            logger.info("exclusive: %s already stopped", other.slug)
            continue

        logger.info("exclusive: stopping %s to free the box for %s", other.slug, slug)
        if other.runtime_type in DOCKER_ENGINE_TYPES:
            result = await evict_spark_runtime_containers(
                other.slug,
                container_name=(other.container_name or None),
                host=other_host,
            )
        elif other.runtime_type == SSH_PROCESS_TYPE:
            result = await stop_ssh_process(other_dict, host=other_host)
        else:
            result = await stop_runtime(other_dict, host=other_host)

        if not result.get("ok"):
            return {
                "ok": False,
                "message": (
                    f"'{other.display_name}' ({other.slug}) läuft noch und konnte nicht "
                    f"gestoppt werden: {result.get('message')}. Start abgebrochen — "
                    f"zwei grosse Modelle passen nicht gleichzeitig auf die Box."
                ),
                "stopped": stopped,
            }
        stopped.append(other.slug)
        await runtime_grace.clear_switching(other.slug)

    return {
        "ok": True,
        "message": (
            f"Box freigegeben (gestoppt: {', '.join(stopped)})."
            if stopped
            else "Box war bereits frei."
        ),
        "stopped": stopped,
    }


async def _emit_exclusive_event(slug: str | None, result: dict) -> None:
    """Record an exclusivity decision in the activity feed. Best-effort — a
    failing event must never be the reason a start does not happen."""
    try:
        from app.services.activity import emit_event
        from app.services.runtime_model_resolver import session_scope

        async with session_scope() as session:
            await emit_event(
                session,
                "runtime.exclusive_evicted" if result.get("ok") else "runtime.exclusive_blocked",
                f"{slug}: {result.get('message')}",
                severity="info" if result.get("ok") else "warning",
                detail={"slug": slug, "stopped": result.get("stopped") or []},
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("exclusive: event emit failed for %s: %s", slug, exc)


async def start_runtime(
    runtime: dict,
    *,
    host: ResolvedHost | None = None,
    grace_source: str = runtime_grace.SOURCE_MANUAL,
) -> dict:
    """Starts a runtime (see :func:`_start_runtime_impl`).

    Three things wrap the actual start:

    * **Exclusivity** (PR6) — an ``exclusive_memory`` runtime frees its box
      first (:func:`ensure_exclusive_host`) and refuses to start if it can't.
      Runtimes without the flag are untouched, so vLLM behaves exactly as
      before.
    * **Memory prep** (PR8) — on a GB10 the same exclusive runtime additionally
      gets its box's page cache dropped and (when configured) the free-memory
      watermark temporarily lowered, because the engine sizes its KV cache
      against host MemFree. The undo runs in a ``finally``: a start that raised
      must not leave a lowered watermark behind
      (:mod:`app.services.host_memory_prep`).
    * **Switch-grace** (PR5) — docker engines and ssh_process take 2–15
      minutes to serve; without the marker the watcher reports that warmup as
      an outage. ``grace_source`` records who asked — ``switch_recipe`` and
      the watcher's auto-recovery pass their own value.

    The marker is cleared here only when the start call itself fails; a
    successful start stays "in flight" until a probe confirms the engine
    serves (runtime_watcher), which is the only honest end of the window.
    ssh_process additionally moves ``launching`` → ``loading`` once the
    process is confirmed alive: from there on it is weights, not launching.
    """
    slug = _grace_slug(runtime)
    runtime_type = runtime.get("runtime_type")
    is_docker = runtime_type in DOCKER_ENGINE_TYPES
    is_ssh_process = runtime_type == SSH_PROCESS_TYPE

    if (is_docker or is_ssh_process) and runtime.get("exclusive_memory"):
        exclusive = await ensure_exclusive_host(runtime, host=host)
        await _emit_exclusive_event(slug, exclusive)
        if not exclusive.get("ok"):
            return {"ok": False, "message": exclusive["message"]}

    if is_docker or is_ssh_process:
        await runtime_grace.mark_switching(
            slug, runtime_grace.PHASE_LAUNCHING, grace_source
        )

    # Memory prep spans the LOAD window, not just this call: `docker compose up`
    # returns in seconds while the engine spends minutes pulling weights and
    # only then decides how large a KV cache it may allocate. Removing the
    # cache dropper here would take it away exactly before the measurement it
    # exists for. So the prep is ended where the window honestly ends — the
    # watcher probe that sees the engine serving (or the crash-loop stop, or
    # the 30-minute orphan sweep). Here we only undo it when the start itself
    # never got off the ground.
    resolved = host or resolve_host_from_runtime_fields(runtime)
    prep = None
    if is_docker or is_ssh_process:
        prep = await host_memory_prep.prepare_for_runtime(runtime, host=resolved)

    if prep is not None and prep.mem_wait_timed_out:
        # PR 10: the box reported the prep as done (cache dropped, watermark
        # lowered) but MemAvailable never cleared the threshold — the exact
        # shape of the reboot-test failure, where a crash-looped engine's
        # allocations had not actually drained. Attempting the start anyway
        # is the blind retry that produced the original OOM, so it never
        # happens: `_start_runtime_impl` is not called, and the prep's own
        # changes (dropper, watermark) are undone below like any failed start.
        result = {
            "ok": False,
            "message": (
                f"Box-Speicher nicht rechtzeitig frei — nur "
                f"{(prep.mem_available_after_wait_kb or 0) // 1024} MiB verfügbar, "
                f"benötigt {(prep.mem_wait_threshold_kb or 0) // 1024} MiB. "
                f"Start abgebrochen statt blind zu versuchen."
            ),
        }
    else:
        try:
            result = await _start_runtime_impl(runtime, host=host)
        except Exception:
            await host_memory_prep.finish_for_runtime(prep, host=resolved, success=False)
            await runtime_grace.clear_switching(slug)
            raise
    if not result.get("ok"):
        await host_memory_prep.finish_for_runtime(prep, host=resolved, success=False)

    if (is_docker or is_ssh_process) and not result.get("ok"):
        await runtime_grace.clear_switching(slug)
    elif is_ssh_process:
        # Process confirmed alive — the rest of the window is weight loading.
        await runtime_grace.mark_switching(
            slug, runtime_grace.PHASE_LOADING, grace_source
        )
    return result


async def _start_runtime_impl(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Starts a runtime.

    vllm_docker / llamacpp_docker: docker start via SSH
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

    if runtime_type in DOCKER_ENGINE_TYPES:
        container_name = (runtime.get("container_name") or "").strip()
        launch_command = (runtime.get("launch_command") or "").strip()
        is_llamacpp = runtime_type == "llamacpp_docker"
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
                    # ADR-059 — container existing is not enough: some launches
                    # (sparkrun solo-mode, manual wrappers) keep PID1 alive
                    # (e.g. `sleep infinity`) while the actual vllm serve
                    # process inside crashed or never started (wrong tp, bad
                    # flags, immediate OOM). This is the failure mode from the
                    # original incident — MC reported success while nothing
                    # was serving. Catch it here instead of discovering it via
                    # a mysteriously "unreachable" runtime minutes later.
                    # llama-server never matches "vllm serve", so llamacpp rows
                    # get the equivalent check on their own process name.
                    if is_llamacpp:
                        serving = await verify_llamacpp_process_started(
                            str(runtime_slug), host=host
                        )
                        process_label, likely_cause = (
                            "llama-server",
                            "falsches GGUF/Repo, fehlende Flags oder Crash",
                        )
                    else:
                        serving = await verify_spark_vllm_process_started(
                            str(runtime_slug), host=host
                        )
                        process_label, likely_cause = (
                            "vllm-serve",
                            "falsche tp/Flags oder Crash",
                        )
                    if serving == "absent":
                        logger.error(
                            "Runtime %s: container appeared but no %s process "
                            "found inside it (%s). Log: %s",
                            runtime["id"], process_label, likely_cause, log_path,
                        )
                        return {
                            "ok": False,
                            "message": (
                                f"{runtime['display_name']}: Container erschien, aber "
                                f"kein {process_label}-Prozess gestartet (wahrscheinlich "
                                f"{likely_cause}). Logs: {log_path}"
                            ),
                        }
                    if serving == "unknown":
                        # Paket 2 — "nicht feststellbar" ist kein Fehlschlag:
                        # beide Live-Launches am 15.08. wurden fälschlich als
                        # tot gemeldet, weil die Box unter Last stand und SSH
                        # nur stockte. Optimistisch melden; der Watcher (Grace,
                        # Crash-Loop, Unreachable) übernimmt das Urteil.
                        logger.warning(
                            "Runtime %s: %s-Prozess nicht verifizierbar "
                            "(SSH/Box unter Last) — Start optimistisch gemeldet, "
                            "Watcher übernimmt. Log: %s",
                            runtime["id"], process_label, log_path,
                        )
                        return {
                            "ok": True,
                            "message": (
                                f"{runtime['display_name']} wird gestartet; "
                                f"Verifikation gerade nicht möglich (Box unter "
                                f"Last) — der Watcher prüft weiter. Logs: {log_path}"
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

    if runtime_type == SSH_PROCESS_TYPE:
        process_name = _process_name(runtime)
        launch_command = (runtime.get("launch_command") or "").strip()
        if not launch_command:
            return {"ok": False, "message": "Keine launch_command konfiguriert."}
        if not process_name:
            return {
                "ok": False,
                "message": (
                    "Kein process_name konfiguriert — MC könnte den Prozess starten, "
                    "danach aber weder sehen noch stoppen."
                ),
            }
        try:
            # Idempotent: the engines this serves ship idempotent start scripts
            # (ds4's start.sh exits early when the port already answers), but
            # re-running a 110 GiB installer because MC didn't look first is
            # not something to leave to the script.
            state = await get_runtime_state(runtime, host=host)
            if state.get("state") == "ready":
                return {
                    "ok": True,
                    "message": f"{runtime['display_name']} läuft bereits — nichts zu tun.",
                }
            if state.get("state") == "warming":
                return {
                    "ok": True,
                    "message": (
                        f"{runtime['display_name']} startet bereits (Prozess läuft, "
                        f"Endpunkt antwortet noch nicht) — nichts zu tun."
                    ),
                }

            slug_safe = _sanitize_slug(runtime.get("id") or runtime.get("slug") or "unknown")
            log_path = f"~/.cache/mc/runtime-launch-{slug_safe}.log"
            detach_cmd = (
                f"mkdir -p ~/.cache/mc && "
                f"nohup bash -lc {shlex_quote(launch_command)} "
                f"> {log_path} 2>&1 &"
            )
            _, stderr, exit_code = await _ssh_run(detach_cmd, host=host)
            if exit_code != 0:
                return {
                    "ok": False,
                    "message": stderr or f"launch_command schlug fehl (exit {exit_code}). Logs: {log_path}",
                }

            appeared = await verify_ssh_process_started(process_name, host=host)
            if not appeared:
                logger.error(
                    "Runtime %s: launch exited 0 but no '%s' process appeared. Log: %s",
                    runtime["id"], process_name, log_path,
                )
                return {
                    "ok": False,
                    "message": (
                        f"{runtime['display_name']} gestartet, aber kein Prozess "
                        f"'{process_name}' erschienen (wahrscheinlich Crash beim Start "
                        f"oder Engine nicht installiert). Logs auf der Box: {log_path}"
                    ),
                }
            logger.info(
                "ssh_process gestartet: %s (Prozess %s, log %s)",
                runtime["id"], process_name, log_path,
            )
            return {
                "ok": True,
                "message": (
                    f"{runtime['display_name']} gestartet (Prozess '{process_name}' läuft). "
                    f"Gewichte laden dauert je nach Grösse mehrere Minuten. Logs: {log_path}"
                ),
            }
        except Exception as e:  # noqa: BLE001
            logger.error("ssh_process Start fehlgeschlagen für %s: %s", runtime["id"], e)
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

    vllm_docker / llamacpp_docker: docker stop via SSH
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

    if runtime_type in DOCKER_ENGINE_TYPES:
        container_name = (runtime.get("container_name") or "").strip()
        # RC-1 fix: container_name is None right after a recipe switch (sparkrun
        # assigns a fresh random id each run). Running `docker stop ` with an
        # empty arg errors and was silently swallowed, leaving the old model up.
        # Fall back to label eviction so the model is actually stopped — but
        # scoped per engine: vLLM gets the full solo/manual sweep (it needs the
        # whole GPU free), llama.cpp only its own label (it is a co-tenant).
        if not container_name:
            slug = runtime.get("slug") or runtime.get("id")
            logger.info(
                "stop_runtime: empty container_name for %s — stopping by label",
                runtime.get("id"),
            )
            if runtime_type == "llamacpp_docker":
                return await stop_llamacpp_containers_by_label(slug, host=host)
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

    if runtime_type == SSH_PROCESS_TYPE:
        return await stop_ssh_process(runtime, host=host)

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


async def restart_runtime(
    runtime: dict,
    *,
    host: ResolvedHost | None = None,
    grace_source: str = runtime_grace.SOURCE_MANUAL,
) -> dict:
    """Restarts a runtime — same switch-grace wrapper as :func:`start_runtime`.

    A `docker restart` drops the engine into the identical multi-minute reload
    window, so it needs the same protection from false unreachable alarms.

    ssh_process is absent here on purpose: its restart is stop + ``start_runtime``,
    and that call already sets the marker (and runs the exclusivity check).
    Marking here too would just overwrite it with the same value.
    """
    slug = _grace_slug(runtime)
    is_docker = runtime.get("runtime_type") in DOCKER_ENGINE_TYPES
    if is_docker:
        await runtime_grace.mark_switching(
            slug, runtime_grace.PHASE_LAUNCHING, grace_source
        )
    result = await _restart_runtime_impl(runtime, host=host)
    if is_docker and not result.get("ok"):
        await runtime_grace.clear_switching(slug)
    return result


async def _restart_runtime_impl(runtime: dict, *, host: ResolvedHost | None = None) -> dict:
    """Restarts a runtime.

    vllm_docker / llamacpp_docker: docker restart via SSH
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

    if runtime_type in DOCKER_ENGINE_TYPES:
        container_name = runtime.get("container_name") or None
        # container_name is None after every recipe-switch (the DB field is cleared
        # and only re-populated once the new container appears). Running
        # `docker restart None` against that value caused a 400 (live incident
        # 2026-06-27).  Discover the actual running container via the label+solo
        # sweep that evict_spark_runtime_containers already uses, then restart it.
        if not container_name:
            slug = runtime.get("slug") or runtime.get("id")
            try:
                if runtime_type == "llamacpp_docker":
                    # Label-only: the solo/manual sweep would happily hand back
                    # the big vLLM container and restart THAT instead.
                    discovered = await _labelled_containers(slug, host=host)
                else:
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

    if runtime_type == SSH_PROCESS_TYPE:
        # No `docker restart` equivalent for a bare process: stop, verify it is
        # gone, then start. Going through the public helpers keeps the
        # exclusivity check and the grace marker on the start half.
        stop_result = await stop_runtime(runtime, host=host)
        if not stop_result["ok"]:
            return stop_result
        return await start_runtime(runtime, host=host)

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


_AGENT_TELEMETRY_FRESH_S = 60  # heartbeat interval is 15s — 60s tolerates 3 missed beats


def _metrics_from_agent_telemetry(telemetry: dict) -> dict:
    """Maps the node-agent's push telemetry (routers/nodes.py TelemetrySchema)
    onto the existing SSH-pull return shape, so TelemetryColumn/UI code needs
    no changes. Extra fields (cpu_pct, load1, disk_*) are additive — unknown
    keys in the dict are simply ignored by callers that don't read them."""
    return {
        "reachable": True,
        "gpu_util_pct": telemetry.get("gpu_util_pct"),
        "vram_used_mb": telemetry.get("vram_used_mb"),
        "vram_total_mb": telemetry.get("vram_total_mb"),
        "gpu_temp_c": telemetry.get("gpu_temp_c"),
        "ram_used_mb": telemetry.get("mem_used_mb"),
        "ram_total_mb": telemetry.get("mem_total_mb"),
        "cpu_pct": telemetry.get("cpu_pct"),
        "load1": telemetry.get("load1"),
        "mem_available_mb": telemetry.get("mem_available_mb"),
        "swap_used_mb": telemetry.get("swap_used_mb"),
        "disk_used_gb": telemetry.get("disk_used_gb"),
        "disk_total_gb": telemetry.get("disk_total_gb"),
    }


def _agent_telemetry_fresh(host: ResolvedHost) -> bool:
    """True only for kind=='agent' hosts (review finding #8, 30.08.2026):
    routers/nodes.py's pairing-codes endpoint lets an admin mint a code
    against ANY pre-existing host_id regardless of its kind, so an ssh-kind
    host could in principle end up with agent_telemetry populated too — and
    without this check, that pushed (possibly stale) snapshot would mask
    its real SSH probe instead of just being additional, unused data."""
    if host.kind != "agent":
        return False
    if not host.agent_telemetry or not host.agent_last_seen_at:
        return False
    age_s = (utcnow() - ensure_aware(host.agent_last_seen_at)).total_seconds()
    return age_s < _AGENT_TELEMETRY_FRESH_S


async def get_host_metrics(host: ResolvedHost | None) -> dict:
    """Fetches live hardware metrics for a host (ADR-048, generic).

    - agent push       → a node-agent (kind=agent, see routers/nodes.py) posts
      a heartbeat every 15s; if the last snapshot is <60s old we answer from
      it instead of opening an SSH connection at all (faster, and works for
      boxes that never got an SSH key wired up). Older/missing telemetry
      falls through to the SSH probe below, same as before this existed.
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
    if host is not None and _agent_telemetry_fresh(host):
        return _metrics_from_agent_telemetry(host.agent_telemetry)
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
        from app.services.ai_provider_config import hf_auth_headers

        headers = await hf_auth_headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://huggingface.co/api/models",
                params={"search": query, "filter": "gguf", "author": "lmstudio-community", "limit": 20, "blobs": "true"},
                headers=headers,
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
        from app.services.ai_provider_config import hf_auth_headers

        headers = await hf_auth_headers()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://huggingface.co/api/models/{repo_id}?blobs=true",
                headers=headers,
            )
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
    """Downloads a GGUF file from HuggingFace directly into the LM Studio models directory.

    With an ``hf_token`` secret stored, the curl carries an Authorization
    header so gated repos work. Without one the command is byte-identical to
    the pre-token version — anonymous, public repos only, exactly today's
    behaviour. The header is only added when a token exists so an install that
    never set one gains no new failure mode.
    """
    from app.services.ai_provider_config import get_hf_token

    author, _, model_name = repo_id.partition("/")
    dest_dir = f"~/.lmstudio/models/{author}/{model_name}"
    safe_name = (repo_id + "_" + filename).replace("/", "_").replace(" ", "_")
    log_path = f"/tmp/hf-download-{safe_name}.log"
    token = await get_hf_token()
    # NOTE: the header lands in the remote shell's argv, so the token is
    # visible in `ps` to anyone with a shell on the GPU box. That box is
    # single-operator by construction (the same person owns the token); a
    # netrc/--config file would hide it from ps but leave it on disk instead.
    auth_arg = f"-H 'Authorization: Bearer {token}' " if token else ""
    command = (
        f"mkdir -p {dest_dir} && "
        f"nohup curl -L {auth_arg}'https://huggingface.co/{repo_id}/resolve/main/{filename}' "
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
