"""Tests fuer Boss Orchestrator Agent — Scopes, Dispatch-Prioritaet, CLI-Bridge."""
from app.scopes import AgentRole, DEFAULT_SCOPES, NON_WORKER_ROLES, ALL_SCOPES


def test_orchestrator_role_exists():
    assert AgentRole.ORCHESTRATOR == "orchestrator"


def test_orchestrator_has_all_scopes():
    scopes = DEFAULT_SCOPES[AgentRole.ORCHESTRATOR]
    assert set(scopes) == set(ALL_SCOPES)


def test_orchestrator_in_non_worker_roles():
    assert AgentRole.ORCHESTRATOR in NON_WORKER_ROLES


import uuid
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def _mk_agent(name="test", is_board_lead=False, role="developer", runtime="openclaw", gateway_id=None):
    a = MagicMock()
    a.id = uuid.uuid4()
    a.name = name
    a.is_board_lead = is_board_lead
    a.role = role
    a.agent_runtime = runtime
    a.assigned_agent_id = None
    return a


def _mk_task():
    t = MagicMock()
    t.assigned_agent_id = None
    t.board_id = uuid.uuid4()
    return t


@pytest.mark.asyncio
async def test_cli_bridge_agent_is_always_online():
    """CLI-Bridge Agents haben keine Gateway-Session → trotzdem als online gelten."""
    from app.services.dispatch import find_dispatch_target
    board_id = uuid.uuid4()
    boss = _mk_agent("boss", is_board_lead=True, role="orchestrator", runtime="cli-bridge")
    task = _mk_task()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_result = MagicMock()
    mock_result.all.return_value = [boss]
    mock_session.exec = AsyncMock(return_value=mock_result)

    if True:  # Phase 29: gateway rpc patch removed
        agent, reason = await find_dispatch_target(mock_session, task, board_id)

    assert agent is boss


@pytest.mark.asyncio
async def test_orchestrator_takes_priority_over_gateway_board_lead():
    """ORCHESTRATOR (CLI-bridge) hat Prioritaet ueber is_board_lead Gateway-Agent."""
    from app.services.dispatch import find_dispatch_target
    board_id = uuid.uuid4()
    boss = _mk_agent("boss", is_board_lead=True, role="orchestrator", runtime="cli-bridge")
    henry = _mk_agent("henry", is_board_lead=False, role="lead", runtime="openclaw")
    task = _mk_task()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_result = MagicMock()
    mock_result.all.return_value = [boss, henry]
    mock_session.exec = AsyncMock(return_value=mock_result)

    if True:  # Phase 29: gateway rpc patch removed
        agent, reason = await find_dispatch_target(mock_session, task, board_id)

    assert agent is boss
    assert "orchestrator" in reason


@pytest.mark.skip(reason="Planner-Pfad seit 2026-04-11 (Phase 6) komplett entfernt — Boss plant selbst")
@pytest.mark.asyncio
async def test_orchestrator_not_selected_as_planning_agent():
    """Obsolete: _find_planning_agent existiert nicht mehr."""
    pass


@pytest.mark.asyncio
async def test_host_runtime_orchestrator_wins_over_gateway_worker():
    """Regression (ADR-014): Boss mit agent_runtime=host MUSS als Orchestrator
    gewählt werden, auch ohne gateway_agent_id. Vor dem Fix fiel host durch den
    _is_online-Check und ein Gateway-Worker (z.B. Sparky) wurde stattdessen genommen.
    """
    from app.services.dispatch import find_dispatch_target
    board_id = uuid.uuid4()
    boss = _mk_agent("boss", is_board_lead=True, role="orchestrator", runtime="host")
    sparky = _mk_agent("sparky", is_board_lead=False, role="developer", runtime="cli-bridge")
    task = _mk_task()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_result = MagicMock()
    mock_result.all.return_value = [sparky, boss]  # Sparky zuerst → Fallback-Risiko
    mock_session.exec = AsyncMock(return_value=mock_result)

    if True:  # Phase 29: gateway rpc patch removed
        agent, reason = await find_dispatch_target(mock_session, task, board_id)

    assert agent is boss, f"Erwartet Boss (host+orchestrator), bekommen: {agent.name} ({reason})"
    assert "orchestrator" in reason


@pytest.mark.asyncio
async def test_host_runtime_board_lead_without_orchestrator_role():
    """Edge case: host-Agent ist nur Board Lead (nicht Orchestrator). Muss trotzdem
    über den board_lead-Pfad gefunden werden, auch ohne gateway_agent_id."""
    from app.services.dispatch import find_dispatch_target
    board_id = uuid.uuid4()
    host_lead = _mk_agent("host_lead", is_board_lead=True, role="lead", runtime="host")
    worker = _mk_agent("worker", is_board_lead=False, role="developer", runtime="cli-bridge")
    task = _mk_task()

    mock_session = AsyncMock()
    mock_session.get = AsyncMock(return_value=None)
    mock_result = MagicMock()
    mock_result.all.return_value = [worker, host_lead]
    mock_session.exec = AsyncMock(return_value=mock_result)

    if True:  # Phase 29: gateway rpc patch removed
        agent, reason = await find_dispatch_target(mock_session, task, board_id)

    assert agent is host_lead
    assert reason == "board_lead"


@pytest.mark.asyncio
async def test_non_gateway_runtimes_constant_covers_host():
    """NON_GATEWAY_RUNTIMES ist die Single Source of Truth für Poll-basierte Runtimes."""
    from app.services.dispatch import NON_GATEWAY_RUNTIMES
    assert "host" in NON_GATEWAY_RUNTIMES
    assert "cli-bridge" in NON_GATEWAY_RUNTIMES
