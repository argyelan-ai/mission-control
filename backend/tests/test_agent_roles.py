"""Tests for AgentRole enum, get_default_scopes(), Agent.role validator, find_agent_by_role()."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.scopes import AgentRole, DEFAULT_SCOPES, get_default_scopes, ALL_SCOPES, WORKER_ROLES, NON_WORKER_ROLES


# ── AgentRole Enum Tests ──────────────────────────────────────────────


def test_agent_role_values():
    """All 10 roles are defined."""
    assert set(AgentRole) == {
        AgentRole.LEAD, AgentRole.DEVELOPER, AgentRole.REVIEWER,
        AgentRole.TESTER, AgentRole.PLANNER, AgentRole.RESEARCHER,
        AgentRole.DEPLOYER, AgentRole.WRITER, AgentRole.ORCHESTRATOR,
        AgentRole.RELAY,
    }


def test_agent_role_matches_default_scopes_keys():
    """Every AgentRole has an entry in DEFAULT_SCOPES."""
    for role in AgentRole:
        assert role in DEFAULT_SCOPES, f"AgentRole.{role.name} fehlt in DEFAULT_SCOPES"


def test_worker_roles():
    assert WORKER_ROLES == frozenset({AgentRole.DEVELOPER, AgentRole.DEPLOYER})


def test_non_worker_roles():
    assert NON_WORKER_ROLES == frozenset({AgentRole.PLANNER, AgentRole.RESEARCHER, AgentRole.WRITER, AgentRole.ORCHESTRATOR})


# ── get_default_scopes() Tests ────────────────────────────────────────


def test_get_default_scopes_with_enum():
    """Lookup with AgentRole enum works."""
    scopes = get_default_scopes(AgentRole.DEVELOPER)
    assert "tasks:read" in scopes
    assert "tasks:write" in scopes


def test_get_default_scopes_with_string():
    """Legacy: lookup with string still works."""
    scopes = get_default_scopes("developer")
    assert "tasks:read" in scopes


def test_get_default_scopes_with_uppercase_string():
    """Legacy: case-insensitive string lookup."""
    scopes = get_default_scopes("REVIEWER")
    assert "tasks:read" in scopes


def test_get_default_scopes_unknown_returns_all():
    """Unknown template name → ALL_SCOPES."""
    scopes = get_default_scopes("unknown_role")
    assert scopes == list(ALL_SCOPES)


def test_get_default_scopes_lead_has_all():
    """Lead has all scopes."""
    scopes = get_default_scopes(AgentRole.LEAD)
    assert scopes == list(ALL_SCOPES)


# ── Agent.role Validator Tests ────────────────────────────────────────


def test_agent_role_validator_valid():
    from app.models.agent import Agent
    agent = Agent(name="TestAgent", role="developer")
    assert agent.role == "developer"


def test_agent_role_validator_none():
    from app.models.agent import Agent
    agent = Agent(name="TestAgent", role=None)
    assert agent.role is None


def test_agent_role_validator_invalid():
    from app.models.agent import Agent
    # SQLModel table=True uses model_validate for Pydantic validation
    with pytest.raises(Exception):
        Agent.model_validate({"name": "TestAgent", "role": "hacker"})


def test_agent_role_validator_all_roles():
    from app.models.agent import Agent
    for role in AgentRole:
        agent = Agent(name="Test", role=role.value)
        assert agent.role == role.value


# ── find_agent_by_role() Tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_agent_by_role_finds_correct_role(session, make_board, make_agent):
    """Finds agent with matching role."""
    board = await make_board()
    reviewer = await make_agent(
        name="Rex", role="reviewer", board_id=board.id,     )
    developer = await make_agent(
        name="Cody", role="developer", board_id=board.id,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == reviewer.id


@pytest.mark.asyncio
async def test_find_agent_by_role_fallback_to_lead(session, make_board, make_agent):
    """Falls back to board lead when no role is found."""
    board = await make_board()
    lead = await make_agent(
        name="Henry", role="lead", board_id=board.id,
        is_board_lead=True,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == lead.id


@pytest.mark.asyncio
async def test_find_agent_by_role_exclude(session, make_board, make_agent):
    """Exclude filter works."""
    board = await make_board()
    reviewer = await make_agent(
        name="Rex", role="reviewer", board_id=board.id,     )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(
        session, board.id, AgentRole.REVIEWER, exclude_agent_id=reviewer.id,
    )
    # No other reviewer, no board lead → None
    assert result is None


@pytest.mark.asyncio
async def test_find_agent_by_role_least_busy(session, make_board, make_agent, make_task):
    """With multiple candidates: agent with fewer active tasks is preferred."""
    board = await make_board()
    dev1 = await make_agent(
        name="Dev1", role="developer", board_id=board.id,     )
    dev2 = await make_agent(
        name="Dev2", role="developer", board_id=board.id,     )

    # dev1 has 2 active tasks, dev2 has 0
    await make_task(board_id=board.id, status="in_progress", assigned_agent_id=dev1.id)
    await make_task(board_id=board.id, status="in_progress", assigned_agent_id=dev1.id)

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.DEVELOPER)
    assert result is not None
    assert result.id == dev2.id


@pytest.mark.asyncio
async def test_find_agent_by_role_counts_held_review_cards(
    session, make_board, make_agent, make_task,
):
    """Gehaltene review-Karten zaehlen in die Last — ein Reviewer mit 5
    wartenden Reviews und 0 Zuegen wird NICHT gewaehlt."""
    board = await make_board()
    busy = await make_agent(name="Busy Rex", role="reviewer", board_id=board.id)
    free = await make_agent(name="Free Rex", role="reviewer", board_id=board.id)

    # Busy haelt 5 Reviews, faehrt aber 0 Zuege (kein in_progress).
    for i in range(5):
        await make_task(
            board_id=board.id, title=f"Review {i}",
            status="review", assigned_agent_id=busy.id,
        )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == free.id


@pytest.mark.asyncio
async def test_find_agent_by_role_skips_offline_reviewer(session, make_board, make_agent):
    """Offline-Reviewer (last_seen_at stale) wird nicht gewaehlt."""
    from datetime import timedelta
    from app.utils import utcnow

    board = await make_board()
    offline = await make_agent(
        name="Offline Rex", role="reviewer", board_id=board.id,
        last_seen_at=utcnow() - timedelta(hours=1),
    )
    online = await make_agent(
        name="Online Rex", role="reviewer", board_id=board.id,
        last_seen_at=utcnow(),
    )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is not None
    assert result.id == online.id
    assert result.id != offline.id


@pytest.mark.asyncio
async def test_find_agent_by_role_none_when_only_offline_reviewer(
    session, make_board, make_agent,
):
    """Nur offline Reviewer, kein Lead → explizit None (kein stiller Fallback)."""
    from datetime import timedelta
    from app.utils import utcnow

    board = await make_board()
    await make_agent(
        name="Offline Rex", role="reviewer", board_id=board.id,
        last_seen_at=utcnow() - timedelta(hours=1),
    )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is None


@pytest.mark.asyncio
async def test_find_agent_by_role_excludes_author(session, make_board, make_agent):
    """Autor der Karte (exclude_agent_id) wird nicht gewaehlt — auch nicht
    als einziger Kandidat; kein stiller Fallback auf ihn."""
    board = await make_board()
    author = await make_agent(name="Author Rex", role="reviewer", board_id=board.id)

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(
        session, board.id, AgentRole.REVIEWER, exclude_agent_id=author.id,
    )
    assert result is None


@pytest.mark.asyncio
async def test_handle_review_handoff_never_selects_author(
    make_board, make_agent, make_task,
):
    """End-to-End (FreeCode-Bug): Autor des PR == einziger Reviewer →
    handle_review_handoff liefert None und weist die Karte NICHT zu."""
    board = await make_board(name="Author Board", slug="author-board")
    developer = await make_agent(
        name="FreeCode", board_id=board.id, role="reviewer", is_board_lead=False,
    )
    task = await make_task(
        board_id=board.id, title="PR Review",
        status="review", assigned_agent_id=developer.id,
    )

    from tests.conftest import test_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    with (
        patch("app.services.activity.broadcast", new_callable=AsyncMock),
        patch("app.services.operations.get_system_mode", new_callable=AsyncMock, return_value="active"),
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            from app.services.task_lifecycle import handle_review_handoff
            result = await handle_review_handoff(s, t, board.id, developer=developer)

        assert result is None
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            t = await s.get(type(task), task.id)
            assert t.assigned_agent_id == developer.id  # unveraendert, kein Self-Review
            assert t.dispatch_intent != "review_handoff"


# ── _find_reviewer() Role-Based Tests ────────────────────────────────


@pytest.mark.asyncio
async def test_find_reviewer_by_role(session, make_board, make_agent):
    """Reviewer is found by role='reviewer', not by name."""
    board = await make_board()
    reviewer = await make_agent(
        name="Agent007", role="reviewer", board_id=board.id,     )

    from app.routers.agent_scoped import _find_reviewer
    result = await _find_reviewer(session, board.id)
    assert result is not None
    assert result.id == reviewer.id


@pytest.mark.asyncio
async def test_find_reviewer_legacy_fallback(session, make_board, make_agent):
    """Legacy: agent with 'rex' in its name is found when role=None."""
    board = await make_board()
    rex = await make_agent(
        name="Rex", role=None, board_id=board.id,     )

    from app.routers.agent_scoped import _find_reviewer
    result = await _find_reviewer(session, board.id)
    assert result is not None
    assert result.id == rex.id


# ── Blocker 2: Autor-Ausschluss auf der Board-Lead-Stufe ─────────────


@pytest.mark.asyncio
async def test_find_agent_by_role_excludes_author_on_board_lead_fallback(
    session, make_board, make_agent,
):
    """Blocker 2 (Rex, PR #148): Autor-Ausschluss gilt auch auf der
    Board-Lead-Stufe. Kein Role-Kandidat, der Autor IST der Board Lead →
    explizit None, nicht der Autor selbst."""
    board = await make_board()
    author = await make_agent(
        name="Author Lead", role="developer", board_id=board.id,
        is_board_lead=True,
    )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(
        session, board.id, AgentRole.REVIEWER, exclude_agent_id=author.id,
    )
    assert result is None, (
        "Autor (hier: Board Lead) darf nicht als eigener Reviewer fallback-selected werden"
    )


@pytest.mark.asyncio
async def test_find_agent_by_role_lead_fallback_selects_other_lead(
    session, make_board, make_agent,
):
    """Gegenprobe: ohne Ausschluss liefert die Lead-Stufe den (fremden)
    Board Lead — die Stufe selbst funktioniert, nur der Autor wird
    ausgeschlossen."""
    board = await make_board()
    author = await make_agent(
        name="Author", role="developer", board_id=board.id,
        is_board_lead=True,
    )
    other_lead = await make_agent(
        name="Second Lead", role="developer", board_id=board.id,
        is_board_lead=True,
    )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(
        session, board.id, AgentRole.REVIEWER, exclude_agent_id=author.id,
    )
    assert result is not None
    assert result.id == other_lead.id


@pytest.mark.asyncio
async def test_find_agent_by_role_lead_fallback_skips_offline_lead(
    session, make_board, make_agent,
):
    """Liveness gilt auch auf der Lead-Stufe: ein Lead mit stale
    last_seen_at wird nicht geliefert (toter Lead → explizit None)."""
    from datetime import timedelta
    from app.utils import utcnow

    board = await make_board()
    await make_agent(
        name="Dead Lead", role="developer", board_id=board.id,
        is_board_lead=True, last_seen_at=utcnow() - timedelta(hours=1),
    )

    from app.services.dispatch import find_agent_by_role
    result = await find_agent_by_role(session, board.id, AgentRole.REVIEWER)
    assert result is None


# ── Blocker 3: role_count-Guard in work_context.find_reviewer ────────


@pytest.mark.asyncio
async def test_find_reviewer_no_legacy_fallback_when_reviewer_role_exists(
    session, make_board, make_agent,
):
    """Blocker 3 (Rex, PR #148): Existiert mind. ein Agent mit
    role='reviewer', darf der Name-basierte Legacy-Fallback NICHT feuern —
    auch nicht, wenn der Role-Kandidat eliminiert wurde (hier: Autor-
    Ausschluss). Ohne den Guard wuerde 'Rex reviewer stand-in' (role=None)
    die Karte bekommen."""
    board = await make_board()
    author = await make_agent(name="Rex", role="reviewer", board_id=board.id)
    # Legacy-Kandidat: 'rex' im Namen, role=None — wuerde den Guard umgehen
    await make_agent(name="Rex reviewer stand-in", role=None, board_id=board.id)

    from app.services.work_context import find_reviewer
    result = await find_reviewer(session, board.id, exclude_agent_id=author.id)
    assert result is None, (
        "role_count > 0 muss zum Abbruch fuehren — der Legacy-Name-Fallback "
        "darf eliminierte Role-Reviewer nicht wieder einwechseln"
    )


@pytest.mark.asyncio
async def test_find_reviewer_legacy_fallback_still_fires_without_role_reviewer(
    session, make_board, make_agent,
):
    """Gegenprobe zum Guard: kein einziger Agent mit role='reviewer' auf
    dem Board → Legacy-Name-Fallback greift weiterhin (Vorfall 94fda9f9)."""
    board = await make_board()
    rex = await make_agent(name="Rex", role=None, board_id=board.id)
    author = await make_agent(name="Cody", role="developer", board_id=board.id)

    from app.services.work_context import find_reviewer
    result = await find_reviewer(session, board.id, exclude_agent_id=author.id)
    assert result is not None
    assert result.id == rex.id
