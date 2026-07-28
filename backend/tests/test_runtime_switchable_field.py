"""Runtime-switch eligibility is derived ONCE and shipped to the UI.

Background — the divergence this file exists to make impossible:

    The frontend re-implemented "may this agent switch runtime?" as
    ``agent_runtime === "host" && harness === "hermes"``, in two places. The
    backend meanwhile grew HOST_ADAPTERS entries for grok, kimi and claude.
    Every one of those host agents (Boss included) kept showing
    "RUNTIME LOCKED · HOST" in the UI although the switch endpoint would have
    accepted them — the UI was simply asking the wrong oracle.

The fix routes both the switch endpoint's guard (``_ensure_agent_switchable``)
and the API's derived ``runtime_switchable`` /
``runtime_switch_blocked_reason`` fields through ONE function:
``host_harness_adapter.runtime_switch_availability``.

The parametrised test below is the guard rail: it iterates HOST_ADAPTERS
itself, so registering a fifth host harness without it becoming switchable
end-to-end is not possible — the new key is tested the moment it is added.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.services.agent_runtime_switch import (
    AgentNotSwitchableError,
    _ensure_agent_switchable,
)
from app.services.host_harness_adapter import (
    HOST_ADAPTERS,
    is_host_inplace,
    runtime_switch_availability,
)

# A harness that must never be in HOST_ADAPTERS — the "adapter-less host"
# control case. Asserted rather than assumed, so this file fails loudly if
# someone ever registers it instead of silently testing nothing.
ADAPTERLESS_HARNESS = "openclaude"


def _agent(**kwargs) -> Agent:
    kwargs.setdefault("name", "Probe")
    return Agent(**kwargs)


def test_adapterless_control_harness_really_has_no_adapter():
    assert ADAPTERLESS_HARNESS not in HOST_ADAPTERS


# ── 1. Every registered host harness is switchable ────────────────────────


@pytest.mark.parametrize("harness", sorted(HOST_ADAPTERS))
def test_every_host_adapter_harness_is_switchable(harness: str):
    """For EACH key in HOST_ADAPTERS a host agent must report switchable.

    Iterating the registry (not a hardcoded list) is the whole point: a newly
    registered adapter is covered automatically, so the frontend lock and the
    backend capability cannot drift apart again.
    """
    agent = _agent(agent_runtime="host", harness=harness)

    assert agent.runtime_switchable is True, (
        f"host harness '{harness}' has a HostHarnessAdapter but reports "
        f"runtime_switchable=False — the UI would lock it out"
    )
    assert agent.runtime_switch_blocked_reason is None
    assert is_host_inplace(agent) is True
    # The switch endpoint's own guard must agree — no exception.
    _ensure_agent_switchable(agent)


# ── 2. A host harness WITHOUT an adapter is blocked, with a reason ────────


def test_host_agent_without_adapter_is_blocked_with_reason():
    agent = _agent(agent_runtime="host", harness=ADAPTERLESS_HARNESS)

    assert agent.runtime_switchable is False
    reason = agent.runtime_switch_blocked_reason
    assert reason, "a blocked agent must always carry a plain-text reason"
    # The reason must be actionable: name the harness and the supported set.
    assert ADAPTERLESS_HARNESS in reason
    for supported in HOST_ADAPTERS:
        assert supported in reason
    assert is_host_inplace(agent) is False

    with pytest.raises(AgentNotSwitchableError) as excinfo:
        _ensure_agent_switchable(agent)
    # Endpoint error text and UI reason are literally the same string.
    assert str(excinfo.value) == reason


def test_host_agent_with_null_harness_is_blocked_with_reason():
    agent = _agent(agent_runtime="host", harness=None)

    assert agent.runtime_switchable is False
    assert agent.runtime_switch_blocked_reason


# ── 3. cli-bridge / other agent runtimes ──────────────────────────────────


def test_cli_bridge_is_switchable_and_not_host_inplace():
    agent = _agent(agent_runtime="cli-bridge", harness="claude")

    assert agent.runtime_switchable is True
    assert agent.runtime_switch_blocked_reason is None
    # cli-bridge switches via container restart, never the in-place host path.
    assert is_host_inplace(agent) is False
    _ensure_agent_switchable(agent)


@pytest.mark.parametrize("agent_runtime", ["manual", "free-code-bridge", "claude-code"])
def test_other_agent_runtimes_are_blocked_with_reason(agent_runtime: str):
    agent = _agent(agent_runtime=agent_runtime)

    assert agent.runtime_switchable is False
    reason = agent.runtime_switch_blocked_reason
    assert reason and agent_runtime in reason

    with pytest.raises(AgentNotSwitchableError) as excinfo:
        _ensure_agent_switchable(agent)
    assert str(excinfo.value) == reason


# ── 4. The verdict and the guard can never disagree ───────────────────────


@pytest.mark.parametrize(
    "agent_runtime,harness",
    [("host", h) for h in sorted(HOST_ADAPTERS)]
    + [
        ("host", ADAPTERLESS_HARNESS),
        ("host", None),
        ("cli-bridge", None),
        ("cli-bridge", "claude"),
        ("manual", None),
        ("free-code-bridge", "omp"),
    ],
)
def test_derived_field_and_switch_guard_never_disagree(agent_runtime, harness):
    """``runtime_switchable`` must predict ``_ensure_agent_switchable`` exactly."""
    agent = _agent(agent_runtime=agent_runtime, harness=harness)
    switchable, reason = runtime_switch_availability(agent)

    assert agent.runtime_switchable is switchable
    assert agent.runtime_switch_blocked_reason == reason
    assert (reason is None) is switchable

    try:
        _ensure_agent_switchable(agent)
        guard_allows = True
    except AgentNotSwitchableError:
        guard_allows = False
    assert guard_allows is switchable


# ── 5. The fields actually reach the API (list AND detail) ────────────────


@pytest.mark.asyncio
async def test_list_and_detail_endpoints_expose_the_derived_fields(
    auth_client, make_agent
):
    """Both GET /agents and GET /agents/{id} must carry the derived fields.

    Serialising Agent objects straight out of the ORM is what makes this work
    (pydantic computed fields on the model) — this test is what stops someone
    swapping in a hand-written response model that quietly drops them.
    """
    boss = await make_agent(
        name="Boss Probe", agent_runtime="host", harness="claude", slug=f"boss-{uuid.uuid4().hex[:6]}"
    )
    locked = await make_agent(
        name="Locked Probe", agent_runtime="host", harness=ADAPTERLESS_HARNESS
    )

    listed = await auth_client.get("/api/v1/agents")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()}

    assert by_id[str(boss.id)]["runtime_switchable"] is True
    assert by_id[str(boss.id)]["runtime_switch_blocked_reason"] is None
    assert by_id[str(locked.id)]["runtime_switchable"] is False
    assert by_id[str(locked.id)]["runtime_switch_blocked_reason"]

    detail = await auth_client.get(f"/api/v1/agents/{locked.id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["runtime_switchable"] is False
    assert body["runtime_switch_blocked_reason"] == (
        by_id[str(locked.id)]["runtime_switch_blocked_reason"]
    )

    boss_detail = await auth_client.get(f"/api/v1/agents/{boss.id}")
    assert boss_detail.json()["runtime_switchable"] is True
