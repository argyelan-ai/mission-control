"""Wave-0 stubs for MEM-04 — Qdrant payload index matrix.

Bodies land in plan 02-03. Today this xfails because
qdrant_service.ensure_payload_indexes() doesn't exist yet.

Pattern: AsyncMock + patch over qdrant_service._get_client (per
test_memory_indexing_gaps.py). 3-layer × 2-field matrix = 6 expected calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def _import_or_xfail():
    try:
        from app.services.qdrant_service import qdrant_service
        return qdrant_service
    except ImportError as e:
        pytest.xfail(f"Plan 02-03 implements MEM-04 changes: {e}")


@pytest.mark.asyncio
async def test_all_three_layers_have_both_indexes():
    """All 3 collections have BOTH agent_id and board_id keyword indexes after
    ensure_payload_indexes() runs against an existing-collections fixture."""
    svc = _import_or_xfail()
    if not hasattr(svc, "ensure_payload_indexes"):
        pytest.xfail("Plan 02-03: ensure_payload_indexes() not yet implemented")

    mock_client = AsyncMock()
    # Pretend all three collections already exist (Pitfall 2 case — pre-existing
    # collections that never received the per-layer index code).
    mock_client.get_collections.return_value = type("X", (), {"collections": [
        type("C", (), {"name": "memory_semantic"})(),
        type("C", (), {"name": "memory_agent"})(),
        type("C", (), {"name": "memory_episodic"})(),
    ]})()

    with patch.object(svc, "_get_client",
                      new=AsyncMock(return_value=mock_client)):
        # Force the fast-path so ensure_collections() short-circuits on
        # _collections_ready.
        svc._collections_ready = True
        await svc.ensure_payload_indexes()

    # Six (collection, field) tuples must be requested.
    calls = mock_client.create_payload_index.await_args_list
    pairs = {(c.kwargs["collection_name"], c.kwargs["field_name"]) for c in calls}
    expected = {
        ("memory_semantic", "agent_id"), ("memory_semantic", "board_id"),
        ("memory_agent",    "agent_id"), ("memory_agent",    "board_id"),
        ("memory_episodic", "agent_id"), ("memory_episodic", "board_id"),
    }
    assert pairs == expected, f"missing index pairs: {expected - pairs}"
