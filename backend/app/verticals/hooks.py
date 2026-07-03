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
