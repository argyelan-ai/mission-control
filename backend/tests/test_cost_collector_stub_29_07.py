"""Phase 29-07 Task 1: cost_collector.collect_session_costs is stubbed.

After Gateway sunset (D-11), session-based cost extraction via rpc.sessions_list
is impossible. collect_session_costs() is stubbed to a no-op returning the
zero-stats dict. Real cli-bridge cost extraction is deferred to Phase 31.
"""
from __future__ import annotations

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession


@pytest.mark.asyncio
async def test_collect_session_costs_returns_zero_stats(session: AsyncSession) -> None:
    """Stubbed collect_session_costs always returns zero stats — no RPC call."""
    from app.services.cost_collector import collect_session_costs

    stats = await collect_session_costs(session)

    assert stats == {"collected": 0, "events_created": 0, "errors": 0}


def test_cost_collector_has_no_rpc_imports() -> None:
    """cost_collector.py must not import openclaw_rpc after refactor."""
    import pathlib

    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "cost_collector.py"
    ).read_text(encoding="utf-8")

    assert "openclaw_rpc" not in src, "cost_collector still imports openclaw_rpc"
    assert "rpc." not in src, "cost_collector still calls rpc.*"
