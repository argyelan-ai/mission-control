"""docker-compose.agents.yml generator (Phase 15).

Renders the agents compose file from DB state so the image per agent follows
agent.runtime_id → runtime.runtime_type instead of being hardcoded via static
YAML anchors.

Image rules:
- runtime.runtime_type == "cloud"                                  → mc-claude-agent:latest
- runtime.runtime_type in {vllm_docker, lmstudio, openai_compatible, unsloth} → mc-agent-base:latest
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

CLAUDE_IMAGE = "mc-claude-agent:latest"
OPENCLAUDE_IMAGE = "mc-agent-base:latest"
# ADR-045: third harness image — omp headless driver (bridge.py --serve) instead
# of an interactive openclaude pane. Selected by runtime_type == "omp".
OMP_IMAGE = "mc-omp-agent:latest"

HARNESS_IMAGES: dict[str, str] = {
    "claude": CLAUDE_IMAGE,
    "openclaude": OPENCLAUDE_IMAGE,
    "omp": OMP_IMAGE,
}

# Token-hardening (fix/agent-token-recreate-hardening):
# MC_TOKEN_<AGENTNAME> vars live in docker/.env.agents (symlink under
# ~/.mc/secrets/…/docker/.env.agents).  By emitting this env_file for every
# rendered agent service we ensure the variables are available inside the
# container even when the caller forgot --env-file docker/.env.agents on
# `docker compose up --force-recreate`.
# Path is relative to the project root — same convention as docker/.env.shared
# used in the anchor blocks.
_AGENTS_ENV_FILE = "docker/.env.agents"
# The shared env file already referenced by anchor blocks.  We re-declare it
# at service level whenever we emit a service-level env_file list so that YAML
# merge semantics (service-level list replaces the anchor list, not merges) do
# not silently drop CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, TAVILY_API_KEY, etc.
_SHARED_ENV_FILE = "docker/.env.shared"

# Compose path: docker/docker-compose.agents.yml relative to repo root.
# Repo root comes from settings.mc_repo_path (MC_REPO_PATH env — set by
# setup.sh; the checkout may have any folder name). Tests inject the path.
DEFAULT_COMPOSE_PATH = (
    Path(settings.mc_repo_path) / "docker" / "docker-compose.agents.yml"
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
    if rt_type in ("vllm_docker", "lmstudio", "openai_compatible", "unsloth", "cloud"):
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


def _ensure_env_file_entry(body_lines: list[str]) -> list[str]:
    """Ensure ``docker/.env.agents`` appears in this service body's ``env_file``
    block.

    Two cases:
    - An ``env_file:`` block already exists in the body (service-level override
      was previously added) → append ``docker/.env.agents`` if not present.
    - No ``env_file:`` block in body (normal case — the service relies on the
      anchor's env_file) → create a new block that explicitly lists BOTH
      ``docker/.env.shared`` (the anchor's file) and ``docker/.env.agents`` so
      that YAML merge semantics (service-level list *replaces* the anchor list)
      do not silently drop the shared credentials.

    Idempotent: re-running with the same body produces the same output.
    """
    agents_env = _AGENTS_ENV_FILE

    # Early-out: already present.
    if any(agents_env in line for line in body_lines):
        return list(body_lines)

    body = list(body_lines)
    env_file_range = _find_block_range(body, "env_file")
    if env_file_range is not None:
        # Existing service-level env_file block — append .env.agents.
        _, end = env_file_range
        body.insert(end, f"      - {agents_env}")
    else:
        # No service-level env_file block — add one with both files.
        body.append("    env_file:")
        body.append(f"      - {_SHARED_ENV_FILE}")
        body.append(f"      - {agents_env}")

    return body


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

    # Map anchor name → its image (read from existing anchor blocks).
    anchor_images: dict[str, str] = {}
    for j, line in enumerate(lines):
        anchor_decl = re.match(r"^\s*x-(?P<aname>[a-z0-9_-]+):\s*&(?P<aanchor>[a-z0-9_-]+)\s*$", line)
        if anchor_decl:
            # Look ahead for `  image: ...` within the anchor block (until next top-level key).
            for k in range(j + 1, min(j + 20, n)):
                m = re.match(r"^\s+image:\s*(\S+)\s*$", lines[k])
                if m:
                    anchor_images[anchor_decl.group("aanchor")] = m.group(1)
                    break

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
        # env_file injection is always applied — every agent service needs the
        # MC_TOKEN_<NAME> variables available inside the container regardless of
        # how compose was invoked (defense layer 1 against silent blank MC_TOKEN).

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

        # Inject vault mount + env vars if this agent has vault:write scope.
        if wants_vault:
            body_lines = _ensure_vault_entries(body_lines, slug)

        # Referenz-Dateien-Mount für ALLE Agent-Services (ADR-053).
        body_lines = _ensure_references_volume(body_lines)

        # Defense layer 1: ensure docker/.env.agents is in every agent service's
        # env_file so MC_TOKEN_<NAME> vars are available at container runtime
        # even when compose is called without --env-file docker/.env.agents.
        body_lines = _ensure_env_file_entry(body_lines)

        out.extend(body_lines)

    rendered = "\n".join(out)
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def _build_new_agent_block(slug: str, image: str | None, is_vault_writer: bool) -> str:
    """Render a full service block for a new cli-bridge agent not present in the
    static compose template.

    - Anchor: ``*claude-agent-base`` for CLAUDE_IMAGE (default), or
      ``*openclaude-agent-base`` for OPENCLAUDE_IMAGE. An explicit ``image:``
      line is emitted only when the resolved image differs from the anchor's
      default.
    - Env: standard 7-var set (AGENT_NAME, MC_API_URL, MC_TOKEN, RECYCLER,
      VAULT_PATH, VAULT_INBOX, AGENT_SLUG).
    - Volumes: 4 standard mounts + optional vault :rw when ``is_vault_writer``.

    ENVKEY = slug.upper().replace('-', '_').
    """
    envkey = slug.upper().replace("-", "_")
    # ADR-045: three-way anchor selection — omp agents hang off the dedicated
    # `omp-agent-base` anchor; openclaude off `openclaude-agent-base`; the
    # anthropic fleet off the default `claude-agent-base`.
    if image == OMP_IMAGE:
        anchor = "omp-agent-base"
        anchor_default_image = OMP_IMAGE
    elif image == OPENCLAUDE_IMAGE:
        anchor = "openclaude-agent-base"
        anchor_default_image = OPENCLAUDE_IMAGE
    else:
        anchor = "claude-agent-base"
        anchor_default_image = CLAUDE_IMAGE

    lines: list[str] = [
        f"  mc-agent-{slug}:",
        f"    <<: *{anchor}",
    ]
    # Only emit explicit image: when it differs from the anchor default.
    if image is not None and image != anchor_default_image:
        lines.append(f"    image: {image}")

    lines += [
        f"    container_name: mc-agent-{slug}",
        # Explicit service-level env_file overrides the anchor's env_file in
        # YAML merge semantics (service-level list replaces the anchor list).
        # We therefore repeat docker/.env.shared here so CLAUDE_CODE_OAUTH_TOKEN
        # and GH_TOKEN remain available, and add docker/.env.agents to ensure
        # MC_TOKEN_<NAME> vars are present even without --env-file at compose-up.
        "    env_file:",
        f"      - {_SHARED_ENV_FILE}",
        f"      - {_AGENTS_ENV_FILE}",
        "    environment:",
        f"      - AGENT_NAME={slug}",
        "      - MC_API_URL=${MC_API_URL:-http://backend:8000}",
        f"      - MC_TOKEN=${{MC_TOKEN_{envkey}}}",
        "      - AGENT_RECYCLER_ENABLED=${AGENT_RECYCLER_ENABLED:-true}",
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
    if not path.exists():
        raise FileNotFoundError(f"compose template not found: {path}")
    static = path.read_text(encoding="utf-8")

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
    # because it now also injects env_file: docker/.env.agents into every agent service
    # (defense layer 1 against blank MC_TOKEN when --env-file is omitted at compose-up).
    rendered = _rewrite_compose(static, overrides, vault_writers=vault_writers)

    # Append full service blocks for agents not already present in the file.
    # Blocks must land inside the ``services:`` section — i.e. BEFORE any
    # top-level sibling keys (``networks:``, ``volumes:``, etc.).  We locate
    # the insertion point once (before the loop) so multiple new agents land
    # contiguously inside services rather than after non-service keys.
    if new_agents:
        rendered = _insert_new_agent_blocks(rendered, new_agents, vault_writers)

    return rendered


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
    services_header_idx: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^services:\s*$", line):
            services_header_idx = i
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

    # Build the text to insert (one block per new agent, blank-line separated).
    blocks_to_insert: list[str] = []
    for slug, resolved_image in new_agents:
        if f"mc-agent-{slug}:" in content:
            continue  # Already present — skip (dedup guard).
        is_vault_writer = slug in vault_writers
        block = _build_new_agent_block(slug, resolved_image, is_vault_writer)
        blocks_to_insert.append(block)

    if not blocks_to_insert:
        return content

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
        tmp.write_text(rendered, encoding="utf-8")
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
