"""Core hook plumbing for verticals (ADR-044), extension-point wave 2:

1. challenge_actions_providers / collect_challenge_actions — GET bench
   challenge detail includes actions contributed by registered providers;
   a raising provider is swallowed (actions list just omits it, 200 OK).
2. approval_resolved_hooks / run_approval_resolved_hooks — fires for
   approvals whose action_type has no dedicated core handler; x_post
   approvals must NOT also trigger this generic hook (they have their own
   x_post_resolved_hooks call inside _handle_x_post_resolution already).

Core-level tests: run in stripped installations too, except the challenge-
actions half which needs the bench_studio vertical present to have a
challenge to fetch.
"""
import uuid
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.approval import Approval
from app.models.board import Board
from app.models.bench import BenchChallenge, BenchEntry
from app.verticals import hooks as vertical_hooks
from app.verticals.bench_studio import orchestrator


@pytest.fixture(autouse=True)
def _clean_hook_registries():
    saved_actions = list(vertical_hooks.challenge_actions_providers)
    saved_approval = list(vertical_hooks.approval_resolved_hooks)
    yield
    vertical_hooks.challenge_actions_providers[:] = saved_actions
    vertical_hooks.approval_resolved_hooks[:] = saved_approval


async def _seed_challenge(session):
    ch = BenchChallenge(title="T", prompt_text="p", status="review")
    session.add(ch)
    await session.commit()
    await session.refresh(ch)
    session.add(BenchEntry(challenge_id=ch.id, model_label="A",
                            source_kind="spark", status="rendered",
                            video_path="/sd/a.mp4"))
    await session.commit()
    return ch


@pytest.mark.asyncio
async def test_challenge_detail_includes_registered_provider_action(
    auth_client, session, monkeypatch,
):
    ch = await _seed_challenge(session)
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())

    async def provider(sess, challenge, entries):
        return [{
            "id": "catalog-publish", "label": "Publish", "style": "primary",
            "method": "POST", "endpoint": f"/api/v1/catalog/{challenge.id}/publish",
            "confirm": None, "disabled": False, "disabled_reason": None, "busy": False,
        }]

    vertical_hooks.challenge_actions_providers.append(provider)

    resp = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")
    assert resp.status_code == 200, resp.text
    actions = resp.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["id"] == "catalog-publish"
    assert actions[0]["endpoint"] == f"/api/v1/catalog/{ch.id}/publish"


@pytest.mark.asyncio
async def test_challenge_detail_swallows_raising_provider(auth_client, session, monkeypatch):
    ch = await _seed_challenge(session)
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())

    async def bad_provider(sess, challenge, entries):
        raise RuntimeError("boom")

    vertical_hooks.challenge_actions_providers.append(bad_provider)

    resp = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["actions"] == []


@pytest.mark.asyncio
async def test_challenge_detail_no_providers_empty_actions(auth_client, session, monkeypatch):
    ch = await _seed_challenge(session)
    monkeypatch.setattr(orchestrator, "reconcile_challenge", AsyncMock())

    resp = await auth_client.get(f"/api/v1/bench/challenges/{ch.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["actions"] == []


async def _make_approval(session, action_type, status="pending"):
    board = Board(name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    session.add(board)
    await session.commit()
    await session.refresh(board)
    approval = Approval(
        board_id=board.id,
        action_type=action_type,
        description="test approval",
        payload={},
        status=status,
    )
    session.add(approval)
    await session.commit()
    await session.refresh(approval)
    return board, approval


@pytest.mark.asyncio
async def test_custom_approval_type_fires_generic_hook(auth_client, session):
    _, approval = await _make_approval(session, "catalog_publish")

    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status):
        seen.append((appr.id, appr.action_type, resolution_status))

    vertical_hooks.approval_resolved_hooks.append(hook)

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "approved"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [(approval.id, "catalog_publish", "approved")]


@pytest.mark.asyncio
async def test_custom_approval_type_rejected_fires_generic_hook_with_rejected(auth_client, session):
    """Rejection must fire the hook too, not just approval (F9, review
    finding) — resolution_status must carry "rejected", not "approved"."""
    _, approval = await _make_approval(session, "catalog_publish")

    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status):
        seen.append((appr.id, resolution_status))

    vertical_hooks.approval_resolved_hooks.append(hook)

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "rejected"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == [(approval.id, "rejected")]


@pytest.mark.asyncio
async def test_x_post_approval_does_not_fire_generic_hook(auth_client, session):
    """x_post has its own x_post_resolved_hooks call inside
    _handle_x_post_resolution already — the generic approval_resolved_hooks
    must not ALSO fire for it (would double-notify overlay verticals)."""
    _, approval = await _make_approval(session, "x_post")

    seen: list[tuple] = []

    async def hook(sess, appr, resolution_status):
        seen.append((appr.id, resolution_status))

    vertical_hooks.approval_resolved_hooks.append(hook)

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}",
        json={"status": "rejected"},
    )
    assert resp.status_code == 200, resp.text
    assert seen == []


# ── F7 (review finding): _CORE_HANDLED_ACTION_TYPES must stay in sync with
# the actual inline `approval.action_type == "..."` / `in {...}` branches in
# approvals.py — a drift there means either a real handler silently ALSO
# gets the generic hook (double-fire), or a newly-added handler is missing
# from the set and its approvals wrongly skip their intended core behavior
# path assumptions elsewhere. Parses the source rather than hand-duplicating
# the list, so it actually catches future drift instead of just re-asserting
# today's snapshot. ──────────────────────────────────────────────────────


def test_core_handled_action_types_matches_inline_handlers():
    import re
    from pathlib import Path

    from app.routers import approvals as approvals_module
    from app.routers.approvals import _CORE_HANDLED_ACTION_TYPES

    source = Path(approvals_module.__file__).read_text()

    # Every inline `approval.action_type == "x"` branch.
    eq_types = set(re.findall(r'approval\.action_type == "([a-z_]+)"', source))

    # The one inline `approval.action_type in {...}` set (install/uninstall).
    in_block_match = re.search(r"approval\.action_type in \{([^}]+)\}", source, re.DOTALL)
    assert in_block_match, "expected an `approval.action_type in {...}` set literal in approvals.py"
    in_block_types = set(re.findall(r'"([a-z_]+)"', in_block_match.group(1)))

    inline_handled = eq_types | in_block_types
    assert inline_handled == _CORE_HANDLED_ACTION_TYPES, (
        "approvals.py's inline action_type branches and _CORE_HANDLED_ACTION_TYPES "
        f"have drifted apart — only in inline code: {inline_handled - _CORE_HANDLED_ACTION_TYPES}, "
        f"only in the set: {_CORE_HANDLED_ACTION_TYPES - inline_handled}"
    )
