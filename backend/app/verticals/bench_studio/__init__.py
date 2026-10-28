"""Benchmark Studio vertical (ADR-044, ADR-070).

Same-prompt showdowns ("Testen in 2 Taps", W1 24.10.2026): MC builds the
plan from what it knows, runs the model (class A, direct API, n runs) and/or
an agent (class B), records run 1, writes a write-once receipt, gates it and
renders the X-Kit (video + card + texts). Mark posts himself — no X path
from here (W1 kill list).

Strippable: delete this directory and the app boots unchanged. Coupling into
core exclusively via app.verticals.hooks (task_review_hooks + task_done_hooks
+ completion_callback_skip_hooks + task_workspace_providers +
background_loops). Core NEVER imports
this package.
"""
from __future__ import annotations


def register(app) -> None:
    """Called once by app.verticals.register_all() during bootstrap."""
    from app.verticals import hooks

    from .callbacks import owns_completion_followup  # noqa: PLC0415
    from .orchestrator import on_task_done, on_task_review  # noqa: PLC0415
    from .routers import router  # noqa: PLC0415
    from .orchestrator import reconcile_loop  # noqa: PLC0415
    from .w1_routes import router as w1_router  # noqa: PLC0415
    from .w1_runner import w1_loop  # noqa: PLC0415

    app.include_router(router)
    # Bench W1 contract (24.10.2026): plan, tests, X-Kit, runtime summary,
    # auto test after a recipe switch — see CONTRACT.md.
    app.include_router(w1_router)
    # W0.8: orphan sweep (hooks.background_loops) — heals challenges whose
    # agent task failed, was aborted or deleted, without anyone opening them.
    if reconcile_loop not in hooks.background_loops:
        hooks.background_loops.append(reconcile_loop)
    # Bench W1: the one-test-at-a-time runner (waiting tests, auto tests
    # after a recipe switch, probe warming on a quiet box, restart heal).
    if w1_loop not in hooks.background_loops:
        hooks.background_loops.append(w1_loop)
    if on_task_review not in hooks.task_review_hooks:
        hooks.task_review_hooks.append(on_task_review)
    if on_task_done not in hooks.task_done_hooks:
        hooks.task_done_hooks.append(on_task_done)
    if owns_completion_followup not in hooks.completion_callback_skip_hooks:
        hooks.completion_callback_skip_hooks.append(owns_completion_followup)
    # A bench agent task starts in an empty folder of its own, never in a
    # worktree of the shared ad-hoc scratch repo (live finding 28.10.2026).
    from .orchestrator import bench_task_workspace  # noqa: PLC0415

    if bench_task_workspace not in hooks.task_workspace_providers:
        hooks.task_workspace_providers.append(bench_task_workspace)
