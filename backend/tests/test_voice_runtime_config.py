"""Jarvis' voice provider is a runtime binding, like every other agent (ADR-074).

Why these tests exist:

    Before this, WHICH provider Jarvis spoke to lived only in the voice-worker's
    container env. Changing it meant editing docker-compose and rebuilding, and
    nothing in MC showed the current state. ADR-060 had ruled the DB out because
    there was no host-switch machinery yet — HOST_ADAPTERS closed that gap.

The load-bearing property is that a voice runtime is NOT an openai runtime.
Both talk to api.openai.com, but the wire protocol is the realtime speech
socket, not chat completions. If the classification ever fell through to
"openai", every openai-speaking CLI harness (openclaude, omp, hermes) would
suddenly look compatible with Jarvis' voice rows and the picker would offer
nonsense bindings.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime
from app.services.harness_compat import (
    VOICE_RUNTIME_TYPES,
    is_compatible,
    runtime_protocol,
)
from app.services.runtime_naming import CURATED_RUNTIME_TYPES

SEED_PATH = Path(__file__).resolve().parents[1] / "config" / "runtimes.json"


def _rt(runtime_type: str, slug: str = "probe", **kw) -> Runtime:
    kw.setdefault("display_name", "Probe")
    kw.setdefault("model_identifier", "some-model")
    return Runtime(slug=slug, runtime_type=runtime_type, **kw)


def _seed_rows() -> list[dict]:
    return json.loads(SEED_PATH.read_text())


# ── Protocol classification ───────────────────────────────────────────────


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_runtime_types_map_to_the_voice_protocol(runtime_type: str):
    assert runtime_protocol(_rt(runtime_type)) == "voice"


def test_jarvis_accepts_voice_runtimes():
    for runtime_type in VOICE_RUNTIME_TYPES:
        assert is_compatible("jarvis", _rt(runtime_type)) is True


@pytest.mark.parametrize("harness", ["claude", "openclaude", "omp", "hermes", "grok", "kimi"])
def test_no_cli_harness_may_bind_a_voice_runtime(harness: str):
    """The regression this guards: voice_openai falling through to "openai".

    openclaude/omp/hermes all speak the openai protocol. If the voice check
    were removed (or placed after the _OPENAI_TYPES check), voice_openai would
    classify as "openai" and every one of them would report compatible.
    """
    assert is_compatible(harness, _rt("voice_openai")) is False
    assert is_compatible(harness, _rt("voice_xai")) is False


def test_jarvis_may_not_bind_a_chat_runtime():
    """The reverse direction — Jarvis is not a CLI and cannot run a chat model."""
    assert is_compatible("jarvis", _rt("vllm_docker")) is False
    assert is_compatible("jarvis", _rt("cloud")) is False
    assert is_compatible("jarvis", _rt("grok")) is False


# ── Seed rows ─────────────────────────────────────────────────────────────


def test_seed_carries_one_row_per_voice_runtime_type():
    by_type = {r["runtime_type"]: r for r in _seed_rows() if r["runtime_type"] in VOICE_RUNTIME_TYPES}
    assert set(by_type) == set(VOICE_RUNTIME_TYPES), (
        "every declared voice runtime type needs a seed row, otherwise the "
        "provider is selectable in code but invisible in the picker"
    )


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_seed_rows_carry_a_model_identifier(runtime_type: str):
    """Empty model_identifier makes the switch probe the live endpoint.

    ensure_runtime_model_identifier() reaches out to the runtime's endpoint to
    discover a model when the row has none. For api.openai.com that is a
    network call on every switch — slow, and it fails closed without a key.
    """
    row = next(r for r in _seed_rows() if r["runtime_type"] == runtime_type)
    assert (row.get("model_identifier") or "").strip(), (
        f"seed row '{row['id']}' must name its model explicitly"
    )


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_display_names_are_protected_from_the_naming_rule(runtime_type: str):
    """api.openai.com is a known provider host — without curation the seeder
    would silently rename the row after it, and the picker would show two
    entries that read like chat models rather than voice arms."""
    assert runtime_type in CURATED_RUNTIME_TYPES


def test_voice_rows_are_single_instance():
    """The same guard grok-cloud and kimi-cloud use, and for the same reason.

    The switch service hard-blocks binding a single_instance runtime unless the
    agent switches in place (host + adapter). That keeps every cli-bridge agent
    away from these rows without a single extra check. Jarvis itself is
    host-in-place, so the block does not apply to him.
    """
    for row in _seed_rows():
        if row["runtime_type"] in VOICE_RUNTIME_TYPES:
            assert row.get("single_instance") is True, row["id"]


# ── The adapter: Jarvis becomes switchable, but nothing is written ─────────


def test_jarvis_is_switchable_as_a_host_agent():
    """The whole point of registering the adapter.

    Explicit rather than relying on the parametrised sweep in
    test_runtime_switchable_field.py: that one iterates HOST_ADAPTERS, so it
    would still pass if "jarvis" were never added. This one fails.

    agent_runtime must be passed explicitly — the Agent default is cli-bridge,
    and a cli-bridge agent is switchable for entirely different reasons, which
    would make this a green test that proves nothing.
    """
    from app.services.host_harness_adapter import HOST_ADAPTERS, is_host_inplace

    assert "jarvis" in HOST_ADAPTERS
    agent = Agent(name="Jarvis", slug="jarvis", agent_runtime="host", harness="jarvis")

    assert agent.runtime_switchable is True
    assert agent.runtime_switch_blocked_reason is None
    assert is_host_inplace(agent) is True


@pytest.mark.asyncio
async def test_switching_a_voice_runtime_writes_no_agent_env(tmp_path, monkeypatch):
    """A voice switch must not touch the filesystem.

    sync_host_agent_model() writes the provider model into the host agent's
    agent.env. For voice that file does not exist and must not be created:
    writing one would put an OPENAI_BASE_URL next to Jarvis' token for a
    process that never reads it — the ADR-056 Finding 5 shape of accident.
    """
    from app.services import host_harness_adapter as hha

    monkeypatch.setattr(hha, "_home_host", lambda: tmp_path, raising=False)
    monkeypatch.setattr(
        "app.services.agent_bootstrap._home_host", lambda: tmp_path, raising=False
    )

    agent = Agent(name="Jarvis", slug="jarvis", agent_runtime="host", harness="jarvis")
    runtime = _rt("voice_openai", slug="voice-openai")

    await hha.sync_host_agent_model(agent, runtime, session=None)

    assert not (tmp_path / ".mc" / "agents" / "jarvis" / "agent.env").exists()
    assert list(tmp_path.rglob("agent.env")) == []


@pytest.mark.asyncio
async def test_reload_does_not_restart_anything():
    """Restarting the voice container mid-call would hang up on Mark.

    The no-op is the design (the worker re-reads per call), so it is asserted
    rather than left implicit.
    """
    from app.services.host_harness_adapter import HOST_ADAPTERS

    result = await HOST_ADAPTERS["jarvis"].reload(
        Agent(name="Jarvis", slug="jarvis", agent_runtime="host", harness="jarvis")
    )

    assert result["ok"] is True
    assert result["restarted"] is False
    assert result["note"].strip()


@pytest.mark.asyncio
async def test_bootstrap_refuses_with_a_useful_message():
    """MC does not provision Jarvis — compose does. The refusal must say so,
    and must not read like the switch is broken."""
    from fastapi import HTTPException

    from app.services.host_harness_adapter import HOST_ADAPTERS

    with pytest.raises(HTTPException) as excinfo:
        await HOST_ADAPTERS["jarvis"].bootstrap(None, Agent(name="Jarvis", slug="jarvis"), None)

    assert excinfo.value.status_code == 422
    assert "compose" in str(excinfo.value.detail).lower()


# ── Host process actions must not be offered for Jarvis ───────────────────


def test_jarvis_has_no_host_process():
    """The restart button on the agent page could only ever fail for Jarvis.

    It is gated on agent_runtime == "host", which Jarvis is by binding — but it
    is a docker-compose service under the "voice" profile, with no launchd job.
    Pressing it returns 'Could not find service "com.mc.agent.jarvis"', which
    reads like a broken agent rather than an inapplicable button. (Live report
    from Mark, 22.08.)
    """
    from app.services.host_harness_adapter import manages_host_process

    jarvis = Agent(name="Jarvis", slug="jarvis", agent_runtime="host", harness="jarvis")

    assert manages_host_process(jarvis) is False
    assert jarvis.host_process_managed is False


@pytest.mark.parametrize("harness", ["hermes", "grok", "kimi", "claude"])
def test_other_host_harnesses_keep_their_process_actions(harness: str):
    """The fix must not hide the button fleet-wide — those agents really do
    have a launchd job, and it is their only handle."""
    from app.services.host_harness_adapter import manages_host_process

    assert manages_host_process(Agent(name="X", agent_runtime="host", harness=harness)) is True


def test_host_agent_without_adapter_keeps_process_actions():
    """Managed outside MC — the process actions are all it has."""
    from app.services.host_harness_adapter import manages_host_process

    assert manages_host_process(Agent(name="X", agent_runtime="host", harness=None)) is True


def test_cli_bridge_agents_have_no_host_process():
    from app.services.host_harness_adapter import manages_host_process

    assert manages_host_process(Agent(name="X", agent_runtime="cli-bridge", harness="claude")) is False
