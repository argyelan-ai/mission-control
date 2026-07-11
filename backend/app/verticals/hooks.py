"""Hook registry — the ONLY coupling direction Vertical → Core.

Core code calls the hook lists without knowing whether a vertical has
populated them. Verticals register their callables in ``register(app)``.
Empty lists = no-op (stripped public release).

This module deliberately lives in core (never stripped) and imports
NOTHING from vertical packages itself.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

# Async hooks, called after a task transitions to status=done.
# Signature: async (session, task) -> None. Errors are logged and
# swallowed by the caller (vertical sync must never break the task flow).
task_done_hooks: list[Callable[..., Awaitable[None]]] = []

# TOOLS.md extra sections: (scope_string, builder) — builder(ctx) -> str.
# tools_md_builder only renders the section if the agent has the scope.
tools_md_sections: list[tuple[str, Callable[[dict], str]]] = []


async def run_task_done_hooks(session: Any, task: Any) -> None:
    """Run all task_done_hooks; log errors, never propagate."""
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    for hook in task_done_hooks:
        try:
            await hook(session, task)
        except Exception:
            logger.exception("task_done hook %s failed", getattr(hook, "__name__", hook))


# Async hooks, called after an x_post approval is resolved — approved AND
# rejected. Signature:
#   async (session, approval, resolution_status: str, result: dict | None) -> None
# `result` is the x_publisher result dict when the post was attempted
# (approve path), None on reject. Registered by verticals (e.g. bench_studio
# flips its challenge to `published`). Errors are logged and swallowed.
x_post_resolved_hooks: list[Callable[..., Awaitable[None]]] = []


async def run_x_post_resolved_hooks(
    session: Any, approval: Any, resolution_status: str, result: Any
) -> None:
    """Run all x_post_resolved_hooks; log errors, never propagate."""
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    for hook in x_post_resolved_hooks:
        try:
            await hook(session, approval, resolution_status, result)
        except Exception:
            logger.exception(
                "x_post_resolved hook %s failed", getattr(hook, "__name__", hook)
            )
