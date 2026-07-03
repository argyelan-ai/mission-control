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
) -> str:
    """Assemble a sparkrun launch command for a given recipe.

    Args:
        recipe: registry-qualified recipe name (e.g. ``@official/qwen3.6...``)
        slug: runtime slug — used as the ``mc.runtime.slug`` docker label so
            lifecycle ops can find the resulting container
        flags: extra flags appended after the recipe. Defaults preserve the
            single-node-mode + persistent-container + idempotency contract
            that ``runtime_manager.start_runtime`` relies on.
    """
    # Validate slug is shell-safe (alnum + - + _ only). Defensive — slugs
    # already pass DB constraints but this prevents accidental injection if
    # a future caller passes user input.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        raise ValueError(f"slug must be alphanumeric / _ / -: {slug!r}")
    if not re.fullmatch(r"[@\w./-]+", recipe):
        raise ValueError(f"recipe contains invalid characters: {recipe!r}")
    return (
        f"uvx sparkrun run {recipe} {flags} "
        f"--label mc.runtime.slug={slug}"
    )


async def list_recipes(host: ResolvedHost | None = None) -> list[dict[str, Any]]:
    """Run ``uvx sparkrun list`` on the Spark host, parse + return entries.

    Each row from ``sparkrun list`` looks like:
        ``@official/qwen3.6-35b-a3b-fp8-vllm  vllm-distributed  1  1  0.8  Qwen/...``

    We extract: name, runtime type, model identifier. Anything we can't
    confidently parse is skipped (logged). ``host=None`` → settings-fallback
    box (ADR-048), same as before the host registry.
    """
    from app.services.runtime_manager import _ssh_run  # noqa: SLF001

    stdout, stderr, exit_code = await _ssh_run(
        "PATH=$HOME/.local/bin:$PATH uvx sparkrun list 2>&1", host=host
    )
    if exit_code != 0:
        logger.warning(
            "sparkrun list failed (exit %d): %s", exit_code, stderr or stdout[:200]
        )
        return []

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
        recipes.append({
            "name": name,
            "model": model,
            "registry": registry,
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
    from app.services.runtime_model_resolver import invalidate_and_reprobe

    old_command = runtime.launch_command or ""
    old_recipe = extract_current_recipe(old_command)
    new_command = build_launch_command(new_recipe, slug=runtime.slug)

    if old_recipe == new_recipe:
        return {"ok": True, "message": f"Recipe already {new_recipe} — no-op."}

    logger.info(
        "switch_recipe: runtime=%s %s → %s",
        runtime.slug, old_recipe, new_recipe,
    )

    # Resolve the runtime's host (ADR-048) — eviction + start run host-scoped,
    # so a switch on box A never stops models on box B.
    host = await resolve_host_for_runtime(session, runtime)

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
