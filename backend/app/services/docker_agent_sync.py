"""
Docker Agent File Sync — renders MC config files from the Jinja2 templates,
syncs them into the DB AND writes them into the claude-config bind mount.

See ADR-006 (single source of truth: templates -> DB -> files).

For Docker V2 agents, this is the transport mechanism between MC and the
openclaude subprocess in the container. The files are written to
$HOME_HOST/.mc/agents/{slug}/claude-config/ and are visible in the
container under /home/agent/.claude/ (via docker-compose volume mount).

The container's entrypoint.sh reads SOUL.md and passes it as
--append-system-prompt to openclaude. TOOLS.md/HEARTBEAT.md/USER.md/
MEMORY.md are available in the same dir and can be referenced by the LLM.

IMPORTANT regarding DB consistency:
So that the UI (GET /agents/{id}/config reads agent.soul_md/...) and
the agent (reads the *.md files in the container) ALWAYS see the same thing,
this function writes on every run:
  Template -> DB field -> File

This guarantees UI and agent stay in sync after every sync-config call.
Consequence: direct UI edits to agent.soul_md get overwritten on the next
sync. Changes must be made in the TEMPLATE (see mc-agent-soul-edit).

Exceptions:
- TOOLS.md comes from agent.tools_md (filled by tools_md_builder with raw_token).
  Not overwritten here — only mirrored into the file.
- MEMORY.md is maintained by the agent itself (knowledge updates). Initially
  from the template, afterwards respects agent updates (not overwritten when DB is populated).
"""
import logging
import os
from pathlib import Path

from app.config import settings

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.secrets_helper import get_secret_plaintext_by_id
from app.services.template_renderer import build_agent_context, render_agent_file

logger = logging.getLogger("mc.docker_agent_sync")

# Host path from the backend container context (bind-mounted via docker-compose).
# HOME_HOST is set explicitly in the backend container (=host $HOME, e.g.
# /Users/<login>); HOME only gives the container's own home (/home/mcuser)
# and would be wrong.
_HOME_HOST = os.environ.get("HOME_HOST", os.path.expanduser("~"))
AGENTS_DIR = Path(_HOME_HOST) / ".mc" / "agents"


def _agent_slug(agent: Agent) -> str:
    """Slug used to look up the agent directory.

    Convention (from cli-bridge.py): lowercase, spaces replaced by '-',
    no special characters. We use the name as the base.
    """
    return agent.name.lower().replace(" ", "-")


def _sanitize_env_val(value: str) -> str:
    """Strip CR and LF characters from a value before writing it to an .env file.

    Defense-in-depth against newline injection: a runtime.endpoint or
    model_identifier containing \\n / \\r would split into multiple env-file
    lines, potentially overriding subsequent keys (e.g. OPENAI_API_KEY).
    Only admins create runtimes today, so exploitability is low — but the
    pattern is dangerous if multi-tenancy or automated runtime creation is
    added later.

    Raises ValueError if the sanitized value differs from the input, so callers
    learn that an unsafe value was attempted (logged, not silently swallowed).
    """
    cleaned = value.replace("\r", "").replace("\n", "")
    if cleaned != value:
        raise ValueError(
            f"env value contains newline characters — injection rejected: {value!r}"
        )
    return cleaned


async def sync_docker_agent_files(
    session: AsyncSession,
    agent: Agent,
) -> dict[str, str]:
    """Renders templates, writes to the DB, and to the claude-config bind mount.

    Flow per file (Template -> DB -> File):
      1. Render the template from backend/templates/ with the agent context
      2. Update agent.<field> in the DB
      3. Write the file in the claude-config directory
      4. session.commit() at the end

    Args:
        session: DB session (used for the team query and commit).
        agent: The agent whose files should be synced.

    Returns:
        dict of filename -> status. status is one of:
        - "written"          (file + DB succeeded)
        - "respected (agent-managed)" (memory: present, not overwritten)
        - "skipped (empty)"  (source empty)
        - "error: msg"       (exception while rendering or writing)
        - "_error: msg"      (global error, e.g. directory not found)
        - "_skipped: host runtime" (agent is host runtime, no Docker sync needed)
    """
    # Host agents (e.g. Boss) manage their own claude-config under
    # ~/.mc/agents/{slug}-host/, not the container path. Skip Docker sync
    # so the host Boss doesn't get its own config overwritten.
    if getattr(agent, "agent_runtime", None) == "host":
        logger.debug(
            "Skipping host agent %s (runtime=%s)", agent.name, agent.agent_runtime
        )
        return {"_skipped": "host runtime"}

    slug = _agent_slug(agent)
    claude_config_dir = AGENTS_DIR / slug / "claude-config"

    if not claude_config_dir.exists():
        msg = f"claude-config dir not found: {claude_config_dir}"
        logger.warning("sync_docker_agent_files(%s): %s", agent.name, msg)
        return {"_error": msg}

    # Team context: other agents on the same board (for Jinja2 team list)
    agents_on_board: list[Agent] = []
    if agent.board_id:
        result = await session.exec(
            select(Agent).where(Agent.board_id == agent.board_id)
        )
        agents_on_board = list(result.all())

    context = build_agent_context(
        agent,
        board_id=str(agent.board_id) if agent.board_id else None,
        agents_on_board=agents_on_board,
    )

    results: dict[str, str] = {}

    # 1. SOUL.md: Template -> DB -> File (always overwrite)
    #    Orchestrated by MC, the operator edits via the template.
    #    HEARTBEAT.md removed in migration 0125 — was rendered but never read
    #    by agents (only SOUL.md is injected via --append-system-prompt).
    for filename, template_name, db_field in [
        ("SOUL.md", "SOUL.md.j2", "soul_md"),
    ]:
        try:
            content = render_agent_file(template_name, context)
            setattr(agent, db_field, content)  # DB-Update
            (claude_config_dir / filename).write_text(content, encoding="utf-8")
            results[filename] = "written"
        except Exception as e:
            logger.error("sync_docker_agent_files(%s) %s: %s", agent.name, filename, e)
            results[filename] = f"error: {e}"

    # 2. USER.md: no DB field, always from template (operator persona)
    try:
        user_md = render_agent_file("USER.md.j2", context)
        (claude_config_dir / "USER.md").write_text(user_md, encoding="utf-8")
        results["USER.md"] = "written (template-only, no DB field)"
    except Exception as e:
        logger.error("sync_docker_agent_files(%s) USER.md: %s", agent.name, e)
        results["USER.md"] = f"error: {e}"

    # 3. MEMORY.md: agent-managed. Initially from template when DB is empty,
    #    otherwise respect agent updates (knowledge grows over time).
    if not agent.memory_md:
        try:
            content = render_agent_file("MEMORY.md.j2", context)
            agent.memory_md = content
        except Exception as e:
            logger.error("sync_docker_agent_files(%s) MEMORY.md render: %s", agent.name, e)
            results["MEMORY.md"] = f"error rendering: {e}"
    if agent.memory_md:
        try:
            (claude_config_dir / "MEMORY.md").write_text(agent.memory_md, encoding="utf-8")
            results.setdefault("MEMORY.md", "written (initial from template)" if not results.get("MEMORY.md") else results["MEMORY.md"])
            if "MEMORY.md" not in results:
                results["MEMORY.md"] = "respected (agent-managed)"
        except Exception as e:
            results["MEMORY.md"] = f"error writing: {e}"

    # 4. TOOLS.md: from agent.tools_md (filled by tools_md_builder with raw_token).
    #    Not overwritten here — only mirrored into the file.
    if agent.tools_md:
        try:
            (claude_config_dir / "TOOLS.md").write_text(agent.tools_md, encoding="utf-8")
            results["TOOLS.md"] = "written (from DB)"
        except Exception as e:
            logger.error("sync_docker_agent_files(%s) TOOLS.md: %s", agent.name, e)
            results["TOOLS.md"] = f"error: {e}"
    else:
        results["TOOLS.md"] = "skipped (empty in DB — run reset-token)"

    # 5. Runtime config (settings.json + .env) — rendered depending on runtime.
    #
    # Two paths:
    #   - anthropic-claude-* runtime: writes `model` into settings.json;
    #     no .env file (claude-code uses CLAUDE_CODE_OAUTH_TOKEN from the
    #     bootstrap response, no shim).
    #   - all other cli-bridge runtimes (ollama-cloud, qwen-coder-lms,
    #     vllm): OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_API_KEY in .env
    #     (openclaude reads them via start-claude.sh).
    #
    # settings.json is always updated (if already present — full re-render
    # via plugin_manager.sync_agent_plugins_to_disk, see below).
    runtime: Runtime | None = None
    if agent.runtime_id:
        runtime = await session.get(Runtime, agent.runtime_id)

    is_anthropic = runtime and runtime.enabled and runtime.slug.startswith("anthropic-claude-")

    # Sync settings.json — Bug 5 permanent fix (2026-05-13).
    #
    # Before: only the `model` field was merged into an existing settings.json.
    # Result: `systemPrompt` drifted away from `agent.soul_md` as soon as the DB
    # was populated, after the file had initially been created empty. Sparky +
    # FreeCode ran for weeks with systemPrompt="" -> no identity, no
    # MC tool awareness, scope creep.
    #
    # Now: when settings.json exists + runtime is active + soul_md is plausible,
    # we delegate the complete render to
    # plugin_manager.sync_agent_plugins_to_disk(). That's the single-source-
    # of-truth path (ADR-006): template `cli_agent_settings.json.j2` + DB state
    # -> file. Writes parent settings.json + claude-config mirror +
    # installed_plugins.json + known_marketplaces.json — all consistent.
    #
    # Self-check: if soul_md < 1000 characters, the DB row is probably
    # incomplete (freshly seeded, template fail). In that case do NOT
    # render -- otherwise a possibly already correctly filled settings.json
    # would get overwritten with an empty systemPrompt (exactly the bug mode).
    settings_path = claude_config_dir / "settings.json"
    soul_len = len(agent.soul_md or "")
    if not settings_path.exists():
        results["settings.json"] = "skipped (file does not exist — provision first)"
    elif not (runtime and runtime.enabled and runtime.model_identifier):
        results["settings.json"] = "unchanged (no runtime or no model_identifier)"
    elif soul_len < 1000:
        logger.warning(
            "sync_docker_agent_files(%s): soul_md too short (%d chars) — "
            "skipping settings.json render to avoid overwriting with empty "
            "systemPrompt. Check that the SOUL.md template rendered correctly.",
            agent.name,
            soul_len,
        )
        results["settings.json"] = (
            f"skipped (soul_md too short: {soul_len} chars)"
        )
    else:
        try:
            from app.services.plugin_manager import sync_agent_plugins_to_disk

            written = sync_agent_plugins_to_disk(
                slug,
                agent.soul_md,
                runtime.model_identifier,
                agent.cli_plugins,
            )
            if written.get("settings.json"):
                results["settings.json"] = (
                    f"written (full render from template, model={runtime.model_identifier})"
                )
            else:
                results["settings.json"] = (
                    "error: sync_agent_plugins_to_disk reported failure"
                )
        except Exception as e:
            logger.error(
                "sync_docker_agent_files(%s) settings.json: %s", agent.name, e
            )
            results["settings.json"] = f"error: {e}"

    # Phase 3 — Claude-Process Recycler kill-switch (MEM-01).
    # Two-tier resolution: per-agent agent.recycler_enabled wins, else global
    # settings.agent_recycler_enabled. ALWAYS rendered into agent.env — the
    # recycler runs in BOTH mc-agent-base and mc-claude-agent containers
    # (runtime-agnostic). Caveat 1 (Plan 03-04): the previous unlink-for-
    # anthropic flow is replaced by an unconditional minimum write, so the
    # recycler-line lands for ALL agents including the 9 claude-binary ones.
    # Lazy local import — keeps the dependency surface small (mirror of the
    # plugin_manager import below at L276) and matches the runtime_context
    # convention from Phase 1 (lazy imports inside service-call sites).
    from app.services.recycler_config import get_effective_recycler_enabled
    recycler_effective = get_effective_recycler_enabled(agent)

    env_path = claude_config_dir / ".env"
    env_lines: list[str] = []
    env_notes: list[str] = []

    # Recycler line goes first — independent of runtime. See ADR-024.
    env_lines.append(
        f"AGENT_RECYCLER_ENABLED={'true' if recycler_effective else 'false'}"
    )
    env_notes.append(f"recycler={'on' if recycler_effective else 'off'}")

    if is_anthropic:
        # Claude agent: no OPENAI shim, OAuth comes via bootstrap. Phase 3
        # (Plan 03-04): we write ONLY the recycler line instead of previously
        # unlinking .env — the recycler in Window 2 needs to know whether it
        # should run. Fallthrough — env_lines already has the recycler entry,
        # the write block below persists it.
        pass
    else:
        try:
            if runtime and runtime.enabled:
                if runtime.endpoint:
                    env_lines.append(f"OPENAI_BASE_URL={_sanitize_env_val(runtime.endpoint)}")
                    env_notes.append(f"runtime={runtime.slug}")
                if runtime.model_identifier:
                    env_lines.append(f"OPENAI_MODEL={_sanitize_env_val(runtime.model_identifier)}")
            elif runtime and not runtime.enabled:
                env_notes.append(f"runtime_disabled={runtime.slug}")
            elif agent.runtime_id:
                env_notes.append("runtime_missing")

            if agent.secret_id:
                plaintext = await get_secret_plaintext_by_id(session, agent.secret_id)
                if plaintext:
                    env_lines.append(f"OPENAI_API_KEY={_sanitize_env_val(plaintext)}")
                    env_notes.append("secret=set")
                else:
                    # Secret decryption error — we still write the
                    # recycler line + any runtime keys, but log the error
                    # explicitly as ".env_secret_error" so the render result
                    # is visible. Previously env_lines was set to None and
                    # the whole write was skipped — that would swallow the
                    # recycler line (Phase-3 regression).
                    results[".env_secret_error"] = "secret not found or decryption failed"
        except ValueError as e:
            # Newline injection detected in a runtime field — log and surface,
            # but continue: the recycler line is still written so Window 2 works.
            logger.error(
                "sync_docker_agent_files(%s) env sanitization error: %s", agent.name, e
            )
            results[".env"] = f"error sanitizing: {e}"
            # Skip the write entirely — do not persist potentially unsafe content.
            return results

    # Unconditional write — env_lines contains at least the recycler line.
    try:
        env_path.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        results[".env"] = f"written ({', '.join(env_notes) or 'runtime/secret'})"
    except Exception as e:
        logger.error("sync_docker_agent_files(%s) .env: %s", agent.name, e)
        results[".env"] = f"error writing: {e}"

    # 6. Copy custom skills from ~/.mc/skills/ into claude-config/skills/
    # (Before: skill files never landed in the container despite cli_skills in DB.
    #  Boss reflection 2026-04-24: Shakespeare had to reconstruct a skill via
    #  WebFetch instead of reading it from /home/agent/.claude/skills/.)
    try:
        from app.services.plugin_manager import sync_agent_skills_to_disk
        skill_sync = sync_agent_skills_to_disk(slug, agent.cli_skills)
        results["skills"] = f"synced ({sum(1 for v in skill_sync.values() if v is True)} ok)"
    except Exception as e:
        logger.error("Skills-Sync fuer %s fehlgeschlagen: %s", agent.name, e)
        results["skills"] = f"error: {e}"

    # 7. Persist DB updates (SOUL.md/HEARTBEAT.md/MEMORY.md may have been updated in DB)
    session.add(agent)
    await session.commit()

    logger.info("sync_docker_agent_files(%s) -> %s", agent.name, results)
    return results


async def sync_host_agent_files(
    session: AsyncSession,
    agent: Agent,
) -> dict[str, str]:
    """Host-runtime variant of :func:`sync_docker_agent_files`.

    Boss + Hermes run as ``agent_runtime: host`` — they aren't started via
    ``docker-compose.agents.yml`` and don't use the openclaude tmux launcher.
    Their MC-config files live at ``agent.workspace_path / claude-config/``
    on the host filesystem (Boss: ``~/.mc/workspaces/boss/claude-config/``;
    Hermes: ``~/.mc/agents/hermes/claude-config/``).

    Mirrors steps 1–4 of the docker variant (Template → DB → File for
    SOUL/HEARTBEAT/USER/MEMORY/TOOLS). Skips steps 5–6 (settings.json + .env
    + skills sync) because those wire up the openclaude shim — host agents
    run their own native CLI (Boss: claude binary, Hermes: MCP bridge) and
    manage their runtime config independently.

    Returns a per-file status dict in the same shape as the docker variant
    so callers can render identically.
    """
    if getattr(agent, "agent_runtime", None) != "host":
        msg = f"sync_host_agent_files called on non-host agent {agent.name} (runtime={agent.agent_runtime!r})"
        logger.warning(msg)
        return {"_error": msg}

    if not agent.workspace_path:
        msg = f"host agent {agent.name} has no workspace_path set"
        logger.error("sync_host_agent_files(%s): %s", agent.name, msg)
        return {"_error": msg}

    claude_config_dir = Path(agent.workspace_path) / "claude-config"
    try:
        claude_config_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        msg = f"cannot create claude-config dir {claude_config_dir}: {e}"
        logger.error("sync_host_agent_files(%s): %s", agent.name, msg)
        return {"_error": msg}

    # Team-Context: same as docker variant (board roster for Jinja2 team list)
    agents_on_board: list[Agent] = []
    if agent.board_id:
        result = await session.exec(
            select(Agent).where(Agent.board_id == agent.board_id)
        )
        agents_on_board = list(result.all())

    context = build_agent_context(
        agent,
        board_id=str(agent.board_id) if agent.board_id else None,
        agents_on_board=agents_on_board,
    )

    results: dict[str, str] = {}

    # 1. SOUL.md: Template → DB → File (always overwrite)
    #    HEARTBEAT.md removed in migration 0125 — never read by agents.
    for filename, template_name, db_field in [
        ("SOUL.md", "SOUL.md.j2", "soul_md"),
    ]:
        try:
            content = render_agent_file(template_name, context)
            setattr(agent, db_field, content)
            (claude_config_dir / filename).write_text(content, encoding="utf-8")
            results[filename] = "written"
        except Exception as e:
            logger.error("sync_host_agent_files(%s) %s: %s", agent.name, filename, e)
            results[filename] = f"error: {e}"

    # 2. USER.md: no DB field, always from template
    try:
        user_md = render_agent_file("USER.md.j2", context)
        (claude_config_dir / "USER.md").write_text(user_md, encoding="utf-8")
        results["USER.md"] = "written (template-only, no DB field)"
    except Exception as e:
        logger.error("sync_host_agent_files(%s) USER.md: %s", agent.name, e)
        results["USER.md"] = f"error: {e}"

    # 3. MEMORY.md: agent-managed. Initial from template if DB empty.
    if not agent.memory_md:
        try:
            content = render_agent_file("MEMORY.md.j2", context)
            agent.memory_md = content
        except Exception as e:
            logger.error("sync_host_agent_files(%s) MEMORY.md render: %s", agent.name, e)
            results["MEMORY.md"] = f"error rendering: {e}"
    if agent.memory_md:
        try:
            (claude_config_dir / "MEMORY.md").write_text(agent.memory_md, encoding="utf-8")
            if "MEMORY.md" not in results:
                results["MEMORY.md"] = "written (initial from template)" if not agent.memory_md else "respected (agent-managed)"
        except Exception as e:
            results["MEMORY.md"] = f"error writing: {e}"

    # 4. TOOLS.md: from agent.tools_md (filled by tools_md_builder with raw_token)
    if agent.tools_md:
        try:
            (claude_config_dir / "TOOLS.md").write_text(agent.tools_md, encoding="utf-8")
            results["TOOLS.md"] = "written (from DB)"
        except Exception as e:
            logger.error("sync_host_agent_files(%s) TOOLS.md: %s", agent.name, e)
            results["TOOLS.md"] = f"error: {e}"
    else:
        results["TOOLS.md"] = "skipped (empty in DB — run reset-token)"

    # 5. DB-Updates persist (SOUL.md/HEARTBEAT.md/MEMORY.md may have been updated)
    session.add(agent)
    await session.commit()

    logger.info("sync_host_agent_files(%s) -> %s", agent.name, results)
    return results


async def sync_agent_files(
    session: AsyncSession,
    agent: Agent,
) -> dict[str, str]:
    """Dispatcher that picks docker vs host sync based on agent.agent_runtime.

    Single entry point so callers don't sprinkle ``if agent_runtime == 'host'``
    branches across the codebase. Anything that ends up at
    :func:`sync_docker_agent_files` directly for a host agent currently
    returns ``{"_skipped": "host runtime"}`` — this dispatcher prevents that
    silent-skip path and routes to the right writer.
    """
    if getattr(agent, "agent_runtime", None) == "host":
        return await sync_host_agent_files(session, agent)
    return await sync_docker_agent_files(session, agent)


def _respawn_agent_window(agent: Agent) -> dict[str, str]:
    """Respawns tmux Window 0 in the running container without restarting the container.

    D-11: Same-image runtime switches should NOT reboot the whole container
    — that would kill poll.sh (Window 1) and the recycler (Window 2) along
    with it and cost 15-30s. Instead: respawn only Window 0.

    IMPORTANT: session_name = slug. entrypoint.sh sets
    `SESSION="${AGENT_NAME:-agent}"`. AGENT_NAME is set in
    docker-compose.agents.yml to the lowercase slug
    (e.g. `AGENT_NAME=sparky`), not to the original DB name.
    Live-verified 2026-04-29 (Phase 16 D-13): Pitfall 3 from RESEARCH.md
    was wrong — slug is correct.
    """
    import subprocess

    slug = _agent_slug(agent)
    container_name = f"mc-agent-{slug}"
    session_name = slug
    cmd = [
        "docker", "exec", container_name,
        "tmux", "respawn-window", "-k", "-t", f"{session_name}:0",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {
            "status": "error: tmux respawn-window timed out",
            "container": container_name,
            "mode": "respawn",
        }
    except FileNotFoundError:
        return {
            "status": "error: docker CLI not found in backend",
            "container": container_name,
            "mode": "respawn",
        }
    except Exception as e:
        logger.error("_respawn_agent_window(%s) failed: %s", container_name, e)
        return {"status": f"error: {e}", "container": container_name, "mode": "respawn"}

    if proc.returncode == 0:
        return {"status": "respawned", "container": container_name, "mode": "respawn"}
    err = proc.stderr.strip() or proc.stdout.strip()
    return {"status": f"error: {err}", "container": container_name, "mode": "respawn"}


async def _wait_for_window_ready(
    agent: Agent,
    *,
    timeout: int = 30,
    poll_interval: float = 3.0,
    ready_signals: tuple[str, ...] | None = None,
) -> dict[str, str | bool]:
    """Waits until tmux Window 0 shows a ready signal (D-12).

    Polls `tmux capture-pane -p -t {session_name}:0` and looks for
    ready signals: openclaude `╭─` header, claude `> ` prompt, or
    bash fallback `$ `.

    ADR-045: `ready_signals` REPLACES the default glyph tuple when provided
    (it is not additive). The omp headless runtime emits no interactive glyph;
    its readiness anchor is the `OMP_BRIDGE_READY` sentinel that
    `bridge.py --serve` prints once its poll loop is up. The default glyphs
    (`$ `, `> `) can appear in bridge.py log output and would false-positive,
    so omp must match the sentinel ONLY.
    """
    import asyncio
    import subprocess
    import time

    if getattr(agent, "agent_runtime", None) == "host":
        return {"healthy": True, "reason": "host runtime — assumed healthy"}

    slug = _agent_slug(agent)
    container_name = f"mc-agent-{slug}"
    session_name = slug  # entrypoint.sh: SESSION=$AGENT_NAME = slug
    deadline = time.time() + timeout
    sigs = ready_signals or ("╭─", "❯", "> ", "$ ")

    picker_dismissed = False
    while time.time() < deadline:
        try:
            proc = subprocess.run(
                [
                    "docker", "exec", container_name,
                    "tmux", "capture-pane", "-p", "-t", f"{session_name}:0",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pane = proc.stdout or ""
            # Note: openclaude prints `❯ ` (non-breaking space), so match
            # the bare `❯` glyph rather than `❯ ` (regular space).
            if any(sig in pane for sig in sigs):
                return {
                    "healthy": True,
                    "reason": f"tmux window ready ({container_name})",
                }
            # openclaude shows a model-picker on launch when the endpoint
            # exposes more than one model. Auto-confirm the default (Enter)
            # once, then keep polling for the real ready signal.
            if not picker_dismissed and "Enter to confirm" in pane:
                subprocess.run(
                    [
                        "docker", "exec", container_name,
                        "tmux", "send-keys", "-t", f"{session_name}:0", "Enter",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                picker_dismissed = True
        except FileNotFoundError:
            return {"healthy": False, "reason": "docker CLI not found"}
        except Exception as e:
            logger.debug("_wait_for_window_ready poll failed: %s", e)
        await asyncio.sleep(poll_interval)

    return {
        "healthy": False,
        "reason": f"timeout after {timeout}s — window not ready",
    }


def restart_docker_agent_container(
    agent: Agent,
    *,
    force_recreate: bool = False,
    respawn_window_only: bool = False,
) -> dict[str, str]:
    """Restarts the agent's Docker container.

    respawn_window_only=True (Phase 16, D-11):
        Calls `_respawn_agent_window(agent)` — restarts only tmux Window 0.
        poll.sh (Window 1) and the recycler (Window 2) are left untouched.
        Wins over force_recreate if both are set.

    force_recreate=False (default):
        `docker restart -t 5 mc-agent-<slug>` — picks up env/config but keeps
        the existing image. Used after a same-image runtime change.

    force_recreate=True (Phase 15):
        `docker compose -f docker-compose.yml -f docker/docker-compose.agents.yml up -d --force-recreate <service>`
        Caller is responsible for running compose_renderer.write_compose_agents()
        BEFORE calling this so the new image override is on disk. 90s timeout.

    Returns:
        dict: {"status": "restarted" | "recreated" | "respawned" | "skipped" | "error: ...",
               "container": name, "mode": "restart"|"recreate"|"respawn"}
    """
    import subprocess

    # Host agents (e.g. Boss) have no Docker container — skip restart.
    if getattr(agent, "agent_runtime", None) == "host":
        logger.debug(
            "Skipping host agent restart %s (runtime=%s)",
            agent.name,
            agent.agent_runtime,
        )
        return {"status": "skipped (host runtime)", "container": "", "mode": "skip"}

    if respawn_window_only:
        return _respawn_agent_window(agent)

    slug = _agent_slug(agent)
    container_name = f"mc-agent-{slug}"

    if force_recreate:
        # Resolve repo root on the host (mounted into backend container at the
        # same absolute path via docker-compose). settings.mc_repo_path comes
        # from MC_REPO_PATH — the checkout may have any folder name.
        repo_root = Path(settings.mc_repo_path)
        compose_main = repo_root / "docker-compose.yml"
        compose_agents = repo_root / "docker" / "docker-compose.agents.yml"
        env_main = repo_root / ".env"
        env_agents = repo_root / "docker" / ".env.agents"
        env_shared = repo_root / "docker" / ".env.shared"
        # Compose v2 supports multiple `--env-file` flags. The agents compose
        # file references ${MC_TOKEN_*}, ${OPENAI_API_KEY_*} etc. that live in
        # docker/.env.agents — without it those expand to empty and agents come
        # up with no auth token (mc CLI then dies with 'MC_AGENT_TOKEN missing').
        cmd = ["docker", "compose"]
        for env_file in (env_main, env_agents, env_shared):
            if env_file.is_file():
                cmd.extend(["--env-file", str(env_file)])
        cmd.extend([
            "-f",
            str(compose_main),
            "-f",
            str(compose_agents),
            "up",
            "-d",
            "--force-recreate",
            container_name,
        ])
        # docker compose substitutes ${HOME} from the caller's env. In the
        # backend container, $HOME=/home/mcuser → the docker daemon gets
        # the wrong path. HOME_HOST mounts the host home at the same
        # path — we have to force HOME to it explicitly.
        run_env = dict(os.environ)
        host_home = os.environ.get("HOME_HOST")
        if host_home:
            run_env["HOME"] = host_home
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=90,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error: docker compose up timed out (90s)",
                "container": container_name,
                "mode": "recreate",
            }
        except FileNotFoundError:
            return {
                "status": "error: docker CLI not found in backend",
                "container": container_name,
                "mode": "recreate",
            }
        except Exception as e:
            logger.error("force_recreate(%s) failed: %s", container_name, e)
            return {
                "status": f"error: {e}",
                "container": container_name,
                "mode": "recreate",
            }
        if proc.returncode == 0:
            return {"status": "recreated", "container": container_name, "mode": "recreate"}
        err = proc.stderr.strip() or proc.stdout.strip()
        return {
            "status": f"error: {err}",
            "container": container_name,
            "mode": "recreate",
        }

    try:
        # docker restart -t 5 container_name
        proc = subprocess.run(
            ["docker", "restart", "-t", "5", container_name],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "error: docker restart timed out",
            "container": container_name,
            "mode": "restart",
        }
    except FileNotFoundError:
        return {
            "status": "error: docker CLI not found in backend",
            "container": container_name,
            "mode": "restart",
        }
    except Exception as e:
        logger.error("restart_docker_agent_container(%s) failed: %s", container_name, e)
        return {"status": f"error: {e}", "container": container_name, "mode": "restart"}

    if proc.returncode == 0:
        return {"status": "restarted", "container": container_name, "mode": "restart"}
    err = proc.stderr.strip() or proc.stdout.strip()
    if "No such container" in err:
        return {"status": "skipped (no container)", "container": container_name, "mode": "restart"}
    return {"status": f"error: {err}", "container": container_name, "mode": "restart"}


def _agent_container_running(container_name: str) -> bool | None:
    """True/False = container run state, None = container doesn't exist / inspect error."""
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        logger.warning("_agent_container_running(%s): inspect failed: %s", container_name, e)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "true"


def ensure_agent_container_started(agent: Agent) -> dict[str, str]:
    """Provision autostart: starts the agent container if it isn't running.

    If the container is already running, it is deliberately NOT recreated — a
    re-provision must not kill an agent mid-task. If the container is missing or
    stopped, force_recreate=True brings it up from the freshly rendered
    compose (caller must have run write_compose_agents() beforehand,
    same precondition as restart_docker_agent_container).
    """
    if getattr(agent, "agent_runtime", None) == "host":
        return {"status": "skipped (host runtime)", "container": "", "mode": "skip"}

    container_name = f"mc-agent-{_agent_slug(agent)}"
    if _agent_container_running(container_name):
        return {"status": "already-running", "container": container_name, "mode": "none"}
    return restart_docker_agent_container(agent, force_recreate=True)


async def wait_for_agent_healthy(
    agent: Agent,
    *,
    timeout: int = 30,
    poll_interval: float = 6.0,
    respawn_mode: bool = False,
    ready_signals: tuple[str, ...] | None = None,
) -> dict[str, str | bool]:
    """Wait until the agent reports a heartbeat after restart/recreate.

    Polls Redis-cached `last_seen_at` via the agent row in DB OR a simple
    docker-ps liveness check. We can't make the agent push a fresh heartbeat
    on demand, so the heuristic is:

      - Container state == "running" (via `docker inspect`)
      - At least one log line written within the last `timeout` seconds OR
        agent.last_seen_at advanced.

    For the implementation here we keep it minimal: poll docker container
    state and report `running` once the container is up. Higher-level code
    can layer additional readiness checks (e.g. agent_poll endpoint).
    """
    import asyncio
    import subprocess
    import time

    if getattr(agent, "agent_runtime", None) == "host":
        return {"healthy": True, "reason": "host runtime — assumed healthy"}

    # ADR-045: route to the pane-scrape readiness check when respawn_mode is set
    # OR when an explicit `ready_signals` anchor is provided. The omp cross-image
    # switch runs with respawn_mode=False (image_change=True), but MUST still
    # scrape the pane for the OMP_BRIDGE_READY sentinel — a bare
    # `docker inspect ...==running` reports healthy before bridge.py bootstraps
    # (and a crash-looping Window 0 keeps the container "running" under the tmux
    # PID-1 watchdog), which would falsely pass a dead runtime with no rollback.
    if respawn_mode or ready_signals is not None:
        return await _wait_for_window_ready(
            agent,
            timeout=timeout,
            poll_interval=poll_interval,
            ready_signals=ready_signals,
        )

    slug = _agent_slug(agent)
    container_name = f"mc-agent-{slug}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
                capture_output=True,
                text=True,
                timeout=5,
            )
            status = (proc.stdout or "").strip().strip("'\"")
            if status == "running":
                return {"healthy": True, "reason": f"container running ({container_name})"}
        except FileNotFoundError:
            return {"healthy": False, "reason": "docker CLI not found"}
        except Exception as e:
            logger.debug("wait_for_agent_healthy poll failed: %s", e)
        await asyncio.sleep(poll_interval)

    return {"healthy": False, "reason": f"timeout after {timeout}s — container not running"}
