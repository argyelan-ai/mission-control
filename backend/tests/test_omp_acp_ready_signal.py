"""Health-gate additive sentinel for OMP_DRIVER=acp (fix omp-acp-no-tui-window).

Window 0 no longer runs the native TUI for an ACP-driven omp agent (see
docker/omp-bridge/entrypoint.sh + test_omp_acp_no_tui_window.py) — it prints
`OMP_ACP_READY` into a plain shell instead. Without an update to the
health-gate glyph tuples (`agent_runtime_switch.OMP_READY_SIGNALS`, consumed
by `runtime_propagation.py` and `docker_agent_sync._wait_for_window_ready`),
every restart/recreate/switch health-wait for such an agent would time out —
the native TUI prompt glyphs (`╭─`, `❯`) never appear.

RED (pre-fix): OMP_READY_SIGNALS == ("╭─", "❯") — no ACP sentinel, and the
switch call site literal ("╭─", "❯") would still hard-fail on OMP_ACP_READY.
GREEN (post-fix): OMP_ACP_READY is in the shared tuple and both
runtime_propagation call sites derive from it (no duplicate literal to drift).
"""
from app.services.agent_runtime_switch import OMP_READY_SIGNALS


def test_omp_ready_signals_includes_native_glyphs_and_acp_sentinel():
    assert "╭─" in OMP_READY_SIGNALS
    assert "❯" in OMP_READY_SIGNALS
    assert "OMP_ACP_READY" in OMP_READY_SIGNALS


def test_runtime_propagation_reuses_the_shared_tuple_not_a_duplicate_literal():
    """Both call sites in runtime_propagation.py must import the shared
    constant rather than carry their own `("╭─", "❯")` literal — a duplicate
    would silently miss the ACP sentinel on the next edit."""
    import inspect

    from app.services import runtime_propagation

    src = inspect.getsource(runtime_propagation)
    assert "OMP_READY_SIGNALS" in src
    assert '("╭─", "❯")' not in src, (
        "runtime_propagation.py must not hardcode a second, driftable glyph tuple"
    )


def test_agent_runtime_switch_uses_shared_tuple_at_its_call_site():
    import inspect

    from app.services import agent_runtime_switch

    src = inspect.getsource(agent_runtime_switch)
    assert "ready_signals=OMP_READY_SIGNALS if is_omp else None" in src
