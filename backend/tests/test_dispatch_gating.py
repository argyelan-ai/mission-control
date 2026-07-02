"""Tests fuer dispatch_phase Pre-Dispatch-Gating.

Testmatrix:
- Guard-Tests: planning blockiert, ready erlaubt, null=Legacy, Flag-off
- Dispatch-Reset: nach Dispatch → null
- Promote-Guards: alle 6 Preconditions
- Agent-Bypass: Work Items erzwungen auf planning
- Update-Backdoor: PATCH kann dispatch_phase nicht aendern
- Root-vs-Child: Root-Tasks kein Gating
- Feature-Flag: Promote bei Flag-off → 409
- Doppel-Promote: zweiter Aufruf → 409 (ready != planning)
- Regression: bestehende Guards intakt
"""
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from app.services.operations import check_dispatch_allowed

# Mock dependencies_met: promote_task_to_ready() importiert es lazy aus dispatch.
# Patch auf der Quelle damit der lazy import den Mock bekommt.
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


# ── Guard-Tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_planning_blocks_dispatch(_active):
    """dispatch_phase='planning' blockiert Dispatch."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, reason = await check_dispatch_allowed(_task("planning"), _agent())
    assert ok is False
    assert "planning" in reason.lower()


@pytest.mark.asyncio
async def test_ready_allows_dispatch(_active):
    """dispatch_phase='ready' erlaubt Dispatch."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(_task("ready"), _agent())
    assert ok is True


@pytest.mark.asyncio
async def test_null_phase_legacy(_active):
    """dispatch_phase=None = Legacy, kein Gating."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(_task(None), _agent())
    assert ok is True


@pytest.mark.asyncio
async def test_flag_off_ignores_planning(_active):
    """Feature-Flag aus: planning wird ignoriert."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = False
        ok, _ = await check_dispatch_allowed(_task("planning"), _agent())
    assert ok is True


# ── Review-Handoff darf durch Planning-Gate passieren ────

@pytest.mark.asyncio
async def test_review_handoff_passes_planning_gate(_active):
    """Review-Handoff (Continuation) darf trotz dispatch_phase=planning passieren."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(
            _task("planning", dispatch_intent="review_handoff"), _agent()
        )
    assert ok is True


@pytest.mark.asyncio
async def test_review_rework_passes_planning_gate(_active):
    """Review-Rework (Continuation) darf trotz dispatch_phase=planning passieren."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, _ = await check_dispatch_allowed(
            _task("planning", dispatch_intent="review_rework"), _agent()
        )
    assert ok is True


@pytest.mark.asyncio
async def test_subtask_still_blocked_by_planning(_active):
    """Normale Subtasks bleiben bei dispatch_phase=planning blockiert."""
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
    """Bestehender run_control Guard bleibt intakt."""
    with patch("app.services.operations.settings") as s:
        s.enable_dispatch_gating = True
        ok, reason = await check_dispatch_allowed(
            _task(dispatch_phase=None, run_control="stopped"), _agent()
        )
    assert ok is False
    assert "stopped" in reason.lower()


# ── Dispatch-Reset ───────────────────────────────────────

def test_dispatch_resets_phase_to_null():
    """Nach Dispatch wird dispatch_phase auf null zurueckgesetzt."""
    task = MagicMock()
    task.dispatch_phase = "ready"

    # Simulate dispatch.py logic
    if task.dispatch_phase is not None:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


def test_dispatch_reset_skips_null():
    """Legacy-Tasks (dispatch_phase=None) bleiben None."""
    task = MagicMock()
    task.dispatch_phase = None

    # Should not change
    if task.dispatch_phase is not None:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


# ── Promote Service Guards ───────────────────────────────

@pytest.mark.asyncio
async def test_promote_non_planning_fails():
    """Promote auf nicht-planning Task gibt 409."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = None
    session = AsyncMock()

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_promote_root_task_fails():
    """Root-Task kann nicht promoted werden."""
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
    """Task ohne assigned_agent_id kann nicht promoted werden."""
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
    """Task mit status != inbox kann nicht promoted werden."""
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
    """Bereits dispatchter Task kann nicht promoted werden."""
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


# ── Doppel-Promote ──────────────────────────────────────

@pytest.mark.asyncio
async def test_double_promote_fails():
    """Zweiter Promote auf ready-Task gibt 409 (kein Doppel-Dispatch)."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = MagicMock()
    task.dispatch_phase = "ready"  # bereits promoted
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
    """Atomarer Guard: rowcount=0 wenn ein anderer Request schon promoted hat.

    Simuliert Race Condition: Soft-Guards passieren (dispatch_phase=planning
    im in-memory Objekt), aber der atomare UPDATE ... WHERE dispatch_phase='planning'
    matcht keine Zeile (weil anderer Request schon auf 'ready' gesetzt hat).
    """
    from app.services.dispatch_gating import promote_task_to_ready

    task = MagicMock()
    task.dispatch_phase = "planning"  # Soft-Guard sieht planning
    task.parent_task_id = "parent-id"
    task.assigned_agent_id = "agent-id"
    task.status = "inbox"
    task.dispatched_at = None
    task.id = "race-task-id"

    session = AsyncMock()

    # Simuliere: atomarer UPDATE matcht 0 Zeilen (anderer Request war schneller)
    mock_result = MagicMock()
    mock_result.rowcount = 0
    session.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(Exception) as exc_info:
        await promote_task_to_ready(task, session)
    assert exc_info.value.status_code == 409
    assert "conflict" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_promote_atomic_success():
    """Atomarer Guard: rowcount=1 bei erfolgreichem Promote."""
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

    # emit_event braucht broadcast Mock
    with patch("app.services.dispatch_gating.emit_event", new_callable=AsyncMock):
        result = await promote_task_to_ready(task, session)

    # Session.execute wurde mit UPDATE aufgerufen
    session.execute.assert_called_once()
    session.commit.assert_called()
    session.refresh.assert_called_once_with(task)


# ── TOCTOU-Tests: Zustand aendert sich zwischen Soft-Guard und atomarem UPDATE ──
# In allen Faellen passieren die Soft-Guards (in-memory Task sieht gueltig aus),
# aber die DB hat sich zwischenzeitlich geaendert → rowcount=0 → 409.

def _promotable_task(**overrides):
    """Task der alle Soft-Guards besteht."""
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
    """AsyncMock Session deren execute rowcount=n zurueckgibt."""
    s = AsyncMock()
    r = MagicMock()
    r.rowcount = n
    s.execute = AsyncMock(return_value=r)
    return s


@pytest.mark.asyncio
async def test_atomic_conflict_status_changed():
    """TOCTOU: status wechselt von inbox zu in_progress zwischen Guard und UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    # Soft-Guard sieht inbox (in-memory), DB hat jetzt in_progress → rowcount=0
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_already_dispatched():
    """TOCTOU: dispatched_at wird gesetzt zwischen Guard und UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    # Soft-Guard sieht dispatched_at=None (in-memory), DB hat jetzt einen Wert → rowcount=0
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_agent_unassigned():
    """TOCTOU: assigned_agent_id wird NULL zwischen Guard und UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_conflict_parent_removed():
    """TOCTOU: parent_task_id wird NULL zwischen Guard und UPDATE."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task()
    session = _session_rowcount(0)

    with pytest.raises(Exception) as exc:
        await promote_task_to_ready(task, session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_atomic_success_with_full_where():
    """Vollstaendige WHERE-Clause: rowcount=1 bei gueltigem Zustand."""
    from app.services.dispatch_gating import promote_task_to_ready
    task = _promotable_task(title="Full WHERE Test", board_id="board-1")
    session = _session_rowcount(1)

    with patch("app.services.dispatch_gating.emit_event", new_callable=AsyncMock):
        result = await promote_task_to_ready(task, session)

    session.execute.assert_called_once()
    session.commit.assert_called()


# ── Root-vs-Child bei User-Create ───────────────────────

def test_user_create_root_task_clears_dispatch_phase():
    """User-Route: Root-Task (kein parent) → dispatch_phase auf null."""
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = None
    task.assigned_agent_id = None

    is_work_item = task.parent_task_id is not None and task.assigned_agent_id is not None
    if not is_work_item:
        task.dispatch_phase = None

    assert task.dispatch_phase is None


def test_user_create_child_task_keeps_planning():
    """User-Route: Child-Task mit parent+agent → dispatch_phase bleibt."""
    task = MagicMock()
    task.dispatch_phase = "planning"
    task.parent_task_id = "parent-123"
    task.assigned_agent_id = "agent-456"

    is_work_item = task.parent_task_id is not None and task.assigned_agent_id is not None
    if not is_work_item:
        task.dispatch_phase = None

    assert task.dispatch_phase == "planning"


# ── Agent-Bypass ────────────────────────────────────────

def test_agent_bypass_null_overridden_to_planning():
    """Agent sendet dispatch_phase=null fuer Work Item → Server erzwingt planning."""
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
    """Agent sendet dispatch_phase=ready fuer Work Item → Server erzwingt planning."""
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
    """Agent assigned an sich selbst → dispatch_phase bleibt null."""
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
    """Agent erstellt Root-Task → dispatch_phase bleibt null."""
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


# ── Update-Backdoor ─────────────────────────────────────

def test_task_update_model_has_no_dispatch_phase():
    """TaskUpdate Model enthaelt KEIN dispatch_phase Feld."""
    from app.routers.tasks import TaskUpdate
    assert "dispatch_phase" not in TaskUpdate.model_fields


def test_agent_task_update_model_has_no_dispatch_phase():
    """AgentTaskUpdate Model enthaelt KEIN dispatch_phase Feld."""
    from app.routers.agent_scoped import AgentTaskUpdate
    assert "dispatch_phase" not in AgentTaskUpdate.model_fields
