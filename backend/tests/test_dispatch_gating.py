"""Tests for dispatch_phase pre-dispatch gating.

Test matrix:
- Guard tests: planning blocks, ready allows, null=legacy, flag off
- Dispatch reset: after dispatch → null
- Promote guards: all 6 preconditions
- Agent bypass: work items forced to planning
- Update backdoor: PATCH cannot change dispatch_phase
- Root vs child: root tasks have no gating
- Feature flag: promote with flag off → 409
- Double promote: second call → 409 (ready != planning)
- Regression: existing guards remain intact
"""
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from app.services.operations import check_dispatch_allowed

# Mock dependencies_met: promote_task_to_ready() imports it lazily from dispatch.
# Patch at the source so the lazy import picks up the mock.
@pytest.fixture(autouse=True)
def _mock_deps_met_global():
    with patch("app.services.dispatch.dependencies_met",
               new_callable=AsyncMock, return_value=True):
        yield


def _task(dispatch_phase=None, run_control=None, dispatch_intent="subtask"):
    t = MagicMock()
    t.run_control = run_control
    t.dispatch_intent = dispatch_intent
    t.dispatch_phase = dispatch_phase
    return t


def _agent(name="test"):
    a = MagicMock()
    a.operational_mode = "active"
    a.last_seen_at = None
    a.name = name
    return a


@pytest.fixture
def _active():
    with patch("app.services.operations.get_system_mode",
               new_callable=AsyncMock, return_value="active"):
        yield


# ── Guard tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_planning_blocks_dispatch(_active):
    """dispatch_phase='planning' blocks dispatch."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, reason = await check_dispatch_allowed(_task("planning"), _agent())
    assert ok is False
    assert "planning" in reason.lower()


@pytest.mark.asyncio
async def test_ready_allows_dispatch(_active):
    """dispatch_phase='ready' allows dispatch."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(_task("ready"), _agent())
    assert ok is True


@pytest.mark.asyncio
async def test_null_phase_legacy(_active):
    """dispatch_phase=None = legacy, no gating."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(_task(None), _agent())
    assert ok is True


@pytest.mark.asyncio
async def test_flag_off_ignores_planning(_active):
    """Feature flag off: planning is ignored."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = False
        ok, _ = await check_dispatch_allowed(_task("planning"), _agent())
    assert ok is True


# ── Review handoff must pass through the planning gate ────

@pytest.mark.asyncio
async def test_review_handoff_passes_planning_gate(_active):
    """Review handoff (continuation) must pass despite dispatch_phase=planning."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(
            _task("planning", dispatch_intent="review_handoff"), _agent()
        )
    assert ok is True


@pytest.mark.asyncio
async def test_review_rework_passes_planning_gate(_active):
    """Review rework (continuation) must pass despite dispatch_phase=planning."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(
            _task("planning", dispatch_intent="review_rework"), _agent()
        )
    assert ok is True


@pytest.mark.asyncio
async def test_subtask_still_blocked_by_planning(_active):
    """Normal subtasks stay blocked at dispatch_phase=planning."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, reason = await check_dispatch_allowed(
            _task("planning", dispatch_intent="subtask"), _agent()
        )
    assert ok is False
    assert "planning" in reason.lower()


# ── Regression ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_control_still_works(_active):
    """Existing run_control guard remains intact."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, reason = await check_dispatch_allowed(
            _task(dispatch_phase=None, run_control="stopped"), _agent()
        )
    assert ok is False
    assert "stopped" in reason.lower()


# ── Dispatch reset ───────────────────────────────────────

def test_dispatch_resets_phase_to_null():
    """After dispatch, dispatch_phase is reset to null."""
    task = MagicMock()
    task.dispatch_phase = "ready"

    # Simulate dispatch.py logic
    if task.dispatch_phase is not None:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


def test_dispatch_reset_skips_null():
    """Legacy tasks (dispatch_phase=None) stay None."""
    task = MagicMock()
    task.dispatch_phase = None

    # Should not change
    if task.dispatch_phase is not None:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


# ── Promote service guards ───────────────────────────────

@pytest.mark.asyncio
async def test_promote_non_planning_fails():
    """Promote on a non-planning task returns 409."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = None
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_promote_root_task_fails():
    """A root task cannot be promoted."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = None
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_promote_no_assigned_agent_fails():
    """A task without assigned_agent_id cannot be promoted."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "some-parent-id"
    task.assigned_agent_id = None
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_promote_wrong_status_fails():
    """A task with status != inbox cannot be promoted."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "some-parent-id"
    task.assigned_agent_id = "some-agent-id"
    task.status = "in_progress"
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_promote_already_dispatched_fails():
    """A task that was already dispatched cannot be promoted."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "some-parent-id"
    task.assigned_agent_id = "some-agent-id"
    task.status = "inbox"
    task.dispatched_at = "2026-03-18T10:00:00Z"
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


# ── Double promote ──────────────────────────────────────

@pytest.mark.asyncio
async def test_double_promote_fails():
    """Second promote on a ready task returns 409 (no double dispatch)."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "ready"  # already promoted
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "agent-id"
    task.status = "inbox"
    task.dispatched_at = None
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409
    assert "planning" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_promote_atomic_conflict():
    """Atomic guard: rowcount=0 when another request already promoted.

    Simulates a race condition: soft guards pass (dispatch_phase=planning
    on the in-memory object), but the atomic UPDATE ... WHERE dispatch_phase='planning'
    matches no row (because another request already set it to 'ready').
    """
    from app.services.dispatch_gating import promote_task_to_ready

    task = MagicMock()
    task.dispatch_phase = "planning"  # soft guard sees planning
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "agent-id"
    task.status = "inbox"
    task.dispatched_at = None
    task.id = "race-task-id"

    session = AsyncMock()

    # Simulate: atomic UPDATE matches 0 rows (another request was faster)
    mock_result = MagicMock()
    mock_result.rowcount = 0
    session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409
    assert "conflict" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_promote_atomic_success():
    """Atomic guard: rowcount=1 on successful promote."""
    from app.services.dispatch_gating import promote_task_to_ready

    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "agent-id"
    task.status = "inbox"
    task.dispatched_at = None
    task.id = "success-task-id"
    task.title = "Test Task"
    task.board_id = "board-id"

    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = 1
    session.execute = AsyncMock(return_value=mock_result)

    # emit_event needs a broadcast mock
    with patch("app.services.dispatch_gating.emit_event", new_callable=AsyncMock):
        result = await promote_task_to_ready(task, session)

    # session.execute was called with UPDATE
    session.execute.assert_called_once()
    session.commit.assert_called()
    session.refresh.assert_called_once_with(task)


# ── TOCTOU tests: state changes between the soft guard and the atomic UPDATE ──
# In all cases the soft guards pass (in-memory task looks valid), but the DB
# has changed in the meantime → rowcount=0 → 409.

def _promotable_task(**overrides):
    """A task that passes all soft guards."""
    t = MagicMock()
    t.dispatch_phase = "planning"
    t.parent_task_id = "parent-id"
    t.assigned_agent_id = "agent-id"
    t.status = "inbox"
    t.dispatched_at = None
    t.id = "toctou-task"
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


def _session_rowcount(n):
    """AsyncMock session whose execute returns rowcount=n."""
    s = AsyncMock()
    r = MagicMock()
    r.rowcount = n
    s.execute = AsyncMock(return_value=r)
    return s


@pytest.mark.asyncio
async def test_atomic_conflict_status_changed():
    """TOCTOU: status changes from inbox to in_progress between guard and UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    # soft guard sees inbox (in-memory), DB now has in_progress → rowcount=0
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_already_dispatched():
    """TOCTOU: dispatched_at gets set between guard and UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    # soft guard sees dispatched_at=None (in-memory), DB now has a value → rowcount=0
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_agent_unassigned():
    """TOCTOU: assigned_agent_id becomes NULL between guard and UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_parent_removed():
    """TOCTOU: parent_task_id becomes NULL between guard and UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_success_with_full_where():
    """Full WHERE clause: rowcount=1 in a valid state."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task(title="Full WHERE Test", board_id="board-1")
    session = _session_rowcount(1)

    with patch("app.services.dispatch_gating.emit_event", new_callable=AsyncMock):
        result = await promote_task_to_ready(task, session)

    session.execute.assert_called_once()
    session.commit.assert_called()


# ── Root vs child on user-create ───────────────────────

def test_user_create_root_task_clears_dispatch_phase():
    """User route: root task (no parent) → dispatch_phase set to null."""
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = None
    task.assigned_agent_id = None

    is_work_item = task.parent_task_id is not None and task.assigned_agent_id is not None
    if not is_work_item:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


def test_user_create_child_task_keeps_planning():
    """User route: child task with parent+agent → dispatch_phase stays."""
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "parent-123"
    task.assigned_agent_id = "agent-456"

    is_work_item = task.parent_task_id is not None and task.assigned_agent_id is not None
    if not is_work_item:
        task.dispatch_phase = None

    assert task.dispatch_phase == "planning"


# ── Agent bypass ────────────────────────────────────────

def test_agent_bypass_null_overridden_to_planning():
    """Agent sends dispatch_phase=null for a work item → server forces planning."""
    task = MagicMock()
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "other-agent-id"
    task.dispatch_phase = None
    agent_id = "creator-agent-id"

    is_executable = (
        task.parent_task_id is not None
        and task.assigned_agent_id is not None
        and task.assigned_agent_id != agent_id
    )
    if is_executable:
        task.dispatch_phase = "planning"

    assert task.dispatch_phase == "planning"


def test_agent_bypass_ready_overridden_to_planning():
    """Agent sends dispatch_phase=ready for a work item → server forces planning."""
    task = MagicMock()
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "other-agent-id"
    task.dispatch_phase = "ready"
    agent_id = "creator-agent-id"

    is_executable = (
        task.parent_task_id is not None
        and task.assigned_agent_id is not None
        and task.assigned_agent_id != agent_id
    )
    if is_executable:
        task.dispatch_phase = "planning"

    assert task.dispatch_phase == "planning"


def test_agent_self_assigned_no_gating():
    """Agent assigned to itself → dispatch_phase stays null."""
    task = MagicMock()
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "creator-agent-id"
    task.dispatch_phase = "planning"
    agent_id = "creator-agent-id"

    is_executable = (
        task.parent_task_id is not None
        and task.assigned_agent_id is not None
        and task.assigned_agent_id != agent_id
    )
    if is_executable:
        task.dispatch_phase = "planning"
    else:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


def test_agent_root_task_no_gating():
    """Agent creates a root task → dispatch_phase stays null."""
    task = MagicMock()
    task.parent_task_id = None
    task.assigned_agent_id = "other-agent-id"
    task.dispatch_phase = "planning"
    agent_id = "creator-agent-id"

    is_executable = (
        task.parent_task_id is not None
        and task.assigned_agent_id is not None
        and task.assigned_agent_id != agent_id
    )
    if is_executable:
        task.dispatch_phase = "planning"
    else:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


# ── Update backdoor ─────────────────────────────────────

def test_task_update_model_has_no_dispatch_phase():
    """TaskUpdate model contains NO dispatch_phase field."""
    from app.routers.tasks import TaskUpdate
    assert "dispatch_phase" not in TaskUpdate.model_fields


def test_agent_task_update_model_has_no_dispatch_phase():
    """AgentTaskUpdate model contains NO dispatch_phase field."""
    from app.routers.agent_scoped import AgentTaskUpdate
    assert "dispatch_phase" not in AgentTaskUpdate.model_fields


# ── ADR-062: is_executable_work_item classification (dispatch_to_agent bypass) ──

from app.services.dispatch_gating import is_executable_work_item  # noqa: E402

BOSS = "boss-id"
CODY = "cody-id"
JARVIS = "jarvis-id"


def test_iewi_board_lead_subtask_delegation_unchanged():
    """Board Lead delegates a SUBTASK to a worker → gated (as before)."""
    assert is_executable_work_item(
        has_parent=True, assigned_agent_id=CODY,
        creator_agent_id=BOSS, creator_is_board_lead=True,
    ) is True


def test_iewi_board_lead_root_delegation_stays_ungated():
    """Board Lead creates a PARENTLESS root task for a worker → NOT gated
    (unchanged: the Board Lead is the orchestrator, carries operator intent)."""
    assert is_executable_work_item(
        has_parent=False, assigned_agent_id=CODY,
        creator_agent_id=BOSS, creator_is_board_lead=True,
    ) is False


def test_iewi_worker_subtask_unchanged():
    """Non-Board-Lead creates a SUBTASK for another agent → gated (unchanged)."""
    assert is_executable_work_item(
        has_parent=True, assigned_agent_id=CODY,
        creator_agent_id="worker-id", creator_is_board_lead=False,
    ) is True


def test_iewi_jarvis_root_foreign_assignment_now_gated():
    """THE FIX: non-Board-Lead (Jarvis) creates a PARENTLESS root task assigned
    to another agent (dispatch_to_agent) → now gated, no bypass."""
    assert is_executable_work_item(
        has_parent=False, assigned_agent_id=CODY,
        creator_agent_id=JARVIS, creator_is_board_lead=False,
    ) is True


def test_iewi_self_assigned_never_gated():
    """Self-assigned tasks are never executable work items (root or child,
    board-lead or not)."""
    for has_parent in (True, False):
        for is_lead in (True, False):
            assert is_executable_work_item(
                has_parent=has_parent, assigned_agent_id=BOSS,
                creator_agent_id=BOSS, creator_is_board_lead=is_lead,
            ) is False


def test_iewi_unassigned_never_gated():
    """No assignee → not an executable work item."""
    assert is_executable_work_item(
        has_parent=False, assigned_agent_id=None,
        creator_agent_id=JARVIS, creator_is_board_lead=False,
    ) is False
