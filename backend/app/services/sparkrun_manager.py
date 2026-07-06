"""Sparkrun Recipe Manager.

Bridges Mission Control to the ``sparkrun`` CLI on the DGX Spark host. Provides:

- :func:`list_recipes` — enumerate registry recipes available on the Spark
- :func:`extract_current_recipe` — parse the recipe name from a runtime's
  ``launch_command`` string
- :func:`build_launch_command` — assemble a fresh ``launch_command`` for a
  given recipe (preserves the labelling + ``--solo --no-rm --ensure`` flags
  Mission Control depends on for lifecycle management)
- :func:`switch_recipe` — atomic stop → swap launch_command → start →
  re-probe model

Used by the ``/api/v1/runtimes`` routes for recipe-aware vllm_docker runtimes.

Design notes
------------
The recipe name lives implicitly in ``Runtime.launch_command`` because
sparkrun's CLI takes the recipe as a positional arg. Rather than add a new
column, we parse + rebuild on switch. This keeps the DB schema minimal and
makes the launch_command field the single source of truth.

"label-coupled" lifecycle: every command we generate adds
``--label mc.runtime.slug=<slug>`` so future ``docker ps`` / ``docker stop``
calls can find the container regardless of which random ID sparkrun
assigned it. This is the contract that makes recipe switches reliable.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services.host_resolver import ResolvedHost, resolve_host_for_runtime

logger = logging.getLogger(__name__)


# Recipe name pattern: ``@official/qwen3.6-35b-a3b-fp8-vllm`` or bare names.
# Sparkrun accepts both ``@registry/recipe`` and ``recipe`` forms.
_RECIPE_RE = re.compile(r"(?:^|\s)(@?[\w./-]+?)(?:\s|$)")


def extract_current_recipe(launch_command: str | None) -> str | None:
    """Return the recipe name from a launch_command, or ``None``.

    Looks for the first token after ``sparkrun run``. Handles both registry-
    qualified (``@official/foo``) and bare (``foo``) names.
    """
    if not launch_command:
        return None
    # Tokenise the command and find ``sparkrun run <recipe>``.
    try:
        tokens = shlex.split(launch_command)
    except ValueError:
        # Unbalanced quotes etc. — give up cleanly.
        return None
    for idx, tok in enumerate(tokens):
        if tok == "sparkrun" and idx + 2 < len(tokens) and tokens[idx + 1] == "run":
            return tokens[idx + 2]
    return None


def build_launch_command(
    recipe: str,
    *,
    slug: str,
    flags: str = "--solo --no-rm --ensure --no-follow",
    tp_override: int | None = None,
) -> str:
    """Assemble a sparkrun launch command for a given recipe.

    Args:
        recipe: registry-qualified recipe name (e.g. ``@official/qwen3.6...``)
        slug: runtime slug — used as the ``mc.runtime.slug`` docker label so
            lifecycle ops can find the resulting container
        flags: extra flags appended after the recipe. Defaults preserve the
            single-node-mode + persistent-container + idempotency contract
            that ``runtime_manager.start_runtime`` relies on.
        tp_override: when set, injects ``--tensor-parallel N`` after ``flags``.
            Used by :func:`switch_recipe` to downscale a recipe's default
            tensor-parallel size to what the target host actually has (e.g.
            a recipe defaulting to ``tp=2`` forced to ``tp=1`` on a 1-GPU
            host). ``--solo`` alone does NOT control this — it only affects
            sparkrun's ray/node bootstrap, never the tp value baked into the
            recipe (this was the root cause of the original solo-launch bug:
            see ADR-059).
    """
    # Validate slug is shell-safe (alnum + - + _ only). Defensive — slugs
    # already pass DB constraints but this prevents accidental injection if
    # a future caller passes user input.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise ValueError(f"slug must be alphanumeric / _ / -: {slug!r}")
    if not re.fullmatch(r"[@\w./-]+", recipe):
        raise ValueError(f"recipe contains invalid characters: {recipe!r}")
    tp_flag = f" --tensor-parallel {int(tp_override)}" if tp_override else ""
    return (
        f"uvx sparkrun run {recipe} {flags}{tp_flag} "
        f"--label mc.runtime.slug={slug}"
    )


def _parse_recipe_count(value: str | None) -> int | None:
    """Parse a TP/Nodes column value. ``sparkrun list`` prints ``-`` for
    recipes that don't declare the field (e.g. some autoround entries) —
    that, and anything else non-numeric, becomes ``None`` rather than a
    crash."""
    if value is None:
        return None
    return int(value) if value.isdigit() else None


async def get_host_gpu_count(host: ResolvedHost | None = None) -> int:
    """Number of GPUs on the target host, via ``nvidia-smi -L | wc -l``.

    Used to decide whether a recipe's declared ``tp`` (tensor-parallel size)
    fits the box it would actually run on — the DGX Spark has exactly 1 GPU
    (GB10), but this must not be hardcoded so the same logic works on a
    future multi-GPU host. Falls back to ``1`` (the conservative, single-GPU
    assumption) on any SSH/parse failure — never raises, since this feeds a
    best-effort UI hint + launch-time guard, not a hard precondition.
    """
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    try:
        stdout, stderr, exit_code = await _ssh_run(
            "nvidia-smi -L | wc -l", host=host, timeout=15
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_host_gpu_count: SSH failed (%s) — falling back to 1", exc)
        return 1
    if exit_code == 0:
        try:
            count = int(stdout.strip())
        except ValueError:
            count = 0
        if count > 0:
            return count
    logger.warning(
        "get_host_gpu_count: unexpected nvidia-smi output (exit=%d): %r / %r — "
        "falling back to 1",
        exit_code, stdout, stderr,
    )
    return 1


async def list_recipes(
    host: ResolvedHost | None = None,
    *,
    host_gpu_count: int | None = None,
) -> list[dict[str, Any]]:
    """Run ``uvx sparkrun list`` on the Spark host, parse + return entries.

    Each row from ``sparkrun list`` looks like:
        ``@official/qwen3.6-35b-a3b-fp8-vllm  vllm-distributed  1  1  0.8  Qwen/...``

    Columns (whitespace-separated): Name, Runtime, TP, Nodes, GPU-Mem, Model,
    Registry. TP/Nodes may be ``-`` for recipes that don't declare them.

    We extract: name, model identifier, registry, tp, nodes, and a derived
    ``solo_capable`` flag (``nodes<=1`` and ``tp<=host_gpu_count``) — the
    signal MC's recipe switcher was missing (ADR-059): a recipe requiring
    more GPUs than the host has (e.g. a ``vllm-ray`` variant with ``tp=2`` on
    a 1-GPU Spark) looked identical to a solo-ready one and silently failed
    to start. Anything we can't confidently parse is skipped (logged).
    ``host=None`` → settings-fallback box (ADR-048), same as before the host
    registry. ``host_gpu_count`` lets callers reuse an already-probed value
    (e.g. :func:`switch_recipe`); when omitted it's probed once here.
    """
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    try:
        stdout, stderr, exit_code = await _ssh_run(
            "PATH=$HOME/.local/bin:$PATH uvx sparkrun list 2>&1", host=host
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sparkrun list raised (%s) — treating as unavailable", exc)
        return []
    if exit_code != 0:
        logger.warning(
            "sparkrun list failed (exit %d): %s", exit_code, stderr or stdout[:200]
        )
        return []

    if host_gpu_count is None:
        host_gpu_count = await get_host_gpu_count(host)

    recipes: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        # Skip header / blank lines
        if not line or line.startswith(("NAME", "Name", "-")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # First token is the recipe name. Skip it when scanning for the
        # model identifier — recipe names contain ``/`` (e.g. ``@official/foo``)
        # which would otherwise match the heuristic.
        name = parts[0]
        # Model identifier looks like ``foo/bar`` or ``foo-bar:tag``. Search
        # only in the tail to skip the recipe name itself.
        model = next(
            (p for p in parts[1:] if "/" in p or ":" in p), None
        )
        # Registry prefix is anything between leading `@` and `/`.
        # sparkrun ships @official, @eugr, @sparkrun-transitional, @community, …
        # rather than enumerating them, just extract the segment.
        if name.startswith("@") and "/" in name:
            registry = name[1:].split("/", 1)[0]
        else:
            registry = "local"
        # TP/Nodes sit at fixed columns (index 2/3) per the sparkrun list
        # layout. Missing columns (short/malformed lines) parse to None
        # rather than raising — same "skip what we can't confidently read"
        # policy as the model/registry heuristics above.
        tp = _parse_recipe_count(parts[2]) if len(parts) > 2 else None
        nodes = _parse_recipe_count(parts[3]) if len(parts) > 3 else None
        solo_capable = (nodes is None or nodes <= 1) and (
            tp is None or tp <= host_gpu_count
        )
        recipes.append({
            "name": name,
            "model": model,
            "registry": registry,
            "tp": tp,
            "nodes": nodes,
            "solo_capable": solo_capable,
        })
    logger.info("sparkrun list returned %d recipes", len(recipes))
    return recipes


async def switch_recipe(
    session: AsyncSession,
    runtime: Runtime,
    new_recipe: str,
) -> dict[str, Any]:
    """Atomic recipe switch: evict → update launch_command → start → re-probe.

    Order matters:
      1. Evict ALL running Spark model containers (label + ``sparkrun_*_solo``
         sweep) and wait until the GPU/RAM is actually free
         (``runtime_manager.evict_spark_runtime_containers``). This replaces the
         old best-effort ``stop_runtime(container_name)`` — container_name is
         None right after a switch, and CLI/externally-started models carry a
         different name, so the old path silently left the previous model
         running and the new one OOMed against a full box. A failed eviction
         ABORTS the switch (we never start a second model on an occupied box).
      2. Persist the new ``launch_command`` to DB. Done after a *successful*
         evict so the DB only points at a recipe we actually attempted to run.
      3. Call ``runtime_manager.start_runtime`` which executes the new
         launch_command via SSH and verifies the container actually appears.
      4. Trigger ``runtime_model_resolver.invalidate_and_reprobe`` so the
         new model_identifier gets picked up automatically.

    Returns a status dict with ``ok``, ``message``, optional ``model``.
    """
    from app.services import runtime_manager
    from app.services.activity import emit_event
    from app.services.runtime_model_resolver import invalidate_and_reprobe

    old_command = runtime.launch_command or ""
    old_recipe = extract_current_recipe(old_command)

    if old_recipe == new_recipe:
        return {"ok": True, "message": f"Recipe already {new_recipe} — no-op."}

    logger.info(
        "switch_recipe: runtime=%s %s → %s",
        runtime.slug, old_recipe, new_recipe,
    )

    # Resolve the runtime's host (ADR-048) — eviction + start run host-scoped,
    # so a switch on box A never stops models on box B.
    host = await resolve_host_for_runtime(session, runtime)

    # Solo-capability guard (ADR-059) — best-effort, never blocks on its own
    # failure. Two outcomes:
    #   - the target recipe needs >1 physical node → this single-host
    #     deployment can NEVER run it; abort BEFORE evicting the current
    #     model so an unwinnable switch doesn't kill a healthy engine.
    #   - the target recipe needs more GPUs (tp) than the host has, but only
    #     1 node → downscale via `--tensor-parallel <host_gpu_count>` and
    #     proceed. Whether that actually fits in VRAM is something only vLLM
    #     itself can determine; the post-launch process check in
    #     `runtime_manager.start_runtime` is the safety net if it doesn't.
    # A recipe absent from `sparkrun list` (unknown/local name) or an
    # unreachable Spark host during the check both mean "can't validate" —
    # proceed without a guard rather than block on missing information.
    tp_override: int | None = None
    abort_nodes: int | None = None
    try:
        host_gpu_count = await get_host_gpu_count(host)
        recipes = await list_recipes(host=host, host_gpu_count=host_gpu_count)
        target = next((r for r in recipes if r["name"] == new_recipe), None)
        if target is None:
            logger.info(
                "switch_recipe: %s not found in `sparkrun list` output — "
                "skipping solo-capability guard.", new_recipe,
            )
        else:
            nodes = target.get("nodes")
            if nodes is not None and nodes > 1:
                abort_nodes = nodes
            else:
                tp = target.get("tp")
                if tp is not None and tp > host_gpu_count:
                    tp_override = host_gpu_count
                    logger.info(
                        "switch_recipe: %s defaults to tp=%s, downscaling to "
                        "tp=%s for this host (%s GPU(s)) — best-effort, may "
                        "still OOM if the model doesn't fit on fewer GPUs.",
                        new_recipe, tp, tp_override, host_gpu_count,
                    )
    except Exception as exc:  # noqa: BLE001 — the guard is best-effort
        logger.warning(
            "switch_recipe: solo-capability check unavailable (%s) — "
            "proceeding without the guard.", exc,
        )

    # Abort BEFORE evicting anything — a switch that can never succeed must
    # not kill the currently-running (healthy) model first. Kept outside the
    # try/except above: a failure while emitting the activity event must not
    # be swallowed and silently downgrade this into "proceed anyway".
    if abort_nodes is not None:
        message = (
            f"Switch abgebrochen — Recipe '{new_recipe}' braucht {abort_nodes} "
            f"Nodes (Multi-Host-Cluster), dieser Host stellt nur 1 Node bereit. "
            f"Nicht solo-startbar."
        )
        logger.warning("switch_recipe: %s", message)
        try:
            await emit_event(
                session,
                "runtime.recipe_switch_rejected",
                f"{runtime.slug}: {new_recipe} braucht {abort_nodes} Nodes — abgelehnt",
                severity="warning",
                detail={
                    "slug": runtime.slug, "recipe": new_recipe,
                    "nodes": abort_nodes, "reason": "multi_node",
                },
            )
        except Exception as exc:  # noqa: BLE001 — the abort itself must not fail
            logger.warning("switch_recipe: failed to emit rejection event: %s", exc)
        return {
            "ok": False,
            "message": message,
            "old_recipe": old_recipe,
            "new_recipe": new_recipe,
        }

    new_command = build_launch_command(
        new_recipe, slug=runtime.slug, tp_override=tp_override
    )

    # 1. Evict ALL running Spark model containers (label + solo sweep) and wait
    # until the box is free. A failed eviction ABORTS — starting a second model
    # on top of a still-occupied GPU/RAM is the exact failure we're fixing.
    evict_result = await runtime_manager.evict_spark_runtime_containers(
        runtime.slug, host=host
    )
    if not evict_result.get("ok"):
        logger.error(
            "switch_recipe: eviction failed for %s — aborting switch: %s",
            runtime.slug, evict_result.get("message"),
        )
        return {
            "ok": False,
            "message": (
                f"Switch abgebrochen — alte Modell-Container nicht freigegeben: "
                f"{evict_result.get('message')}"
            ),
            "old_recipe": old_recipe,
            "new_recipe": new_recipe,
        }

    # 2. Persist new launch_command + clear stale model_identifier so the
    # resolver re-probes against the freshly-launched recipe.
    runtime.launch_command = new_command
    runtime.model_identifier = None
    runtime.container_name = None  # sparkrun assigns a fresh ID on each run
    session.add(runtime)
    await session.commit()
    await session.refresh(runtime)

    # 3. Start with new recipe. Re-read dict so it reflects the persisted
    # launch_command.
    runtime_dict = _to_runtime_dict(runtime)
    start_result = await runtime_manager.start_runtime(runtime_dict, host=host)
    if not start_result.get("ok"):
        # Surface the launch failure as a first-class activity event — this is
        # the fix for the original incident's silent failure mode: sparkrun
        # reported success (exit 0, fire-and-forget) while vLLM never actually
        # came up, and nothing told Mark. start_runtime's own process-liveness
        # check now catches that case; here we make sure it's visible.
        try:
            await emit_event(
                session,
                "runtime.launch_failed",
                f"{runtime.slug}: recipe switch to {new_recipe} failed to start",
                severity="error",
                detail={
                    "slug": runtime.slug, "recipe": new_recipe,
                    "reason": start_result.get("message"),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("switch_recipe: failed to emit launch_failed event: %s", exc)
        return {
            "ok": False,
            "message": f"Recipe persisted but start failed: {start_result.get('message')}",
            "old_recipe": old_recipe,
            "new_recipe": new_recipe,
        }

    # 4. Trigger probe — won't complete instantly (vllm takes minutes to load),
    # but kicks off the auto-detection so the next request gets the new model.
    # We don't await/block here on the load — the resolver will negative-cache
    # briefly then re-probe.
    try:
        await invalidate_and_reprobe(session, runtime.slug)
    except Exception as exc:  # noqa: BLE001
        logger.debug("switch_recipe: post-start re-probe failed (expected during warmup): %s", exc)

    return {
        "ok": True,
        "message": (
            f"Recipe switch initiated: {old_recipe} → {new_recipe}. "
            f"Container warmup typically takes 2-5 min."
        ),
        "old_recipe": old_recipe,
        "new_recipe": new_recipe,
        "launch_command": new_command,
    }


def _to_runtime_dict(runtime: Runtime) -> dict[str, Any]:
    """Coerce a Runtime row into the dict shape runtime_manager.* expects.

    Mirrors ``Runtime.to_registry_dict`` but inline to avoid a circular
    import. Kept narrow — only the fields runtime_manager reads.
    """
    return {
        "id": str(runtime.id),
        "slug": runtime.slug,
        "display_name": runtime.display_name,
        "runtime_type": runtime.runtime_type,
        "endpoint": runtime.endpoint,
        "container_name": runtime.container_name,
        "host": runtime.host,
        "launch_command": runtime.launch_command,
        "lms_identifier": runtime.lms_identifier,
        "lms_cli_path": runtime.lms_cli_path,
    }
