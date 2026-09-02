"""docker-compose.agents.yml generator (Phase 15).

Renders the agents compose file from DB state so the image per agent follows
agent.runtime_id → runtime.runtime_type instead of being hardcoded via static
YAML anchors.

Image rules:
- runtime.runtime_type == "cloud"                                  → mc-claude-agent:latest
- runtime.runtime_type in {vllm_docker, llamacpp_docker, lmstudio, openai_compatible, unsloth}
  → mc-agent-base:latest
- runtime is None                                                  → keep static fallback
                                                                      (preserve existing assignment for
                                                                       legacy agents without runtime_id).

The function reads the existing compose file once (as a fallback baseline) so
we never lose service-specific volumes/build/env stanzas. We only rewrite the
`image:` line per service. Preserves comments and ordering by line-based edit.

In addition to image rewriting, the renderer injects vault mount entries for
agents that hold the ``vault:write`` scope (or have ``scopes=None|[]``, which
is treated as all-scopes for backward-compat). Injection adds a
``${HOME}/.mc/vault:/vault:rw`` volume entry plus ``AGENT_VAULT_PATH``,
``AGENT_VAULT_INBOX``, and ``AGENT_SLUG`` environment variables per service.

Services whose resolved image is ``OMP_IMAGE`` (ADR-045 headless harness)
additionally get an omp session-transcript mount (see
``_OMP_SESSIONS_TARGET`` / ``_ensure_omp_sessions_volume``) — without it the
token harvester never sees omp's JSONL transcripts.

Atomic write: rendered to a tmpfile (`<path>.tmp`), then `os.replace()` on the
target. The previous file is moved to `<path>.bak` first.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from app.config import settings

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import get_redis
from app.scopes import Scope

logger = logging.getLogger("mc.compose_renderer")

# Lock hierarchy (hold outermost first to avoid deadlock):
#   1. mc:agent:{id}:runtime-switch  — per-agent switch lock (agent_runtime_switch.py)
#   2. mc:compose:agents-yml:write   — global compose-file write lock (this module)
# The two locks protect different resources and may both be held simultaneously.
# The per-agent lock prevents two switches on the same agent from racing each
# other; the compose lock prevents two switches on *different* agents from
# both reading DB state, rendering, and writing the file concurrently (where
# the last writer could overwrite the other agent's image change).
COMPOSE_WRITE_LOCK_KEY = "mc:compose:agents-yml:write"
COMPOSE_WRITE_LOCK_TTL = 60  # seconds

# Agent image names are prefix+tag configurable so self-hosters can run
# published GHCR images while developers keep bare local names.
# Default is the published GHCR images (since v0.2.0 — the release that
# first published mc-claude-agent + mc-agent-base). Existing installs
# upgrading past this point either run scripts/build-agent-images.sh once
# (dual-tags local builds so they shadow the pull) or pin
# MC_AGENT_IMAGE_PREFIX="" in .env to stay on bare local names. compose
# does NOT fall back from a missing registry name to a bare local image. scripts/build-agent-images.sh
# tags every local build under BOTH names so a local build always shadows
# a registry pull once the prefix is active.
_AGENT_IMAGE_PREFIX = os.environ.get("MC_AGENT_IMAGE_PREFIX", "ghcr.io/argyelan-ai/")
_AGENT_IMAGE_TAG = os.environ.get("MC_AGENT_IMAGE_TAG") or "latest"


def _agent_image(name: str) -> str:
    return f"{_AGENT_IMAGE_PREFIX}{name}:{_AGENT_IMAGE_TAG}"


def _image_is(image: str | None, base_name: str) -> bool:
    """True if ``image`` refers to ``base_name`` regardless of registry
    prefix, tag or digest — legacy agents.yml files carry bare local names
    (``mc-kimi-agent:latest``) while prefixed renders carry registry names.
    """
    if not image:
        return False
    repo = image.split("@", 1)[0]
    head, sep, tail = repo.rpartition(":")
    if sep and "/" not in tail:  # strip the tag, but not a registry port
        repo = head
    return repo == base_name or repo.endswith(f"/{base_name}")


CLAUDE_IMAGE = _agent_image("mc-claude-agent")
OPENCLAUDE_IMAGE = _agent_image("mc-agent-base")
# ADR-045: third harness image — omp headless driver (bridge.py --serve) instead
# of an interactive openclaude pane. Selected by runtime_type == "omp".
# omp/kimi stay BARE local names on purpose: their binaries are pinned
# arm64-only, the images will never be published to a registry, and a
# prefixed name would send `docker compose` on a pull that can only fail
# (adversarial-review finding, 2026-08-06).
OMP_IMAGE = "mc-omp-agent:latest"
# Fourth harness image — Kimi Code CLI (tmux+poll.sh like claude, own binary).
# Selected by harness == "kimi" / runtime_type == "kimi".
KIMI_IMAGE = "mc-kimi-agent:latest"

HARNESS_IMAGES: dict[str, str] = {
    "claude": CLAUDE_IMAGE,
    "openclaude": OPENCLAUDE_IMAGE,
    "omp": OMP_IMAGE,
    "kimi": KIMI_IMAGE,
}

# Token isolation (fix/agent-token-env-file-leak, supersedes the earlier
# "defense layer 1"): docker/.env.agents (symlink under ~/.mc/secrets/…) holds
# the MC_TOKEN_<NAME> secret of EVERY agent.  Mounting it as env_file handed
# each container all of them in plain text — a compromised agent could act as
# any other.  The renderer therefore never emits it and strips lingering
# entries written by older versions.  Tokens reach a container only through
# ``MC_TOKEN=${MC_TOKEN_<NAME>}`` interpolation; every compose caller passes
# ``--env-file docker/.env.agents`` for that (cli_terminal.force_recreate,
# docker_agent_sync, scripts/start-all.sh).
_AGENTS_ENV_FILE = "docker/.env.agents"
# The shared env file already referenced by anchor blocks.  Whenever a
# service-level env_file list remains we keep it there so that YAML merge
# semantics (service-level list replaces the anchor list, not merges) do not
# silently drop CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, TAVILY_API_KEY, etc.
_SHARED_ENV_FILE = "docker/.env.shared"

# Compose path: docker/docker-compose.agents.yml relative to repo root.
# Repo root comes from settings.mc_repo_path (MC_REPO_PATH env — set by
# setup.sh; the checkout may have any folder name). Tests inject the path.
DEFAULT_COMPOSE_PATH = (
    Path(settings.mc_repo_path) / "docker" / "docker-compose.agents.yml"
)

# The shipped template next to it. The file above left version control with
# the OSS split — it describes its owner's fleet. What ships instead is this
# agent-free template; `setup.sh` makes the personal copy from it.
# The rendered file lists the operator's agents, the projects they touch and
# where their mounts point. Same class of content as docker/.env.shared next to
# it, which the project already keeps at 0600 — so does this, on every write.
# Without it the hardening would last exactly until the next runtime switch.
COMPOSE_FILE_MODE = 0o600

COMPOSE_TEMPLATE_FILENAME = "docker-compose.agents.example.yml"
DEFAULT_COMPOSE_TEMPLATE_PATH = DEFAULT_COMPOSE_PATH.with_name(
    COMPOSE_TEMPLATE_FILENAME
)


def _read_compose_or_template(path: Path) -> str:
    """Read the operator's own agents file — falling back to the template.

    Why the fallback exists: anyone who skips `setup.sh` (e.g. plain
    `docker compose up -d` straight from the README quickstart) does not have
    the file. The first runtime bind then calls `write_compose_agents` → here,
    and a hard `FileNotFoundError` disappears into provisioning's BackgroundTask
    logger — the user sees a silent non-effect. With the template as scaffolding
    the render goes through and writes a valid file of their own.
    """
    if path.exists():
        return path.read_text(encoding="utf-8")

    # Guard: if the whole directory is gone, the explanation is not "no agent
    # created yet" but a lost mount. Rendering from the template would then
    # replace a real fleet with a generated one — hand edits included. Fail
    # loudly instead.
    if not path.parent.exists():
        raise FileNotFoundError(
            f"{path.parent} does not exist — the docker/ directory is not "
            "visible inside the backend container (lost mount). Deliberately "
            "NOT rendering from the template: that would replace your own fleet "
            "with a generated one. Fix: docker compose restart backend"
        )

    for template in (path.with_name(COMPOSE_TEMPLATE_FILENAME),
                     DEFAULT_COMPOSE_TEMPLATE_PATH):
        if template.exists():
            logger.warning(
                "compose_renderer: %s missing — rendering from template %s "
                "(normal for the first agent; ./setup.sh creates the copy otherwise)",
                path, template,
            )
            return template.read_text(encoding="utf-8")

    raise FileNotFoundError(
        f"neither the agents file ({path}) nor the template "
        f"{COMPOSE_TEMPLATE_FILENAME} next to it was found — ./setup.sh in the "
        "project directory creates the copy"
    )


def pick_image_for_runtime(runtime: Runtime | None) -> str | None:
    """Resolve the docker image required for a given runtime.

    The `claude` binary in CLAUDE_IMAGE only speaks the native Anthropic API.
    Selection is by **slug prefix** (`anthropic-claude-*`) — not by
    `runtime_type` — so cloud-hosted OpenAI-compatible endpoints like Ollama
    Cloud (slug `ollama-cloud`, runtime_type `cloud`) correctly route to the
    openclaude binary in OPENCLAUDE_IMAGE. Keeps image selection in sync with
    docker_agent_sync.py's `is_anthropic = slug.startswith("anthropic-claude-")`
    check — both must agree, otherwise the .env render and the binary that
    reads it disagree on whether OPENAI_* shims are needed.

    Returns None when runtime is missing — caller should fall back to the
    existing static assignment instead of overwriting it.
    """
    if runtime is None or not runtime.enabled:
        return None
    if (runtime.slug or "").startswith("anthropic-claude-"):
        return CLAUDE_IMAGE
    rt_type = (runtime.runtime_type or "").strip()
    # ADR-045: the omp headless runtime binds to its own image. Keyed on
    # runtime_type (not slug) — checked BEFORE the openclaude allowlist so an
    # omp runtime never falls through to the openclaude image. Without this the
    # function returned None for "omp", which detect_image_change reads as
    # "assume image change" and the switch could not resolve the omp image.
    if rt_type == "omp":
        return OMP_IMAGE
    # kimi runtimes bind to the kimi image — checked before the openclaude
    # allowlist for the same reason as omp (never fall through to a shim).
    if rt_type == "kimi":
        return KIMI_IMAGE
    # llamacpp_docker is an OpenAI-compatible ENGINE, so the agent side needs
    # the same openclaude shim as vllm_docker. The llama.cpp server image
    # itself is not chosen here — this function returns the AGENT container
    # image; the engine image lives in the launch command / local recipe
    # (config/local-recipes.json, default ghcr.io/ggml-org/llama.cpp:server*).
    if rt_type in (
        "vllm_docker", "llamacpp_docker", "lmstudio", "openai_compatible", "unsloth", "cloud"
    ):
        return OPENCLAUDE_IMAGE
    return None


def pick_image_for_harness(harness: str | None, runtime: Runtime | None) -> str | None:
    """Image selection under ADR-056: the harness decides the image.

    harness None = legacy row (pre-backfill / host agents) -> old
    runtime-type coupling via pick_image_for_runtime.
    """
    if harness in HARNESS_IMAGES:
        return HARNESS_IMAGES[harness]
    return pick_image_for_runtime(runtime)


def detect_image_change(
    old_runtime: Runtime | None,
    new_runtime: Runtime | None,
    *,
    old_harness: str | None = None,
    new_harness: str | None = None,
) -> bool:
    """True when switching old → new requires a docker image swap.

    None on either side counts as "unknown — assume yes" so callers force a
    recreate (safe default during first-time bind).
    """
    old_img = pick_image_for_harness(old_harness, old_runtime)
    new_img = pick_image_for_harness(new_harness, new_runtime)
    if old_img is None or new_img is None:
        return True
    return old_img != new_img


def _agent_slug(agent: Agent) -> str:
    return (agent.name or "").lower().replace(" ", "-")


def _service_image_blocks(content: str) -> list[tuple[int, int, str]]:
    """Locate `image: ...` lines inside service definitions.

    The compose file uses `<<: *claude-agent-base` to inherit `image:`. We
    don't rewrite the anchor blocks; we only override the per-service `image:`
    where present and add an explicit `image:` for services that inherit
    when their runtime forces a different image than their anchor's default.

    Returns: list of (line_index, indent_columns, current_image) for every
    explicit `image:` line found.
    """
    out: list[tuple[int, int, str]] = []
    for idx, line in enumerate(content.splitlines()):
        m = re.match(r"^(\s*)image:\s*(\S+)\s*$", line)
        if m:
            indent = len(m.group(1))
            out.append((idx, indent, m.group(2)))
    return out


_SERVICE_RE = re.compile(r"^(\s*)mc-agent-(?P<slug>[a-z0-9_-]+):\s*$")
_ANCHOR_RE = re.compile(r"^\s*<<:\s*\*(?P<anchor>[a-z0-9_-]+)\s*$")

# Per-agent vault injection (M.3 T1). Replaces the M.2 hand-edit pattern.
# Service blocks use 2-space indent for the service name, 4 spaces for
# environment:/volumes: keys, and 6 spaces ("      - ...") for list items.
_VAULT_VOLUME_TEMPLATE = "      - ${HOME}/.mc/vault:/vault:rw"

# Referenz-Dateien (ADR-053): Source UND Target = Host-Pfad, damit die
# absoluten Pfade aus der Dispatch-Directive im Container identisch
# auflösen (compose-up läuft mit HOME=HOME_HOST, docker_agent_sync.py).
_REFERENCES_VOLUME_TEMPLATE = "      - ${HOME}/.mc/references:${HOME}/.mc/references:ro"

# omp session transcripts (ADR-045 headless harness): omp writes JSONL
# turn-by-turn transcripts to $HOME/.omp/profiles/mc-agent/agent/sessions
# inside the container. No host mount existed for this before the token
# harvester fix (root cause: model_usage_events stayed empty for omp agents
# since 2026-07-05) — every restart/recreate silently discarded the
# transcripts the harvester needs. Target path must match the omp-bridge
# entrypoint's OMP_PROFILE=mc-agent convention (docker/omp-bridge/entrypoint.sh).
_OMP_SESSIONS_TARGET = "/home/agent/.omp/profiles/mc-agent/agent/sessions"


def _find_block_range(
    body_lines: list[str], key: str
) -> tuple[int, int] | None:
    """Locate a top-level service-body block (e.g. ``environment:`` or
    ``volumes:``) inside the captured service body.

    Returns ``(header_idx, end_idx_exclusive)`` where ``header_idx`` points at
    the ``key:`` line and ``end_idx_exclusive`` points just past the last list
    item or nested line. The block is the contiguous range of lines starting
    with the header and continuing while subsequent lines are list items
    (``      - ...``) or deeper nested content; it stops at the next 4-space
    top-level key or at the end of the body.

    Returns ``None`` if the block is not present in the body.
    """
    # Service keys (environment, volumes, build, ...) sit at 4-space indent.
    header_re = re.compile(rf"^(    ){re.escape(key)}:\s*$")
    other_top_re = re.compile(r"^(    )[A-Za-z_][A-Za-z0-9_-]*:\s*$")

    header_idx: int | None = None
    for i, line in enumerate(body_lines):
        if header_re.match(line):
            header_idx = i
            break
    if header_idx is None:
        return None

    end = len(body_lines)
    for j in range(header_idx + 1, len(body_lines)):
        line = body_lines[j]
        # Stop at the next 4-space top-level key (e.g. ``    volumes:`` ends
        # ``    environment:``).
        if other_top_re.match(line):
            end = j
            break
        # If we hit something that's not indented at all, stop (shouldn't
        # happen inside a service body — that's the service-boundary case
        # already handled by the caller).
        if line and not line.startswith(" "):
            end = j
            break
    # Trim trailing blank/whitespace-only lines so insertions land *inside*
    # the list, not after a stray blank that separates this block from the
    # next service.
    while end > header_idx + 1 and not body_lines[end - 1].strip():
        end -= 1
    return (header_idx, end)


def _ensure_vault_entries(body_lines: list[str], slug: str) -> list[str]:
    """Insert the vault volume mount + env vars into a service body if they
    are not already present.

    - Volume: appended to existing ``volumes:`` block, or a new ``volumes:``
      block is created at the end of the service body.
    - Env vars: appended to existing ``environment:`` block, or a new
      ``environment:`` block is created at the end of the service body
      (before ``volumes:`` if both must be created).

    Idempotent: existing entries are detected by substring match and skipped.
    """
    volume_marker = "/.mc/vault:/vault:rw"
    env_path_line = f"- AGENT_VAULT_PATH=/vault/agents/{slug}"
    env_inbox_line = "- AGENT_VAULT_INBOX=/vault/_inbox"
    env_slug_line = f"- AGENT_SLUG={slug}"

    def _line_present(body: list[str], target: str) -> bool:
        """True when any list-item line in *body* matches *target* exactly
        (ignoring leading/trailing whitespace on the line, but the list-item
        dash and the key=value must be an exact token match — not a prefix)."""
        return any(line.strip() == target for line in body)

    body = list(body_lines)

    # ── Environment vars ─────────────────────────────────────────────────
    missing_env: list[str] = []
    if not _line_present(body, env_path_line):
        missing_env.append(f"      - AGENT_VAULT_PATH=/vault/agents/{slug}")
    if not _line_present(body, env_inbox_line):
        missing_env.append("      - AGENT_VAULT_INBOX=/vault/_inbox")
    if not _line_present(body, env_slug_line):
        missing_env.append(f"      - AGENT_SLUG={slug}")

    if missing_env:
        env_range = _find_block_range(body, "environment")
        if env_range is not None:
            _, end = env_range
            # Insert just before ``end`` so new entries land at the bottom of
            # the existing environment list.
            body[end:end] = missing_env
        else:
            # No environment: block — append a fresh one to the body.
            body.append("    environment:")
            body.extend(missing_env)

    # ── Volume ───────────────────────────────────────────────────────────
    # Volume marker has no slug component — substring match on the joined body
    # is safe here (no prefix-shadowing risk).
    joined = "\n".join(body)
    if volume_marker not in joined:
        vol_range = _find_block_range(body, "volumes")
        if vol_range is not None:
            _, end = vol_range
            body.insert(end, _VAULT_VOLUME_TEMPLATE)
        else:
            body.append("    volumes:")
            body.append(_VAULT_VOLUME_TEMPLATE)

    return body


def _ensure_references_volume(body_lines: list[str]) -> list[str]:
    """Referenz-Dateien-Mount (ADR-053) für JEDEN Agent-Service — sonst sind
    die absoluten Pfade aus der Dispatch-Directive im Container unlesbar.
    Idempotent via Substring-Marker."""
    body = list(body_lines)
    if "/.mc/references:" not in "\n".join(body):
        vol_range = _find_block_range(body, "volumes")
        if vol_range is not None:
            _, end = vol_range
            body.insert(end, _REFERENCES_VOLUME_TEMPLATE)
        else:
            body.append("    volumes:")
            body.append(_REFERENCES_VOLUME_TEMPLATE)
    return body


def _ensure_omp_sessions_volume(body_lines: list[str], slug: str) -> list[str]:
    """Mounts the omp session-transcript directory for omp-harness services.

    Idempotent via substring marker, same insert-only pattern as
    ``_ensure_vault_entries``/``_ensure_references_volume``. Called only for
    services whose resolved image is ``OMP_IMAGE`` — see the caller in
    ``_rewrite_compose``.
    """
    marker = f"/.mc/agents/{slug}/omp-sessions:{_OMP_SESSIONS_TARGET}"
    body = list(body_lines)
    if marker not in "\n".join(body):
        line = f"      - ${{HOME}}/.mc/agents/{slug}/omp-sessions:{_OMP_SESSIONS_TARGET}"
        vol_range = _find_block_range(body, "volumes")
        if vol_range is not None:
            _, end = vol_range
            body.insert(end, line)
        else:
            body.append("    volumes:")
            body.append(line)
    return body


def _ensure_kimi_config_volume(body_lines: list[str], slug: str) -> list[str]:
    """Mounts the per-agent Kimi config dir (KIMI_CODE_HOME) for kimi-harness
    services.

    ~/.mc/agents/<slug>/kimi-config → /home/agent/.kimi-code (rw). Holds
    config.toml (entrypoint-rendered) AND the OAuth credentials/ files from
    the one-time per-agent `/login` — Kimi has no long-lived token, and
    refresh-token rotation kills copied credentials, so this mount is the
    ONLY thing that keeps the login alive across container recreates.
    Idempotent via substring marker, same pattern as _ensure_omp_sessions_volume.
    """
    marker = f"/.mc/agents/{slug}/kimi-config:/home/agent/.kimi-code"
    body = list(body_lines)
    if marker not in "\n".join(body):
        line = f"      - ${{HOME}}/.mc/agents/{slug}/kimi-config:/home/agent/.kimi-code"
        vol_range = _find_block_range(body, "volumes")
        if vol_range is not None:
            _, end = vol_range
            body.insert(end, line)
        else:
            body.append("    volumes:")
            body.append(line)
    return body


def _ensure_msg_delivery_mode(body_lines: list[str]) -> list[str]:
    """Ensure ``MSG_DELIVERY_MODE`` is present in the service's environment
    block (fleet default nudge+pull, W2.1 / ADR-071).

    Only poll.sh reads the variable — omp bridges ignore it, and agents
    without comm_v2 never receive messages in the first place, so injecting
    it into every agent service is safe. The compose substitution keeps a
    host-wide override knob (``MSG_DELIVERY_MODE=paste``).

    Idempotent: an existing MSG_DELIVERY_MODE entry (any value) is kept as-is
    so a deliberate per-service override survives re-rendering.
    """
    body = list(body_lines)
    if any(
        line.strip().startswith("- MSG_DELIVERY_MODE=") for line in body
    ):
        return body
    entry = "      - MSG_DELIVERY_MODE=${MSG_DELIVERY_MODE:-nudge}"
    env_range = _find_block_range(body, "environment")
    if env_range is not None:
        _, end = env_range
        body.insert(end, entry)
    else:
        body.append("    environment:")
        body.append(entry)
    return body


def _strip_agents_env_file(body_lines: list[str]) -> list[str]:
    """Remove ``docker/.env.agents`` from this service body's ``env_file`` block.

    Files rendered before the token-isolation fix carry a service-level
    ``env_file`` listing both ``docker/.env.shared`` and ``docker/.env.agents``.
    Only the agents file is dropped; the block itself stays (with
    ``docker/.env.shared``) because a service-level list overrides the anchor
    list — deleting the whole block would also be safe, but keeping it is the
    smaller change and does not depend on the anchor being intact.

    Idempotent: bodies without the entry are returned unchanged.
    """
    env_file_range = _find_block_range(body_lines, "env_file")
    if env_file_range is None:
        return list(body_lines)
    start, end = env_file_range
    body = list(body_lines)
    kept = [
        line for line in body[start + 1:end]
        if _AGENTS_ENV_FILE not in line
    ]
    if kept:
        body[start + 1:end] = kept
    else:
        del body[start:end]
    return body


def _anchor_images(content: str) -> dict[str, str]:
    """Map ``anchor name → the image it declares`` from the file itself.

    Read the anchors rather than assume them: what the module constants resolve
    to and what the operator's file actually says can differ. A file written
    before the registry prefix carries the bare ``mc-claude-agent:latest`` while
    the renderer now resolves ``ghcr.io/…/mc-claude-agent:latest`` — an agent
    that inherits the anchor in that file ends up on a tag no registry has.
    """
    lines = content.splitlines(keepends=False)
    n = len(lines)
    images: dict[str, str] = {}
    for j, line in enumerate(lines):
        decl = re.match(
            r"^\s*x-(?P<aname>[a-z0-9_-]+):\s*&(?P<aanchor>[a-z0-9_-]+)\s*$", line
        )
        if not decl:
            continue
        # Look ahead for `  image: ...` within the anchor block.
        for k in range(j + 1, min(j + 20, n)):
            m = re.match(r"^\s+image:\s*(\S+)\s*$", lines[k])
            if m:
                images[decl.group("aanchor")] = m.group(1).strip('"\'')
                break
    return images


def _rewrite_compose(
    content: str,
    image_overrides: dict[str, str],
    vault_writers: set[str] | None = None,
) -> str:
    """Rewrite per-service image assignments and inject per-agent vault mounts.

    - For each service `mc-agent-<slug>:` we look at its inherited anchor
      (`<<: *claude-agent-base` or `*openclaude-agent-base`). If the
      override image differs from what the anchor provides, we insert an
      explicit `image: <override>` line right after the `<<: *anchor` line.
      If an explicit `image:` line already exists, we replace its value.
    - When ``vault_writers`` contains an agent's slug, the renderer ensures
      the service body has:
        ``      - ${HOME}/.mc/vault:/vault:rw`` (volume mount)
        ``      - AGENT_VAULT_PATH=/vault/agents/<slug>``
        ``      - AGENT_VAULT_INBOX=/vault/_inbox``
        ``      - AGENT_SLUG=<slug>``
      Existing entries are detected and not duplicated (insert-only;
      removal is out of scope — agents that lose the scope keep entries
      until the file is regenerated from scratch).
    - Anchor blocks themselves are untouched — they remain the static base.
    - Indentation: 4 spaces (matches the existing file).

    Idempotent: rerunning produces the same output.
    """
    vault_writers = vault_writers or set()
    lines = content.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    n = len(lines)

    # Map anchor name → its image, read from the anchor blocks in THIS file.
    anchor_images = _anchor_images(content)

    while i < n:
        line = lines[i]
        svc_match = _SERVICE_RE.match(line)
        if not svc_match:
            out.append(line)
            i += 1
            continue

        slug = svc_match.group("slug")
        out.append(line)
        i += 1

        target_image = image_overrides.get(slug)
        wants_vault = slug in vault_writers
        # env_file stripping is always applied — no agent service may mount
        # docker/.env.agents (it holds every agent's token).

        # Walk through the service body until we hit the next top-level key
        # (no leading whitespace) or another service definition. Collect the
        # body locally so we can mutate it (image rewrite + vault inject)
        # before flushing to ``out``.
        body_lines: list[str] = []
        anchor_line_idx: int | None = None
        explicit_image_line_idx: int | None = None
        anchor_inherited_image: str | None = None

        while i < n:
            cur = lines[i]
            # End of service body? Either another mc-agent-* service or top-level.
            if _SERVICE_RE.match(cur):
                break
            if cur and not cur.startswith(" ") and not cur.startswith("\t"):
                # New top-level (services, networks, volumes, comment is fine).
                # Only break on a real key (line ending in `:`).
                if cur.endswith(":") or re.match(r"^[a-zA-Z_]", cur):
                    break

            body_lines.append(cur)
            anchor_match = _ANCHOR_RE.match(cur)
            if anchor_match:
                anchor_line_idx = len(body_lines) - 1
                anchor_inherited_image = anchor_images.get(anchor_match.group("anchor"))

            img_match = re.match(r"^(\s*)image:\s*(\S+)\s*$", cur)
            if img_match:
                explicit_image_line_idx = len(body_lines) - 1

            i += 1

        # Apply image override (if any).
        if target_image is not None:
            if explicit_image_line_idx is not None:
                indent_match = re.match(r"^(\s*)", body_lines[explicit_image_line_idx])
                indent = indent_match.group(1) if indent_match else "    "
                body_lines[explicit_image_line_idx] = f"{indent}image: {target_image}"
            elif anchor_inherited_image != target_image and anchor_line_idx is not None:
                indent_match = re.match(r"^(\s*)", body_lines[anchor_line_idx])
                indent = indent_match.group(1) if indent_match else "    "
                body_lines.insert(
                    anchor_line_idx + 1,
                    f"{indent}image: {target_image}",
                )

        # Resolve the FINAL image for this service (override > pre-existing
        # explicit image: line, unmodified since no override applies > anchor
        # default) so the omp-sessions mount below is decided the same way
        # regardless of whether this run carries a DB-sourced override or is
        # just re-rewriting an already-correct static file (idempotency check
        # in tests calls _rewrite_compose with image_overrides={}).
        if target_image is not None:
            final_image = target_image
        elif explicit_image_line_idx is not None:
            existing_img_match = re.match(r"^\s*image:\s*(\S+)\s*$", body_lines[explicit_image_line_idx])
            final_image = existing_img_match.group(1) if existing_img_match else anchor_inherited_image
        else:
            final_image = anchor_inherited_image

        # Inject vault mount + env vars if this agent has vault:write scope.
        if wants_vault:
            body_lines = _ensure_vault_entries(body_lines, slug)

        # omp headless harness (ADR-045) needs its session-transcript dir
        # mounted so the token harvester can see it — see _OMP_SESSIONS_TARGET.
        if _image_is(final_image, "mc-omp-agent"):
            body_lines = _ensure_omp_sessions_volume(body_lines, slug)

        # kimi harness needs its KIMI_CODE_HOME mount (config + OAuth files).
        if _image_is(final_image, "mc-kimi-agent"):
            body_lines = _ensure_kimi_config_volume(body_lines, slug)

        # Referenz-Dateien-Mount für ALLE Agent-Services (ADR-053).
        body_lines = _ensure_references_volume(body_lines)

        # Token isolation: drop docker/.env.agents from any service-level
        # env_file (legacy renders) — see _AGENTS_ENV_FILE.
        body_lines = _strip_agents_env_file(body_lines)

        # Fleet default nudge+pull (W2.1, ADR-071) for every agent service.
        body_lines = _ensure_msg_delivery_mode(body_lines)

        out.extend(body_lines)

    rendered = "\n".join(out)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _needs_explicit_image(
    image: str | None,
    anchor: str,
    anchor_default_image: str,
    anchor_images: dict[str, str] | None,
) -> bool:
    """Would inheriting ``*anchor`` put this agent on the wrong image?

    Three cases, and the middle one is the bug this replaces:

    * The anchor in the file declares a plain image name that differs from the
      resolved one → spell it out. This is the fresh-install failure: the file
      says ``mc-claude-agent:latest``, MC_AGENT_IMAGE_PREFIX defaults to the
      GHCR registry, and the container comes up with ``pull access denied``.
    * The anchor computes its image from ``${MC_AGENT_IMAGE_PREFIX…}`` — the
      same two variables the renderer uses. Inheriting is then not just safe
      but better: a fixed line would freeze the value at render time and take
      away developer mode (``MC_AGENT_IMAGE_PREFIX=``).
    * The anchor is not in the file at all (older file, newer harness) → there
      is nothing to inherit, so write it.

    Without ``anchor_images`` (direct unit calls) we fall back to comparing
    against the module constant, which for claude/openclaude is the same value
    by construction — i.e. the old, image-less behaviour.
    """
    if image is None:
        return False
    if anchor_images is None:
        return image != anchor_default_image

    declared = anchor_images.get(anchor)
    if declared is None:
        return True  # nothing to inherit
    if "${" in declared:
        return False  # the anchor resolves itself, from the same variables
    return declared != image


def _build_new_agent_block(
    slug: str,
    image: str | None,
    is_vault_writer: bool,
    anchor_images: dict[str, str] | None = None,
) -> str:
    """Render a full service block for a new cli-bridge agent not present in the
    static compose template.

    - Anchor: ``*claude-agent-base`` for CLAUDE_IMAGE (default), or
      ``*openclaude-agent-base`` for OPENCLAUDE_IMAGE. An explicit ``image:``
      line is emitted only when inheriting the anchor would land the agent on
      the wrong image — see ``_needs_explicit_image``.
    - ``anchor_images``: what the anchors in the TARGET FILE declare, from
      ``_anchor_images()``. Defaults to None for direct unit calls, which then
      falls back to comparing against the module constants (i.e. never emit) —
      production callers pass the real map.
    - Env: standard 7-var set (AGENT_NAME, MC_API_URL, MC_TOKEN, RECYCLER,
      VAULT_PATH, VAULT_INBOX, AGENT_SLUG).
    - Volumes: 4 standard mounts + optional vault :rw when ``is_vault_writer``.

    ENVKEY = slug.upper().replace('-', '_').
    """
    envkey = slug.upper().replace("-", "_")
    # ADR-045: three-way anchor selection — omp agents hang off the dedicated
    # `omp-agent-base` anchor; openclaude off `openclaude-agent-base`; the
    # anthropic fleet off the default `claude-agent-base`.
    if _image_is(image, "mc-omp-agent"):
        anchor = "omp-agent-base"
        anchor_default_image = OMP_IMAGE
    elif _image_is(image, "mc-kimi-agent"):
        anchor = "kimi-agent-base"
        anchor_default_image = KIMI_IMAGE
    elif _image_is(image, "mc-agent-base"):
        anchor = "openclaude-agent-base"
        anchor_default_image = OPENCLAUDE_IMAGE
    else:
        anchor = "claude-agent-base"
        anchor_default_image = CLAUDE_IMAGE

    lines: list[str] = [
        f"  mc-agent-{slug}:",
        f"    <<: *{anchor}",
    ]
    if _needs_explicit_image(image, anchor, anchor_default_image, anchor_images):
        lines.append(f"    image: {image}")

    lines += [
        f"    container_name: mc-agent-{slug}",
        # Explicit service-level env_file overrides the anchor's env_file in
        # YAML merge semantics (service-level list replaces the anchor list).
        # We therefore repeat docker/.env.shared here so CLAUDE_CODE_OAUTH_TOKEN
        # and GH_TOKEN remain available.  docker/.env.agents is deliberately
        # NOT listed — it holds every agent's token (see _AGENTS_ENV_FILE).
        "    env_file:",
        f"      - {_SHARED_ENV_FILE}",
        "    environment:",
        f"      - AGENT_NAME={slug}",
        "      - MC_API_URL=${MC_API_URL:-http://backend:8000}",
        f"      - MC_TOKEN=${{MC_TOKEN_{envkey}}}",
        "      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}",
        # Fleet default is nudge+pull (W2.1, ADR-071): poll.sh pastes a short
        # 📬 nudge and the agent pulls content via `mc inbox`. Only poll.sh
        # reads this var — omp bridges ignore it, and agents without comm_v2
        # never receive messages in the first place. Override host-wide via
        # MSG_DELIVERY_MODE=paste in the compose environment.
        "      - MSG_DELIVERY_MODE=${MSG_DELIVERY_MODE:-nudge}",
        f"      - AGENT_VAULT_PATH=/vault/agents/{slug}",
        "      - AGENT_VAULT_INBOX=/vault/_inbox",
        f"      - AGENT_SLUG={slug}",
        "    volumes:",
        f"      - ${{HOME}}/.mc/agents/{slug}/claude-config:/home/agent/.claude",
        "      - ${HOME}/.mc/mcp-servers:/mc-servers:ro",
        f"      - ${{HOME}}/.mc/workspaces/{slug}:/workspace",
        f"      - ${{HOME}}/.mc/deliverables/{slug}:/deliverables",
        _REFERENCES_VOLUME_TEMPLATE,
    ]
    if _image_is(image, "mc-omp-agent"):
        lines.append(f"      - ${{HOME}}/.mc/agents/{slug}/omp-sessions:{_OMP_SESSIONS_TARGET}")
    if _image_is(image, "mc-kimi-agent"):
        lines.append(f"      - ${{HOME}}/.mc/agents/{slug}/kimi-config:/home/agent/.kimi-code")
    if is_vault_writer:
        lines.append("      - ${HOME}/.mc/vault:/vault:rw")

    return "\n".join(lines)


async def render_compose_agents(
    session: AsyncSession,
    compose_path: Path | None = None,
) -> str:
    """Generate compose YAML by overlaying DB-driven image overrides on the
    existing static file, then appending full service blocks for any new
    cli-bridge agents whose service is not already present in the file.

    - Reads all cli-bridge agents and resolves their target image.
    - Falls back to the static anchor assignment when runtime_id is None or
      pick_image_for_runtime returns None.
    - For agents whose ``mc-agent-<slug>:`` service is NOT already in the
      rendered content, appends a full service block at the end of the file.
    - Returns the rendered string (does not write).
    """
    path = compose_path or DEFAULT_COMPOSE_PATH
    static = _read_compose_or_template(path)

    result = await session.exec(
        select(Agent).where(Agent.agent_runtime == "cli-bridge")
    )
    agents = list(result.all())

    overrides: dict[str, str] = {}
    vault_writers: set[str] = set()
    new_agents: list[tuple[str, str | None]] = []  # (slug, resolved_image_or_None)
    for ag in agents:
        slug = _agent_slug(ag)
        resolved_image: str | None = None

        # Image overrides require a runtime binding; vault scope does not.
        if ag.runtime_id is not None:
            rt = await session.get(Runtime, ag.runtime_id)
            resolved_image = pick_image_for_harness(getattr(ag, "harness", None), rt)
            if resolved_image is not None:
                overrides[slug] = resolved_image

        # ``scopes is None`` is treated as "all scopes" per CLAUDE.md
        # backward-compat (agents created before scope-system rollout).
        # ``scopes == []`` is *also* "all scopes" by the same rule.
        scopes = ag.scopes
        if not scopes or Scope.VAULT_WRITE.value in scopes:
            vault_writers.add(slug)

        # Track agents not yet in the static file so we can append them.
        if f"mc-agent-{slug}:" not in static:
            new_agents.append((slug, resolved_image))

    # Note: _rewrite_compose is always called (even with empty overrides/vault_writers)
    # because it also strips env_file: docker/.env.agents from every agent service
    # (token isolation — see _AGENTS_ENV_FILE).
    rendered = _rewrite_compose(static, overrides, vault_writers=vault_writers)

    # Append full service blocks for agents not already present in the file.
    # Blocks must land inside the ``services:`` section — i.e. BEFORE any
    # top-level sibling keys (``networks:``, ``volumes:``, etc.).  We locate
    # the insertion point once (before the loop) so multiple new agents land
    # contiguously inside services rather than after non-service keys.
    if new_agents:
        rendered = _insert_new_agent_blocks(rendered, new_agents, vault_writers)

    # Self-healing: a file left behind by the older code with a bare
    # ``services:`` and no agents becomes valid again on the next render.
    return _restore_empty_services_map(rendered)


def _insert_new_agent_blocks(
    content: str,
    new_agents: list[tuple[str, str | None]],
    vault_writers: set[str],
) -> str:
    """Insert full service blocks for new agents inside the ``services:``
    section of *content* (before the first top-level sibling key such as
    ``networks:`` or ``volumes:``).

    Strategy:
    - Find the last line that belongs to the ``services:`` section by scanning
      backwards from the end for the last ``  mc-agent-`` line (2-space indent),
      then advance past its body until we hit a non-indented key or EOF.
    - If no services section boundary is found, fall back to appending at the
      file end (safe but unconventional).
    - Skip any agent whose service key is already present (dedup guard).
    """
    lines = content.splitlines(keepends=False)

    # Locate insertion point: the line index just BEFORE the first top-level
    # key that follows the ``services:`` block.  A top-level key is a line
    # that starts with a non-space, non-comment character and ends with ``:``.
    # We start scanning from the line after ``services:`` until we find a
    # sibling top-level key.
    # ``services: {}`` is matched too, not just ``services:``. Reason: the
    # shipped template (docker-compose.agents.example.yml) deliberately carries
    # NO agents — otherwise every installation would start with its author's
    # fleet. A bare ``services:`` would be invalid compose though ("services
    # must be a mapping", verified live), so the empty mapping sits there.
    # Without this branch the renderer would not find the section and would
    # append the first agent at the END OF THE FILE — behind
    # ``networks:``/``volumes:``, where it lands as a top-level key and
    # destroys the file.
    services_header_idx: int | None = None
    services_header_is_empty_map = False
    for i, line in enumerate(lines):
        m = re.match(r"^services:\s*(\{\s*\})?\s*$", line)
        if m:
            services_header_idx = i
            services_header_is_empty_map = m.group(1) is not None
            break

    # Default: insert at end of file content.
    insert_before: int = len(lines)

    if services_header_idx is not None:
        # Walk forward from services header to find the first sibling top-level
        # key (not indented, ends with ':', is not a comment).
        for j in range(services_header_idx + 1, len(lines)):
            ln = lines[j]
            if ln and not ln.startswith(" ") and not ln.startswith("\t") and not ln.startswith("#"):
                if re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*:\s*$", ln):
                    insert_before = j
                    break

    # What the anchors in THIS file declare — not what the constants resolve to.
    anchors = _anchor_images(content)

    # Build the text to insert (one block per new agent, blank-line separated).
    blocks_to_insert: list[str] = []
    for slug, resolved_image in new_agents:
        if f"mc-agent-{slug}:" in content:
            continue  # Already present — skip (dedup guard).
        is_vault_writer = slug in vault_writers
        block = _build_new_agent_block(
            slug, resolved_image, is_vault_writer, anchor_images=anchors
        )
        blocks_to_insert.append(block)

    if not blocks_to_insert:
        return content

    # First agent into an empty template: the empty mapping has to go, or
    # ``services: {}`` would sit above real entries — YAML then takes the empty
    # mapping and ignores everything below it.
    if services_header_is_empty_map and services_header_idx is not None:
        lines[services_header_idx] = "services:"

    # Insert all blocks at the computed position, each preceded by a blank line.
    insert_text = "\n" + "\n\n".join(blocks_to_insert) + "\n"
    before = "\n".join(lines[:insert_before]).rstrip("\n")
    after = "\n".join(lines[insert_before:])
    result = before + insert_text + (("\n" + after) if after.strip() else after)
    if not result.endswith("\n"):
        result += "\n"
    return result


async def write_compose_agents(
    session: AsyncSession,
    compose_path: Path | None = None,
) -> dict[str, str]:
    """Render and atomically replace the compose file.

    Steps:
      1. Acquire global compose-write lock (prevents concurrent renders from
         different agents racing to write the shared file).
      2. Render via render_compose_agents (reads fresh DB state inside lock).
      3. Backup current file to <path>.bak (overwrite previous backup).
      4. Write rendered content to <path>.tmp.
      5. os.replace(.tmp, target) — atomic on POSIX.
      6. Release lock.

    Lock: COMPOSE_WRITE_LOCK_KEY (mc:compose:agents-yml:write), TTL 60s.
    The lock is acquired here, INSIDE any per-agent runtime-switch lock held
    by the caller — never the other way around (see lock-hierarchy comment at
    the top of this module).

    Returns: {"path": str, "backup": str, "bytes": str, "changed": "true|false"}.
    """
    redis = await get_redis()
    # nx=True: only set if not exists (acquire). The lock value is irrelevant.
    acquired = await redis.set(
        COMPOSE_WRITE_LOCK_KEY, "1", nx=True, ex=COMPOSE_WRITE_LOCK_TTL
    )
    if not acquired:
        # Another switch is currently writing the compose file. Wait briefly
        # and retry once — compose writes are fast (<100ms). If still locked,
        # raise so the caller's switch-service can surface the error.
        import asyncio
        await asyncio.sleep(2)
        acquired = await redis.set(
            COMPOSE_WRITE_LOCK_KEY, "1", nx=True, ex=COMPOSE_WRITE_LOCK_TTL
        )
        if not acquired:
            raise RuntimeError(
                "compose write lock busy — concurrent switch in progress"
            )
    try:
        path = compose_path or DEFAULT_COMPOSE_PATH
        # Render INSIDE the lock so we read DB state after the lock is held,
        # preventing a TOCTOU where another writer commits a DB change between
        # our DB read and our file write.
        rendered = await render_compose_agents(session, compose_path=path)
        target = Path(path)
        tmp = target.with_suffix(target.suffix + ".tmp")
        bak = target.with_suffix(target.suffix + ".bak")

        previous = target.read_text(encoding="utf-8") if target.exists() else ""
        if previous == rendered:
            return {
                "path": str(target),
                "backup": str(bak),
                "bytes": str(len(rendered)),
                "changed": "false",
            }

        if target.exists():
            bak.write_text(previous, encoding="utf-8")
            os.chmod(bak, COMPOSE_FILE_MODE)  # same fleet, same mode
        tmp.write_text(rendered, encoding="utf-8")
        # chmod the tmpfile BEFORE the rename: after os.replace the target is
        # live, and a window where it sits world-readable is the thing we are
        # closing.
        os.chmod(tmp, COMPOSE_FILE_MODE)
        os.replace(tmp, target)
        logger.info("compose_renderer wrote %s (%d bytes)", target, len(rendered))
        return {
            "path": str(target),
            "backup": str(bak),
            "bytes": str(len(rendered)),
            "changed": "true",
        }
    finally:
        await redis.delete(COMPOSE_WRITE_LOCK_KEY)


def _restore_empty_services_map(content: str) -> str:
    """Put ``services: {}`` back when no entry is left.

    ``_insert_new_agent_blocks`` replaces the empty mapping with a bare
    ``services:`` for the first agent. Without this way back that is a one-way
    street: remove the last agent again and a naked ``services:`` remains —
    invalid compose ("services must be a mapping", verified live). From then on
    EVERY compose command fails: start-all.sh step 3, the container recreate,
    the runtime switch.

    Pure function. Changes nothing while an entry still sits under
    ``services:``, and nothing when ``{}`` is already there.
    """
    lines = content.splitlines(keepends=True)
    header_idx: int | None = None
    for idx, line in enumerate(lines):
        if re.match(r"^services:[ \t]*$", line.rstrip("\n").rstrip("\r")):
            header_idx = idx
            break
    if header_idx is None:
        return content

    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        # Blank lines and comments are not entries — YAML does not see them.
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            break  # next top-level key: the section ends with no entry
        return content  # a real entry is still there

    eol = "\n" if lines[header_idx].endswith("\n") else ""
    lines[header_idx] = "services: {}" + eol
    return "".join(lines)


def prune_compose_agent_block(content: str, slug: str) -> tuple[str, bool]:
    """Remove the ``mc-agent-<slug>:`` service block from compose YAML.

    render_compose_agents is *additive* — it overlays image overrides and
    appends new service blocks, but never removes one. So a deleted cli-bridge
    agent's block lingered in docker-compose.agents.yml forever (found
    2026-07-11), and `docker compose up` kept trying to recreate its container.

    This is a *targeted* prune: it removes only the block for the exact slug
    the caller just deleted — never inferred from DB state, which could wrongly
    drop a static anchor agent whose DB row is momentarily absent.

    A service block starts at a line ``^  mc-agent-<slug>:`` (2-space indent
    under ``services:``) and runs until the next line at ≤2-space indent
    (the next service, or a top-level key) or EOF. Pure function: returns
    ``(new_content, removed)``.
    """
    lines = content.splitlines(keepends=True)
    header = f"  mc-agent-{slug}:"
    start: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n").rstrip("\r")
        if stripped == header:
            start = idx
            break
    if start is None:
        return content, False

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        line = lines[idx]
        # Blank / whitespace-only lines belong to the block (trailing spacing).
        if not line.strip():
            continue
        # A line whose first non-space column is ≤ 2 ends the block: either a
        # sibling service ("  other:") or a top-level key ("services:").
        indent = len(line) - len(line.lstrip(" "))
        if indent <= 2:
            end = idx
            break

    del lines[start:end]
    # If that was the last agent the empty mapping has to come back — a bare
    # ``services:`` would leave the file invalid.
    return _restore_empty_services_map("".join(lines)), True


async def prune_compose_agent(slug: str, compose_path: Path | None = None) -> dict:
    """Remove a deleted cli-bridge agent's service block from the compose file.

    Lock-protected + atomic + best-effort, mirroring write_compose_agents:
    acquire the shared compose-write lock, back up to <path>.bak, write via
    <path>.tmp + os.replace. A no-op (no matching block, or file absent)
    writes nothing. Callers in the delete path must treat this as best-effort
    — a lock/IO failure must never block the DB delete.

    Returns ``{"removed": "true|false", "path": str, "changed": "true|false"}``.
    """
    path = Path(compose_path or DEFAULT_COMPOSE_PATH)
    if not path.exists():
        return {"removed": "false", "path": str(path), "changed": "false"}

    redis = await get_redis()
    acquired = await redis.set(
        COMPOSE_WRITE_LOCK_KEY, "1", nx=True, ex=COMPOSE_WRITE_LOCK_TTL
    )
    if not acquired:
        import asyncio
        await asyncio.sleep(2)
        acquired = await redis.set(
            COMPOSE_WRITE_LOCK_KEY, "1", nx=True, ex=COMPOSE_WRITE_LOCK_TTL
        )
        if not acquired:
            raise RuntimeError(
                "compose write lock busy — concurrent switch in progress"
            )
    try:
        previous = path.read_text(encoding="utf-8")
        rendered, removed = prune_compose_agent_block(previous, slug)
        if not removed or rendered == previous:
            return {"removed": "false", "path": str(path), "changed": "false"}

        tmp = path.with_suffix(path.suffix + ".tmp")
        bak = path.with_suffix(path.suffix + ".bak")
        bak.write_text(previous, encoding="utf-8")
        os.chmod(bak, COMPOSE_FILE_MODE)
        tmp.write_text(rendered, encoding="utf-8")
        os.chmod(tmp, COMPOSE_FILE_MODE)
        os.replace(tmp, path)
        logger.info("compose_renderer pruned mc-agent-%s from %s", slug, path)
        return {"removed": "true", "path": str(path), "changed": "true"}
    finally:
        await redis.delete(COMPOSE_WRITE_LOCK_KEY)
