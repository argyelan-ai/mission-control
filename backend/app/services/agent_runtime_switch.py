"""Atomic agent ↔ runtime switch service (Phase 15 Wave 2).

Wraps the moving parts that PATCH /agents/{id} previously did inline (DB
update, file render, container restart) into a single transaction-shaped
flow with full rollback on failure, optional in-progress override, and an
explicit Redis lock so two concurrent switches on the same agent can't
race each other.

Flow on success:
  1. Validate runtime exists, enabled, agent-type allows switching, compatibility.
  2. Check `current_task_id` busy state (raise unless force_when_in_progress).
  3. Snapshot old state (runtime_id + image).
  4. Acquire Redis lock `mc:agent:{id}:runtime-switch` (TTL 120s).
  5. If image-switch is required: render the new docker-compose.agents.yml
     overlay BEFORE we touch the container.
  6. Update `agent.runtime_id` in the DB (commit).
  7. Re-render claude-config files (sync_docker_agent_files).
  8. Restart the container (force_recreate=image_change).
  9. Wait for the container to be reachable.
 10. On any failure between (5) and (9): full rollback (DB + files + image
     overlay + container) and raise SwitchHealthCheckFailed.
 11. Publish `mc:agent:{id}:terminal:remount` so the Sessions WebSocket re-mounts.
 12. Emit `agent.runtime_switched` activity event.
 13. Release the lock.

Dry-run short-circuits after compatibility validation and returns the
preview payload without mutating anything (used by the UI to surface
warnings + image-switch flag before the user confirms).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.redis_client import RedisKeys, get_redis
from app.services.activity import emit_event
from app.services.discord import send_discord_notification
from app.services.compose_renderer import (
    detect_image_change,
    write_compose_agents,
)
from app.services.docker_agent_sync import (
    restart_docker_agent_container,
    sync_docker_agent_files,
    wait_for_agent_healthy,
)
from app.utils import utcnow

logger = logging.getLogger("mc.agent_runtime_switch")

LOCK_TTL_SECONDS = 120
HEALTH_TIMEOUT_RECREATE = 90
HEALTH_TIMEOUT_RESTART = 30

# OpenAI-compatible runtime types where a `/models` probe is meaningful.
# Cloud (Anthropic, Ollama) already ship a model_identifier from the seed.
_PROBEABLE_RUNTIME_TYPES = {
    "vllm_docker",
    "llamacpp_docker",
    # ssh_process serves the same OpenAI /v1 as the docker engines — only the
    # thing behind the port is a host process instead of a container.
    "ssh_process",
    "lmstudio",
    "openai_compatible",
    "unsloth",
    "unsloth_porsche",
    # omp is a CLI harness, but its runtime row points at an OpenAI-compatible
    # endpoint (the same vLLM the TUI talks to). Without this entry the runtime
    # watcher never probes omp rows, so engine-side model drift never reaches
    # omp-bound agents.
    "omp",
}


# Model ids that can never be the chat model of a chat runtime. LM Studio
# routinely serves an embedding model alongside the chat one (vector search),
# and `/models` order is not specified — so taking data[0] blindly let an
# embedding model become a runtime's model_identifier. Observed 2026-07-25:
# both `nemotron-super` and `qwen-coder-lms` sat on
# `text-embedding-nomic-embed-text-v1.5`, which cannot answer a completion.
_NON_CHAT_MODEL_MARKERS = (
    "embed",        # text-embedding-*, nomic-embed-*, *-embedding-*
    "rerank",
    "whisper",
    "tts-",
    "-tts",
    "stable-diffusion",
    "clip-",
)


def _is_chat_capable(model_id: str) -> bool:
    """Heuristic: could this id plausibly be a chat/completion model?

    Deliberately a denylist of unmistakable non-chat families rather than an
    allowlist — an allowlist would reject every new model name the moment a
    provider ships one, which is the exact failure this whole change exists to
    prevent.
    """
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CHAT_MODEL_MARKERS)


def select_probed_model(model_ids: list[str], current: str | None) -> str | None:
    """Pick the model a probe should report, given everything the endpoint serves.

    Three rules, in order:

    1. Drop ids that cannot be chat models at all.
    2. If the runtime's CURRENT model is still being served, keep it. A probe
       exists to CONFIRM a binding, not to re-pick one — without this, a restart
       that happens to reorder `/models` would silently repoint every agent on
       that runtime at a different model.
    3. Otherwise take the first remaining candidate.

    Returns None when nothing plausible remains. That is deliberate: writing
    "no idea" is worse than writing nothing, because the runtime watcher would
    persist the wrong value as confirmed drift and flag agents for a restart.
    """
    candidates = [m.strip() for m in model_ids if isinstance(m, str) and m.strip()]
    chat_candidates = [m for m in candidates if _is_chat_capable(m)]
    if not chat_candidates:
        return None
    if current and current.strip() in chat_candidates:
        return current.strip()
    return chat_candidates[0]


class ProbedModel(NamedTuple):
    """What ``/v1/models`` says the engine is serving right now.

    ``context_len`` is the served model's ``max_model_len`` when the engine
    reports one (vLLM does, on every entry). ``None`` means "the endpoint did
    not say" — never "no context window": callers must leave the stored value
    alone rather than write a guess.
    """

    model_id: str | None
    context_len: int | None


def _served_context_len(entry: object) -> int | None:
    """Read the served context window off one ``/v1/models`` entry.

    vLLM reports ``max_model_len``; LM Studio and several OpenAI-compatible
    shims use ``context_length`` / ``max_context_length`` for the same number.
    Anything non-positive or non-integral is treated as "not reported" — a
    zero or a string here is a shim quirk, not a real 0-token window.
    """
    if not isinstance(entry, dict):
        return None
    for key in ("max_model_len", "context_length", "max_context_length"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value > 0:
            return value
    return None


async def probe_runtime_model(runtime: Runtime) -> str | None:
    """Best-effort probe of an OpenAI-compatible `/models` endpoint.

    Returns the model id the endpoint is serving for this runtime, or None on
    failure or when nothing chat-capable is on offer. Selection rules live in
    `select_probed_model`. Caller is responsible for persisting the value.

    Thin wrapper over :func:`probe_runtime_model_info` — every caller that only
    cares about identity keeps its old signature.
    """
    return (await probe_runtime_model_info(runtime)).model_id


async def probe_runtime_model_info(runtime: Runtime) -> ProbedModel:
    """:func:`probe_runtime_model` plus the served model's context window.

    The context window comes from the SAME ``/v1/models`` response and the SAME
    entry as the picked model id — probing them separately could pair a model
    with another model's window across a switch. ``ProbedModel(None, None)`` on
    any failure.
    """
    if not runtime.endpoint:
        return ProbedModel(None, None)
    base = runtime.endpoint.rstrip("/")
    # Normalise: vLLM/LM Studio typically have `/v1` baked into the endpoint;
    # bare base URLs are also valid. Both `/v1/models` and `/models` paths are
    # tried so one config style covers all current rows.
    candidates = []
    if base.endswith("/v1"):
        candidates.append(f"{base}/models")
    else:
        candidates.append(f"{base}/v1/models")
        candidates.append(f"{base}/models")
    try:
        import httpx  # local import — already a project dep
    except ImportError:
        logger.warning("probe_runtime_model: httpx unavailable")
        return ProbedModel(None, None)
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in candidates:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else None
                if isinstance(items, list) and items:
                    entries = [it for it in items if isinstance(it, dict)]
                    ids = [
                        it.get("id")
                        for it in entries
                        if isinstance(it.get("id"), str)
                    ]
                    picked = select_probed_model(ids, runtime.model_identifier)
                    if picked:
                        entry = next(
                            (it for it in entries if it.get("id") == picked), None
                        )
                        return ProbedModel(picked, _served_context_len(entry))
                    # Endpoint answered, but served nothing chat-capable. Do not
                    # fall through to the next candidate URL with a different
                    # shape — report "unknown" so the caller leaves the binding
                    # alone instead of confirming a wrong value.
                    logger.info(
                        "probe_runtime_model %s: no chat-capable model among %s "
                        "— leaving model_identifier untouched",
                        runtime.slug,
                        ids,
                    )
                    return ProbedModel(None, None)
            except Exception as e:
                logger.debug("probe_runtime_model %s failed: %s", url, e)
                continue
    return ProbedModel(None, None)


async def ensure_runtime_model_identifier(
    session: AsyncSession, runtime: Runtime
) -> Runtime:
    """If `runtime.model_identifier` is NULL and the type is OpenAI-compatible,
    probe `/models` and persist the result. Returns the (possibly updated) row.
    """
    if runtime.model_identifier:
        return runtime
    if runtime.runtime_type not in _PROBEABLE_RUNTIME_TYPES:
        return runtime
    if not runtime.enabled:
        return runtime
    probed = await probe_runtime_model(runtime)
    if probed:
        runtime.model_identifier = probed
        runtime.updated_at = utcnow() if hasattr(runtime, "updated_at") else None
        session.add(runtime)
        await session.commit()
        await session.refresh(runtime)
        logger.info(
            "ensure_runtime_model_identifier: %s → %s", runtime.slug, probed
        )
    return runtime


# ── Custom exceptions ─────────────────────────────────────────────────────


class RuntimeSwitchError(Exception):
    """Base class so callers can grep one type if they want."""


class RuntimeNotFoundError(RuntimeSwitchError):
    """Target runtime row does not exist."""


class RuntimeIncompatibleError(RuntimeSwitchError):
    """Hard-block: runtime is disabled or otherwise unfit for this agent."""


class AgentNotSwitchableError(RuntimeSwitchError):
    """Host / openclaw agents can't have their runtime switched via MC."""


class AgentBusyError(RuntimeSwitchError):
    """Agent has a `current_task_id` and caller did not force."""

    def __init__(self, message: str, *, current_task_id: uuid.UUID | None = None):
        super().__init__(message)
        self.current_task_id = current_task_id


class SwitchHealthCheckFailed(RuntimeSwitchError):
    """Post-restart health check timed out — rollback was applied."""


class RuntimeSwitchLockTimeout(RuntimeSwitchError):
    """Concurrent switch in flight; we did not acquire the lock."""


# ── Result shape ──────────────────────────────────────────────────────────


@dataclass
class SwitchResult:
    old_runtime: dict[str, Any] | None
    new_runtime: dict[str, Any]
    image_switched: bool
    duration_ms: int
    warnings: list[str]
    dry_run: bool = False
    health: dict[str, Any] = field(default_factory=dict)
    # ADR-056: the second switch axis. `harness` is the effective target
    # harness (new_harness or agent.harness or derive_harness(new_runtime)),
    # `old_harness` the pre-switch effective harness — both surfaced so the UI
    # can render the harness change alongside the runtime change.
    harness: str | None = None
    old_harness: str | None = None
    # Task #26 — the switch now auto-triggers the agent restart/recreate that
    # used to require a manual click. `restart_skipped` is True exactly when
    # that restart was deliberately NOT run even though the switch itself
    # succeeded (busy agent or `restart_after_switch=False`) — the DB/config
    # side of the switch is still committed either way. The UI surfaces this
    # so "switch succeeded" is never mistaken for "agent is already running
    # the new model".
    restart_skipped: bool = False
    restart_skip_reason: str | None = None
    # Distinct from restart_skipped: the restart was ATTEMPTED (not
    # deliberately withheld) and the attempt itself errored. Per Task #26
    # design, a restart failure is a "the agent still needs a manual bump"
    # problem, not a "the switch was bad" problem — the switch stays
    # committed and this is reported instead of rolling everything back.
    restart_failed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_runtime": self.old_runtime,
            "new_runtime": self.new_runtime,
            "image_switched": self.image_switched,
            "duration_ms": self.duration_ms,
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
            "health": self.health or None,
            "harness": self.harness,
            "old_harness": self.old_harness,
            "restart_skipped": self.restart_skipped,
            "restart_skip_reason": self.restart_skip_reason,
            "restart_failed": self.restart_failed,
        }


def _runtime_summary(rt: Runtime | None) -> dict[str, Any] | None:
    if rt is None:
        return None
    return {
        "id": str(rt.id),
        "slug": rt.slug,
        "display_name": rt.display_name,
        "runtime_type": rt.runtime_type,
        "model_identifier": rt.model_identifier,
        "single_instance": getattr(rt, "single_instance", False),
    }


# ── Public helpers (also used by validators / UI dry-run) ─────────────────


def is_agent_busy(agent: Agent) -> bool:
    """Truthy when the agent has an active task assignment."""
    return getattr(agent, "current_task_id", None) is not None


def _restart_skip_reason(agent: Agent, *, restart_after_switch: bool) -> str | None:
    """None when the post-switch restart should run; else the user-facing
    reason it was skipped. Busy takes precedence over the flag in the message
    (it is the more actionable fact), but either alone is sufficient to skip.
    """
    if is_agent_busy(agent):
        return (
            f"Task {agent.current_task_id} läuft noch — Neustart übersprungen. "
            f"Modell wechselt erst beim nächsten manuellen oder automatischen Neustart."
        )
    if not restart_after_switch:
        return "restart_after_switch=false — Neustart bewusst übersprungen."
    return None


# Plugins that imply tool-use → if the runtime can't do tool-calls, warn.
# Conservative list; expand as patterns emerge. See PLAN.md T2.3.
_TOOL_USING_PLUGIN_HINTS = ("coding-agent", "github", "search", "bash", "tools")


def _agent_uses_tools(agent: Agent) -> bool:
    """Heuristic: does this agent need tool-calling support?

    cli_plugins == None  → all plugins enabled (default) → tool-rich, return True
    cli_plugins == []    → explicit empty allowlist → no plugins → return False
    cli_plugins == [..]  → True iff any name contains a tool-hint keyword.
    """
    raw = getattr(agent, "cli_plugins", None)
    if raw is None:
        return True
    if not raw:
        return False
    return any(hint in p.lower() for p in raw for hint in _TOOL_USING_PLUGIN_HINTS)


async def validate_compatibility(
    session: AsyncSession,
    agent: Agent,
    runtime: Runtime,
) -> list[str]:
    """Return list of soft-warnings. Hard incompatibilities raise.

    Hard rules (raise RuntimeIncompatibleError):
      - runtime.enabled is False
    Soft rules (warn-only, returned for UI display):
      - agent uses tools, runtime.supports_tools is False
      - runtime is a docker engine (vllm_docker / llamacpp_docker) and not
        ready (state read via runtime_state, best effort)
    """
    if not runtime.enabled:
        raise RuntimeIncompatibleError(
            f"Runtime '{runtime.slug}' ist disabled — zuerst aktivieren."
        )

    warnings: list[str] = []

    if _agent_uses_tools(agent) and not runtime.supports_tools:
        warnings.append(
            f"Agent nutzt Tools — Runtime '{runtime.slug}' unterstuetzt aber kein "
            f"tool-calling. Tool-using prompts werden vermutlich fehlschlagen."
        )

    if runtime.runtime_type in ("vllm_docker", "llamacpp_docker"):
        try:
            from app.services.runtime_state import get_runtime_state_dict  # type: ignore[import-not-found]
            state = await get_runtime_state_dict(runtime)
            if isinstance(state, dict) and state.get("state") not in (None, "ready", "running"):
                engine = "llama.cpp" if runtime.runtime_type == "llamacpp_docker" else "vLLM"
                warnings.append(
                    f"{engine}-Container '{runtime.slug}' ist aktuell "
                    f"'{state.get('state')}' — Health-Check kann fehlschlagen."
                )
        except ImportError:
            # No runtime_state helper available — fail open.
            pass
        except Exception as e:  # pragma: no cover — defensive
            logger.debug("validate_compatibility runtime-state probe failed: %s", e)

    return warnings


def _is_host_inplace(agent: Agent) -> bool:
    """Thin alias of ``host_harness_adapter.is_host_inplace`` (see that module).

    Kept as a module-local name because the rest of this service (and its
    tests) reference it; the RULE itself lives in host_harness_adapter so the
    API's derived `runtime_switchable` field and this guard can never diverge.
    """
    from app.services.host_harness_adapter import is_host_inplace

    return is_host_inplace(agent)


def _ensure_agent_switchable(agent: Agent) -> None:
    """Raise unless ``agent`` is eligible for an MC-driven runtime switch.

    The eligibility rule AND its plain-text reason both come from
    host_harness_adapter.runtime_switch_availability — the same function that
    feeds Agent.runtime_switchable / .runtime_switch_blocked_reason into the
    API, so the UI can never disagree with this endpoint about who may switch.
    """
    from app.services.host_harness_adapter import runtime_switch_availability

    switchable, reason = runtime_switch_availability(agent)
    if switchable:
        return
    raise AgentNotSwitchableError(reason or "Runtime switch is not supported for this agent.")


# ── Lock helpers ──────────────────────────────────────────────────────────


def _lock_key(agent_id: uuid.UUID) -> str:
    return f"mc:agent:{agent_id}:runtime-switch"


async def _acquire_lock(agent_id: uuid.UUID) -> bool:
    redis = await get_redis()
    return bool(await redis.set(_lock_key(agent_id), "1", nx=True, ex=LOCK_TTL_SECONDS))


async def _release_lock(agent_id: uuid.UUID) -> None:
    try:
        redis = await get_redis()
        await redis.delete(_lock_key(agent_id))
    except Exception as e:  # pragma: no cover
        logger.warning("release_lock failed for %s: %s", agent_id, e)


async def publish_switch_progress(
    agent_id: uuid.UUID, step: str, *, error: str | None = None
) -> None:
    """Best-effort progress breadcrumbs for the switch modal (TTL 5 min).

    Steps: rendering → restarting → waiting_healthy → done | rolled_back.
    Redis failures are swallowed — progress is cosmetic, never load-bearing.
    """
    try:
        redis = await get_redis()
        payload = json.dumps({"step": step, "error": error, "ts": time.time()})
        await redis.setex(
            RedisKeys.agent_switch_progress(str(agent_id)), 300, payload
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("switch progress publish failed: %s", exc)


def terminal_remount_channel(agent_id: uuid.UUID) -> str:
    """Per-agent Redis pub/sub channel name for terminal remount signals."""
    return f"mc:agent:{agent_id}:terminal:remount"


async def _publish_terminal_remount(agent_id: uuid.UUID, *, image_changed: bool) -> None:
    """Tell the Sessions page that the underlying tmux container changed.

    Routed through `services.sse.broadcast` so the SSE generator can decode
    the payload (it expects `{id, event, data}` shape) without changes.
    """
    try:
        from app.services.sse import broadcast  # local import — avoids cycle
        await broadcast(
            terminal_remount_channel(agent_id),
            "terminal_remount",
            {
                "reason": "runtime_switched",
                "image_changed": image_changed,
                "ts": int(time.time()),
            },
        )
    except Exception as e:  # pragma: no cover
        logger.warning("terminal:remount publish failed for %s: %s", agent_id, e)


# ── Main entrypoint ───────────────────────────────────────────────────────


async def switch_agent_runtime(
    session: AsyncSession,
    agent: Agent,
    new_runtime_id: uuid.UUID,
    *,
    force_when_in_progress: bool = False,
    new_harness: str | None = None,
    dry_run: bool = False,
    restart_after_switch: bool = True,
) -> SwitchResult:
    """Atomically switch ``agent`` to ``new_runtime_id``.

    Task #26: the restart/recreate that makes the switch actually take effect
    (container respawn/recreate, or the host process restart) now happens
    automatically as part of this call — callers no longer have to find a
    separate restart button. Two things can still suppress it, in which case
    the DB/config side of the switch is committed exactly as before but the
    restart step is skipped and the reason is surfaced on the result /
    activity event instead of being silently lost:

      * ``restart_after_switch=False`` — explicit opt-out.
      * the agent is busy (``current_task_id`` set). This only matters when
        ``force_when_in_progress=True`` let the switch past the busy guard
        below — restarting a busy agent's process/container would kill its
        running task, so the safer default is to persist the new binding and
        let a human (or a future automatic retry) trigger the restart once
        the task is done.

    Raises:
        AgentNotSwitchableError: agent is host/openclaw.
        RuntimeNotFoundError: new_runtime_id does not exist.
        RuntimeIncompatibleError: target runtime is disabled.
        AgentBusyError: agent has current_task_id and force is False.
        RuntimeSwitchLockTimeout: another switch is currently running.
        SwitchHealthCheckFailed: post-restart health check timed out (rollback applied).
    """
    started_at = time.monotonic()
    _ensure_agent_switchable(agent)

    new_runtime = await session.get(Runtime, new_runtime_id)
    if new_runtime is None:
        raise RuntimeNotFoundError(f"Runtime {new_runtime_id} not found.")

    # ADR-064: a host agent with an adapter switches in place. The reload is
    # strictly sequential (kill → re-render agent.env → restart the single
    # session), so it never creates a parallel instance — the single_instance
    # hard-block below must NOT fire for this path (it still blocks binding a
    # second / adapter-less agent onto a single_instance runtime).
    is_host_inplace = _is_host_inplace(agent)

    # HERM-04 / D-08 / D-09: single_instance hard-block (Phase 24 plan 03).
    # Some runtimes (e.g. Hermes) own their own session lifecycle outside
    # MC's compose-managed agent fleet — switching INTO or OUT OF such a
    # runtime would leave MC and the underlying process in inconsistent
    # state. Generic mechanism: any runtime row flagged single_instance is
    # opaque to the switch service. ``getattr`` keeps this resilient until
    # plan 24-01's migration lands (column defaults to False either way).
    if not is_host_inplace and getattr(new_runtime, "single_instance", False):
        raise AgentNotSwitchableError(
            f"Runtime '{new_runtime.slug}' ist als single_instance markiert "
            f"und kann nicht via Switch gewechselt werden."
        )

    old_runtime: Runtime | None = None
    if agent.runtime_id is not None:
        old_runtime = await session.get(Runtime, agent.runtime_id)
        if (
            not is_host_inplace
            and old_runtime is not None
            and getattr(old_runtime, "single_instance", False)
        ):
            raise AgentNotSwitchableError(
                f"Agent ist an single_instance Runtime '{old_runtime.slug}' "
                f"gebunden — Switch nicht erlaubt."
            )

    # Auto-fill model_identifier for OpenAI-compatible runtimes that landed in
    # the registry without one (vLLM rows seeded with NULL). Without this the
    # bootstrap omits OPENAI_MODEL and the container falls through to the
    # IMAGE-baked default (glm-5.1:cloud), which silently mismatches the
    # endpoint the agent is actually pointed at.
    new_runtime = await ensure_runtime_model_identifier(session, new_runtime)

    warnings = await validate_compatibility(session, agent, new_runtime)

    # ADR-056: harness/provider decoupling — the second switch axis. The
    # effective harness resolves through the explicit request, then the agent's
    # current harness, then a runtime-derived legacy fallback (so unmigrated
    # NULL rows behave exactly as before). Matrix validation only fires when we
    # actually have an effective harness — legacy NULL keeps the old behaviour.
    from app.services.harness_compat import (
        derive_harness,
        incompat_reason,
        is_compatible,
    )

    effective_old_harness = agent.harness or derive_harness(old_runtime)
    effective_new_harness = new_harness or agent.harness or derive_harness(new_runtime)
    if effective_new_harness is not None and not is_compatible(
        effective_new_harness, new_runtime
    ):
        raise RuntimeIncompatibleError(
            incompat_reason(effective_new_harness, new_runtime)
            or f"Harness '{effective_new_harness}' ist mit Runtime "
            f"'{new_runtime.slug}' nicht kompatibel."
        )

    if is_agent_busy(agent) and not force_when_in_progress:
        raise AgentBusyError(
            f"Agent {agent.name} hat eine aktive Task ({agent.current_task_id}). "
            f"Force-Toggle aktivieren um trotzdem zu switchen.",
            current_task_id=agent.current_task_id,
        )

    image_change = detect_image_change(
        old_runtime,
        new_runtime,
        old_harness=effective_old_harness,
        new_harness=effective_new_harness,
    )

    if dry_run:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        return SwitchResult(
            old_runtime=_runtime_summary(old_runtime),
            new_runtime=_runtime_summary(new_runtime) or {},
            image_switched=image_change,
            duration_ms=elapsed_ms,
            warnings=warnings,
            dry_run=True,
            health={},
            harness=effective_new_harness,
            old_harness=effective_old_harness,
        )

    acquired = await _acquire_lock(agent.id)
    if not acquired:
        raise RuntimeSwitchLockTimeout(
            f"Switch fuer Agent {agent.name} laeuft bereits. "
            f"Bitte warten oder Lock manuell loeschen."
        )

    snapshot_old_runtime_id = agent.runtime_id
    snapshot_old_harness = agent.harness
    await publish_switch_progress(agent.id, "rendering")

    try:
        # ── Host in-place switch (ADR-064) ──────────────────────────────────
        # A host agent with an adapter never touches the docker/compose path.
        # The adapter owns the reload: rewrite OPENAI_* in agent.env (token
        # preserved) then restart the single host session. Sequential, so no
        # parallel instance is created. Rollback restores the prior binding.
        if is_host_inplace:
            from app.services.host_harness_adapter import (
                get_adapter,
                sync_host_agent_model,
            )

            adapter = get_adapter(agent.harness)
            prev_runtime_id = agent.runtime_id
            prev_model = agent.model
            agent.runtime_id = new_runtime.id
            if new_runtime.model_identifier:
                agent.model = new_runtime.model_identifier
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            await session.refresh(agent)
            # Task #26 — decide up front whether the restart step runs. A busy
            # agent only reaches this point because force_when_in_progress
            # let it past the guard above; restarting its process now would
            # kill the running task, so the binding is saved but the restart
            # is deferred and clearly flagged instead.
            skip_reason = _restart_skip_reason(agent, restart_after_switch=restart_after_switch)

            # Config render (agent.env) failing means the switch itself did
            # NOT take effect — roll back exactly as before #26.
            try:
                await sync_host_agent_model(agent, new_runtime, session=session)
            except Exception as e:
                agent.runtime_id = prev_runtime_id
                agent.model = prev_model
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                await _emit_failure_event(
                    session, agent, old_runtime, new_runtime,
                    reason=f"host config render failed: {e}",
                    elapsed_ms=int((time.monotonic() - started_at) * 1000),
                )
                await publish_switch_progress(
                    agent.id, "rolled_back", error=f"host config render failed: {e}"
                )
                raise

            # Restart failing is different: agent.env already has the new
            # binding rendered — the switch is real, only the process bounce
            # itself needs a retry. Report it instead of undoing the switch
            # (Task #26, item e).
            restart_failed = False
            restart_error: str | None = None
            if skip_reason is None:
                try:
                    await adapter.reload(agent)
                except Exception as e:
                    restart_failed = True
                    restart_error = str(e)
                    logger.warning(
                        "host restart after runtime switch failed for %s: %s",
                        agent.name, e,
                    )

            if skip_reason is None and not restart_failed:
                await _publish_terminal_remount(agent.id, image_changed=False)
            if skip_reason:
                progress_step = "restart_skipped"
            elif restart_failed:
                progress_step = "restart_failed"
            else:
                progress_step = "done"
            await publish_switch_progress(agent.id, progress_step, error=restart_error)
            elapsed_ms = int((time.monotonic() - started_at) * 1000)
            if skip_reason:
                note = f" — Neustart übersprungen: {skip_reason}"
            elif restart_failed:
                note = f" — Neustart fehlgeschlagen (Switch bleibt gespeichert): {restart_error}"
            else:
                note = ""
            await emit_event(
                session,
                "agent.runtime_switched",
                f"{agent.name}: "
                f"{old_runtime.slug if old_runtime else 'n/a'} → {new_runtime.slug} "
                f"(host in-place){note}",
                severity="warning" if restart_failed else "info",
                agent_id=agent.id,
                board_id=agent.board_id,
                detail={
                    "old_runtime": _runtime_summary(old_runtime),
                    "new_runtime": _runtime_summary(new_runtime),
                    "image_switched": False,
                    "duration_ms": elapsed_ms,
                    "warnings": warnings,
                    "mode": "host_inplace",
                    "restart_skipped": skip_reason is not None,
                    "restart_skip_reason": skip_reason,
                    "restart_failed": restart_failed,
                    "restart_error": restart_error,
                },
            )
            return SwitchResult(
                old_runtime=_runtime_summary(old_runtime),
                new_runtime=_runtime_summary(new_runtime) or {},
                image_switched=False,
                duration_ms=elapsed_ms,
                warnings=warnings,
                dry_run=False,
                health={"healthy": skip_reason is None and not restart_failed, "mode": "host_inplace"},
                harness=effective_new_harness,
                old_harness=effective_old_harness,
                restart_skipped=skip_reason is not None,
                restart_skip_reason=skip_reason,
                restart_failed=restart_failed,
            )

        # Step 5 — render new compose overlay BEFORE touching the container.
        if image_change:
            try:
                # We need the new runtime_id reflected in the DB so the
                # renderer picks the correct image for this agent. Apply
                # the DB change first, then render.
                agent.runtime_id = new_runtime.id
                agent.harness = effective_new_harness
                if new_runtime.model_identifier:
                    agent.model = new_runtime.model_identifier
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                await write_compose_agents(session)
            except Exception as e:
                # Roll back DB and re-raise as health-check failure (cleanest
                # path for the caller — user sees rollback semantics).
                logger.error("compose render failed for %s: %s", agent.name, e)
                agent.runtime_id = snapshot_old_runtime_id
                agent.harness = snapshot_old_harness
                agent.updated_at = utcnow()
                session.add(agent)
                await session.commit()
                await session.refresh(agent)
                await _emit_failure_event(
                    session, agent, old_runtime, new_runtime,
                    reason=f"compose render failed: {e}", elapsed_ms=int((time.monotonic() - started_at) * 1000),
                )
                await publish_switch_progress(
                    agent.id, "rolled_back", error=f"compose render failed: {e}"
                )
                raise SwitchHealthCheckFailed(
                    f"Compose-Render fehlgeschlagen — kein Switch ausgefuehrt: {e}"
                ) from e
        else:
            # Same-image switch: update DB now, no compose change needed.
            agent.runtime_id = new_runtime.id
            agent.harness = effective_new_harness
            if new_runtime.model_identifier:
                agent.model = new_runtime.model_identifier
            agent.updated_at = utcnow()
            session.add(agent)
            await session.commit()
            await session.refresh(agent)

        # Step 7 — re-render claude-config files with new runtime context.
        try:
            await sync_docker_agent_files(session, agent)
        except Exception as e:
            logger.warning("sync_docker_agent_files during switch failed: %s", e)
            warnings.append(f"sync_docker_agent_files warned: {e}")

        # Step 8 — restart / recreate container.
        # D-11: same-image switches use tmux respawn-window (15-30s saved);
        # cross-image switches still need force_recreate to pull the new image.
        #
        # ADR-056 exception: omp agents render their provider config
        # (models.yml / omp.env) ONLY in entrypoint.sh at container start. A
        # tmux respawn-window re-execs Window 0 but keeps the container's
        # existing environment, so the old endpoint/model would survive a
        # same-image switch INTO omp. Force a full `docker restart` (not a
        # respawn) whenever the effective target harness is omp — the
        # entrypoint then re-runs bootstrap and emits a fresh models.yml,
        # mirroring the ADR-054 watcher's docker-restart mechanism.
        #
        # Task #26 — a busy agent only reaches this point because
        # force_when_in_progress let it past the earlier guard. Recreating
        # its container now would kill the running task, so the restart is
        # deferred (DB/config stay switched) instead of forced through.
        skip_reason = _restart_skip_reason(agent, restart_after_switch=restart_after_switch)
        health: dict[str, Any] = {}
        restart_failed = False
        restart_error: str | None = None
        if skip_reason is None:
            await publish_switch_progress(agent.id, "restarting")
            restart_result = restart_docker_agent_container(
                agent,
                force_recreate=image_change,
                respawn_window_only=(not image_change and effective_new_harness != "omp"),
            )
            status = restart_result.get("status", "")
            if status.startswith("error"):
                # Task #26 (e) — the restart COMMAND itself failing does not
                # invalidate the switch: the DB/config already point at the
                # new runtime, only the container bounce needs a retry.
                # Report it instead of rolling back and skip the (pointless)
                # health probe of a container we never restarted.
                restart_failed = True
                restart_error = f"container restart failed: {status}"
                logger.warning(
                    "container restart after runtime switch failed for %s: %s",
                    agent.name, status,
                )
            else:
                # Step 9 — wait for container to be reachable.
                # D-12: respawn_mode delegates to tmux capture-pane polling instead of
                # docker inspect, matching the respawn restart path above.
                timeout = HEALTH_TIMEOUT_RECREATE if image_change else HEALTH_TIMEOUT_RESTART
                # ADR-049: the omp runtime now runs omp's native TUI in Window 0 (not the
                # headless bridge print). Anchor readiness on the TUI's prompt glyphs via
                # pane scrape regardless of image_change — the initial openclaude→omp
                # switch is cross-image (respawn_mode=False), whose docker-inspect check
                # would report healthy before the TUI is up. The glyphs match the omp
                # chat prompt box ("╭─" frame + "❯" input) shown after setup-wizard skip.
                is_omp = (
                    effective_new_harness == "omp"
                    if effective_new_harness
                    else new_runtime.runtime_type == "omp"
                )
                await publish_switch_progress(agent.id, "waiting_healthy")
                health = await wait_for_agent_healthy(
                    agent,
                    timeout=timeout,
                    respawn_mode=(not image_change),
                    ready_signals=("╭─", "❯") if is_omp else None,
                )
                if not health.get("healthy"):
                    # Unlike the restart-command failure above, this is the
                    # existing (pre-#26) safety net: the container DID
                    # restart but never came up healthy on the new runtime —
                    # that is evidence the new runtime itself is broken, so
                    # rolling back to the last known-good binding stays the
                    # right call. Behaviour intentionally unchanged.
                    await _rollback(session, agent, snapshot_old_runtime_id, image_change, old_harness=snapshot_old_harness)
                    await _emit_failure_event(
                        session, agent, old_runtime, new_runtime,
                        reason=f"health check failed: {health.get('reason')}",
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                    )
                    await publish_switch_progress(
                        agent.id,
                        "rolled_back",
                        error=f"health check failed: {health.get('reason')}",
                    )
                    raise SwitchHealthCheckFailed(
                        f"Health-Check nach Restart fehlgeschlagen "
                        f"({health.get('reason')}) — Rollback ausgefuehrt."
                    )

                # Step 11 — broadcast for Sessions auto-remount BEFORE the activity event.
                await _publish_terminal_remount(agent.id, image_changed=image_change)

        if skip_reason:
            progress_step = "restart_skipped"
        elif restart_failed:
            progress_step = "restart_failed"
        else:
            progress_step = "done"
        await publish_switch_progress(agent.id, progress_step, error=restart_error)

        # Step 12 — success event.
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        if skip_reason:
            note = f" — Neustart übersprungen: {skip_reason}"
        elif restart_failed:
            note = f" — Neustart fehlgeschlagen (Switch bleibt gespeichert): {restart_error}"
        else:
            note = ""
        await emit_event(
            session,
            "agent.runtime_switched",
            f"{agent.name}: "
            f"{old_runtime.slug if old_runtime else 'n/a'} → {new_runtime.slug}{note}",
            severity="warning" if restart_failed else "info",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={
                "old_runtime": _runtime_summary(old_runtime),
                "new_runtime": _runtime_summary(new_runtime),
                "image_switched": image_change,
                "duration_ms": elapsed_ms,
                "warnings": warnings,
                "restart_skipped": skip_reason is not None,
                "restart_skip_reason": skip_reason,
                "restart_failed": restart_failed,
                "restart_error": restart_error,
            },
        )

        return SwitchResult(
            old_runtime=_runtime_summary(old_runtime),
            new_runtime=_runtime_summary(new_runtime) or {},
            image_switched=image_change,
            duration_ms=elapsed_ms,
            warnings=warnings,
            dry_run=False,
            health=dict(health),
            harness=effective_new_harness,
            restart_skipped=skip_reason is not None,
            restart_skip_reason=skip_reason,
            restart_failed=restart_failed,
            old_harness=effective_old_harness,
        )

    finally:
        await _release_lock(agent.id)


# ── Internal helpers ──────────────────────────────────────────────────────


async def _rollback(
    session: AsyncSession,
    agent: Agent,
    old_runtime_id: uuid.UUID | None,
    image_change: bool,
    *,
    old_harness: str | None = None,
) -> None:
    """Restore DB + files + image overlay + container to the pre-switch state."""
    try:
        agent.runtime_id = old_runtime_id
        agent.harness = old_harness
        agent.updated_at = utcnow()
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    except Exception as e:  # pragma: no cover — defensive
        logger.error("rollback DB step failed for %s: %s", agent.name, e)

    if image_change:
        try:
            await write_compose_agents(session)
        except Exception as e:  # pragma: no cover
            logger.error("rollback compose render failed: %s", e)

    try:
        await sync_docker_agent_files(session, agent)
    except Exception as e:  # pragma: no cover
        logger.error("rollback sync failed: %s", e)

    try:
        restart_docker_agent_container(agent, force_recreate=image_change)
    except Exception as e:
        logger.error("rollback restart failed for %s: %s", agent.name, e)
        # The container may be down while DB shows old (pre-switch) runtime.
        # Surface this broken state through three channels so the operator can act:
        #   1. Activity event (severity=error) — visible in MC UI activity feed.
        #   2. Discord ops notification — pings the operator even if not watching MC.
        #   3. provision_status = "error" — AgentCard shows red error badge in UI.
        try:
            await emit_event(
                session,
                "agent.runtime_rollback_failed",
                f"{agent.name}: Rollback-Neustart fehlgeschlagen — Container manuell prüfen",
                severity="error",
                agent_id=agent.id,
                board_id=agent.board_id,
                detail={
                    "rollback_status": "container_unreachable",
                    "error": str(e),
                    "old_runtime_id": str(old_runtime_id) if old_runtime_id else None,
                },
            )
        except Exception as emit_err:  # pragma: no cover
            logger.error("emit rollback_failed event failed for %s: %s", agent.name, emit_err)

        try:
            await send_discord_notification(
                title=f"🚨 Agent {agent.name} container DOWN after rollback",
                description=(
                    f"Runtime rollback restart failed — container may be unreachable.\n"
                    f"Agent ID: `{agent.id}`\n"
                    f"Error: `{e}`"
                ),
                severity="error",
            )
        except Exception as discord_err:  # pragma: no cover
            logger.error("discord rollback notification failed for %s: %s", agent.name, discord_err)

        try:
            agent.provision_status = "error"
            session.add(agent)
            await session.commit()
        except Exception as db_err:  # pragma: no cover
            logger.error("set provision_status=error failed for %s: %s", agent.name, db_err)


async def _emit_failure_event(
    session: AsyncSession,
    agent: Agent,
    old_runtime: Runtime | None,
    new_runtime: Runtime,
    *,
    reason: str,
    elapsed_ms: int,
) -> None:
    try:
        await emit_event(
            session,
            "agent.runtime_switch_failed",
            f"{agent.name}: Switch fehlgeschlagen ({new_runtime.slug}) — {reason}",
            severity="warning",
            agent_id=agent.id,
            board_id=agent.board_id,
            detail={
                "old_runtime": _runtime_summary(old_runtime),
                "attempted_runtime": _runtime_summary(new_runtime),
                "reason": reason,
                "duration_ms": elapsed_ms,
                "rolled_back": True,
            },
        )
    except Exception as e:  # pragma: no cover
        logger.error("emit failure event failed for %s: %s", agent.name, e)
