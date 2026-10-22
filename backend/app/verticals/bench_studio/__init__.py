"""Benchmark Studio vertical (ADR-044, ADR-070).

One-shot LLM capability demos: prompt -> N models generate index.html ->
mc-playwright records videos -> ffmpeg grid -> Studio review -> X-post draft
via the CORE Approval + ContentPipeline lifecycles (no second lifecycle).

Strippable: delete this directory and the app boots unchanged. Coupling into
core exclusively via app.verticals.hooks (task_review_hooks + task_done_hooks +
x_post_resolved_hooks + background_loops). Core NEVER imports this package.
"""
from __future__ import annotations


def register(app) -> None:
    """Called once by app.verticals.register_all() during bootstrap."""
    from app.verticals import hooks

    from .drafts import on_x_post_resolved  # noqa: PLC0415
    from .orchestrator import (  # noqa: PLC0415
        on_task_done,
        on_task_review,
        reconcile_loop,
    )
    from .routers import router  # noqa: PLC0415

    app.include_router(router)
    if on_task_review not in hooks.task_review_hooks:
        hooks.task_review_hooks.append(on_task_review)
    if on_task_done not in hooks.task_done_hooks:
        hooks.task_done_hooks.append(on_task_done)
    if on_x_post_resolved not in hooks.x_post_resolved_hooks:
        hooks.x_post_resolved_hooks.append(on_x_post_resolved)
    # W0.8: orphan sweep — heals challenges whose agent task failed, was
    # aborted or deleted, without anyone opening them.
    if reconcile_loop not in hooks.background_loops:
        hooks.background_loops.append(reconcile_loop)
