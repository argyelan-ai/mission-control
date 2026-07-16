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


# Async hooks, called when a task lands in status=review, before the generic
# "wait for a human reviewer" flow runs (task_lifecycle.handle_human_review_
# handoff). Signature: async (session, task) -> bool. Return True when the
# hook fully handled/finalized the task itself (e.g. bench_studio finalizing
# a one-shot bench task straight to done) — the caller then skips its own
# default review-wait side effects. Return False for a no-op / self-filtered
# skip. Errors are logged and swallowed (treated as False, i.e. "not
# handled") — a broken hook falls back to the normal human-review flow
# instead of breaking the request.
task_review_hooks: list[Callable[..., Awaitable[bool]]] = []


async def run_task_review_hooks(session: Any, task: Any) -> bool:
    """Run all task_review_hooks; first True wins. Log errors, never propagate."""
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    for hook in task_review_hooks:
        try:
            if await hook(session, task):
                return True
        except Exception:
            logger.exception("task_review hook %s failed", getattr(hook, "__name__", hook))
    return False


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


# Async providers, called when a bench challenge's detail is fetched, that
# contribute extra action buttons to the operator UI. Signature:
#   async (session, challenge, entries) -> list[dict]
# Each dict is an action descriptor:
#   {
#     "id": str,               # stable key, used as the React list key
#     "label": str,            # button text
#     "style": "default" | "primary" | "danger",
#     "method": "POST",        # HTTP method for the click handler
#     "endpoint": str,         # absolute API path, e.g. "/api/v1/x/..."
#                               # — provider substitutes any {id}-style
#                               # placeholders itself before returning
#     "confirm": str | None,   # optional confirm-dialog text before firing
#     "disabled": bool,
#     "disabled_reason": str | None,
#     "busy": bool,             # show a spinner + force-disable (e.g. a
#                                # background job for this challenge is
#                                # already running)
#   }
# Registered by verticals (e.g. a private catalog_publisher overlay adding a
# "Publish" button). Empty providers list = no actions, nothing renders.
challenge_actions_providers: list[Callable[..., Awaitable[list[dict]]]] = []


async def collect_challenge_actions(session: Any, challenge: Any, entries: Any) -> list[dict]:
    """Run all challenge_actions_providers and concatenate their results.

    A raising provider is logged and skipped (its actions are simply
    omitted) — one broken vertical must never break the challenge detail
    endpoint for everyone else.
    """
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    actions: list[dict] = []
    for provider in challenge_actions_providers:
        try:
            actions.extend(await provider(session, challenge, entries))
        except Exception:
            logger.exception(
                "challenge_actions provider %s failed", getattr(provider, "__name__", provider)
            )
    return actions


# Async hooks, called after an Approval is resolved (approved OR rejected)
# whose action_type has NO dedicated core handler (x_post has its own hook
# call inside _handle_x_post_resolution — see approvals.py's
# _CORE_HANDLED_ACTION_TYPES set for the full list of core-handled types).
# Signature: async (session, approval, resolution_status: str) -> None.
# Lets overlay verticals react to their own custom approval action_types
# (e.g. a "catalog_publish" approval) without core needing to know they
# exist. Errors are logged and swallowed.
approval_resolved_hooks: list[Callable[..., Awaitable[None]]] = []


async def run_approval_resolved_hooks(session: Any, approval: Any, resolution_status: str) -> None:
    """Run all approval_resolved_hooks; log errors, never propagate."""
    import logging

    logger = logging.getLogger("mc.verticals.hooks")
    for hook in approval_resolved_hooks:
        try:
            await hook(session, approval, resolution_status)
        except Exception:
            logger.exception(
                "approval_resolved hook %s failed", getattr(hook, "__name__", hook)
            )
