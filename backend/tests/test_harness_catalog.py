"""Harness catalog — dynamic model discovery + the observed context-window
map, replacing the hardcoded alias/window maps (the adapter-contract
generalization pattern for future harnesses).

Four groups, matching the round's own spec:
1. ``parse_model_picker`` (pure) against a REAL captured ``/model`` picker
   fixture (freecode, cli-bridge, Claude Code 2.1.234, 2026-08-18 — no
   personal data, generic CLI chrome).
2. Version-keyed cache: a cached catalog for one ``cli_version`` must never
   answer a request for a different one.
3. The observed-map precedence chain (``resolve_context_window``'s
   ``observed`` tier).
4. Fallback paths: no harness, no version, discovery failure, discovery
   already in progress (lock contention) — all -> ``[]``, never raise.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

import app.redis_client as redis_client_mod
from app.services import harness_catalog as hc
from app.services.transcript_chat import resolve_context_window

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════════════════
# parse_model_picker — pure, real captured fixture
# ══════════════════════════════════════════════════════════════════════════

# Real capture (freecode, cli-bridge, Claude Code 2.1.234, 2026-08-18) —
# `/model` opened in a THROWAWAY window, never a live session. No personal
# data — generic picker chrome + built-in model names + one local-model row.
REAL_MODEL_PICKER_PANE = """\
   Select model
   Switch between Claude models. Your pick becomes the default for new
   sessions. For other/previous model names, specify with --model.

     1. Default (recommended)     Sonnet 5 · Efficient for routine tasks
     2. Sonnet                    Sonnet 5 · Efficient for routine tasks
   ❯ 3. Opus ✔                    Opus 5 · Best for everyday, complex tasks
     4. Haiku                     Haiku 4.5 · Fastest for quick answers
     5. Qwen/Qwen3.6-35B-A3B-FP8  Detected from Local OpenAI-compatible

   ● High effort (default) ←/→ to adjust

   Enter to set as default · s to use this session only · Esc to cancel
"""


async def test_parse_model_picker_real_fixture():
    options = hc.parse_model_picker(REAL_MODEL_PICKER_PANE)

    assert options == [
        {"command": "default", "label": "Default"},
        {"command": "sonnet", "label": "Sonnet"},
        {"command": "opus", "label": "Opus"},
        {"command": "haiku", "label": "Haiku"},
        {"command": "Qwen/Qwen3.6-35B-A3B-FP8", "label": "Qwen/Qwen3.6-35B-A3B-FP8"},
    ]


async def test_parse_model_picker_strips_active_marker_and_recommended_suffix():
    """Row 3 in the real fixture (currently-selected "Opus ✔", with the "❯"
    row-pointer too) must not leak either marker into the label or command —
    row 1's "(recommended)" suffix likewise."""
    options = hc.parse_model_picker(REAL_MODEL_PICKER_PANE)
    by_command = {o["command"]: o for o in options}

    assert by_command["opus"]["label"] == "Opus"
    assert "✔" not in by_command["opus"]["label"]
    assert by_command["default"]["label"] == "Default"
    assert "recommended" not in by_command["default"]["label"].lower()


async def test_parse_model_picker_ignores_non_row_lines():
    """Header/description/effort-row/footer lines must never be misread as
    model rows."""
    options = hc.parse_model_picker(REAL_MODEL_PICKER_PANE)
    assert len(options) == 5  # exactly the 5 numbered rows, nothing else


async def test_parse_model_picker_empty_pane_yields_empty_list():
    assert hc.parse_model_picker("") == []
    assert hc.parse_model_picker("nothing matching here\nor here") == []


# ══════════════════════════════════════════════════════════════════════════
# harness_for — v1 runtime gating
# ══════════════════════════════════════════════════════════════════════════


async def test_harness_for_docker_agent():
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")
    assert hc.harness_for(agent) == "claude"


async def test_harness_for_foreign_cli_returns_none():
    """Kimi/Sparky sind cli-bridge, aber kein Claude — der Katalog darf ihre
    TUI nie mit einem /model-Picker-Probe anfassen (Gate 18.08.2026)."""
    for harness in ("kimi", "omp", None):
        agent = SimpleNamespace(slug="kimi", agent_runtime="cli-bridge", harness=harness)
        assert hc.harness_for(agent) is None


async def test_harness_for_host_agent_returns_none():
    agent = SimpleNamespace(slug="boss", agent_runtime="host")
    assert hc.harness_for(agent) is None


# ══════════════════════════════════════════════════════════════════════════
# resolve_cli_version — subprocess mocked
# ══════════════════════════════════════════════════════════════════════════


async def test_resolve_cli_version_parses_real_output_format(monkeypatch):
    def _fake_run(argv, **kwargs):
        assert argv == ["docker", "exec", "-u", "agent", "mc-agent-rex", "claude", "--version"]
        return subprocess.CompletedProcess(argv, returncode=0, stdout="2.1.234 (Claude Code)\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")
    assert await hc.resolve_cli_version(agent) == "2.1.234"


async def test_resolve_cli_version_none_for_host_agent():
    agent = SimpleNamespace(slug="boss", agent_runtime="host")
    assert await hc.resolve_cli_version(agent) is None


async def test_resolve_cli_version_none_on_nonzero_exit(monkeypatch):
    def _fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode=1, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")
    assert await hc.resolve_cli_version(agent) is None


async def test_resolve_cli_version_none_on_subprocess_exception(monkeypatch):
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")
    assert await hc.resolve_cli_version(agent) is None


# ══════════════════════════════════════════════════════════════════════════
# discover_model_catalog — Redis cache (fakeredis), version-keying,
# discovery lock, fallback paths
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture
async def redis_env(fake_redis, monkeypatch):
    """Points app.redis_client's module-level singleton at fakeredis, so
    get_redis() (called internally by harness_catalog) returns it without
    a real connection attempt."""
    monkeypatch.setattr(redis_client_mod, "_redis", fake_redis)
    return fake_redis


async def test_discover_model_catalog_no_harness_returns_empty_without_any_io(redis_env):
    """Boss/host: harness_for -> None -> short-circuits before touching
    Redis OR subprocess at all."""
    agent = SimpleNamespace(slug="boss", agent_runtime="host")
    assert await hc.discover_model_catalog(agent) == []


async def test_discover_model_catalog_no_version_returns_empty(monkeypatch, redis_env):
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _no_version(a):
        return None

    monkeypatch.setattr(hc, "resolve_cli_version", _no_version)

    assert await hc.discover_model_catalog(agent) == []


async def test_discover_model_catalog_cache_hit_skips_discovery(monkeypatch, redis_env):
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _version(a):
        return "2.1.234"

    async def _boom(a):
        raise AssertionError("discovery must not run on a cache hit")

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _boom)

    cached = [{"command": "opus", "label": "Opus"}]
    await redis_env.set(hc.RedisKeys.model_catalog("claude", "2.1.234"), json.dumps(cached))

    assert await hc.discover_model_catalog(agent) == cached


async def test_discover_model_catalog_version_keyed_cache_does_not_leak_across_versions(monkeypatch, redis_env):
    """A cached catalog for version 2.1.233 must NOT answer a request for
    2.1.234 — a CLI upgrade invalidates the catalog automatically."""
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _version(a):
        return "2.1.234"

    discovery_calls = {"n": 0}

    async def _discover(a):
        discovery_calls["n"] += 1
        return [{"command": "haiku", "label": "Haiku"}]

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _discover)

    await redis_env.set(
        hc.RedisKeys.model_catalog("claude", "2.1.233"),
        json.dumps([{"command": "opus", "label": "Opus"}]),
    )

    result = await hc.discover_model_catalog(agent)

    assert result == [{"command": "haiku", "label": "Haiku"}]  # NOT the 2.1.233 entry
    assert discovery_calls["n"] == 1  # a fresh discovery ran for 2.1.234


async def test_discover_model_catalog_populates_cache_after_fresh_discovery(monkeypatch, redis_env):
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _version(a):
        return "2.1.234"

    async def _discover(a):
        return [{"command": "opus", "label": "Opus"}]

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _discover)

    result = await hc.discover_model_catalog(agent)
    assert result == [{"command": "opus", "label": "Opus"}]

    cached = await redis_env.get(hc.RedisKeys.model_catalog("claude", "2.1.234"))
    assert json.loads(cached) == [{"command": "opus", "label": "Opus"}]


async def test_discover_model_catalog_discovery_failure_returns_empty_not_raises(monkeypatch, redis_env):
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _version(a):
        return "2.1.234"

    async def _boom(a):
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _boom)

    assert await hc.discover_model_catalog(agent) == []


async def test_discover_model_catalog_concurrent_request_skips_when_lock_held(monkeypatch, redis_env):
    """Two cache-miss requests for the same (harness, version) at once must
    not both spin up a throwaway discovery window — the second one gets []
    (falls back to the static alias list for that one request) rather than
    piling on."""
    agent = SimpleNamespace(slug="rex", agent_runtime="cli-bridge", harness="claude")

    async def _version(a):
        return "2.1.234"

    discovery_calls = {"n": 0}

    async def _discover(a):
        discovery_calls["n"] += 1
        return [{"command": "opus", "label": "Opus"}]

    monkeypatch.setattr(hc, "resolve_cli_version", _version)
    monkeypatch.setattr(hc, "_discover_via_throwaway_window", _discover)

    # Simulate another request already holding the lock.
    await redis_env.set(
        hc.RedisKeys.model_catalog_discovery_lock("claude", "2.1.234"), "1", ex=60
    )

    assert await hc.discover_model_catalog(agent) == []
    assert discovery_calls["n"] == 0


# ══════════════════════════════════════════════════════════════════════════
# observe_model_window / get_observed_model_windows — Redis hash
# ══════════════════════════════════════════════════════════════════════════


async def test_observe_and_get_model_windows_roundtrip(redis_env):
    await hc.observe_model_window("claude-opus-5", 1_000_000)
    await hc.observe_model_window("claude-haiku-4-5", 200_000)

    windows = await hc.get_observed_model_windows()

    assert windows == {"claude-opus-5": 1_000_000, "claude-haiku-4-5": 200_000}


async def test_observe_model_window_newest_write_wins(redis_env):
    await hc.observe_model_window("claude-opus-5", 500_000)
    await hc.observe_model_window("claude-opus-5", 1_000_000)  # a later, fresher read

    windows = await hc.get_observed_model_windows()

    assert windows["claude-opus-5"] == 1_000_000


async def test_get_observed_model_windows_empty_hash_returns_empty_dict(redis_env):
    assert await hc.get_observed_model_windows() == {}


async def test_get_observed_model_windows_fail_silent_on_redis_error(monkeypatch):
    async def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(hc, "get_redis", _boom)

    assert await hc.get_observed_model_windows() == {}


async def test_observe_model_window_fail_silent_on_redis_error(monkeypatch):
    async def _boom():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(hc, "get_redis", _boom)

    await hc.observe_model_window("claude-opus-5", 1_000_000)  # must not raise


# ══════════════════════════════════════════════════════════════════════════
# resolve_context_window's observed-map precedence tier
# ══════════════════════════════════════════════════════════════════════════


async def test_resolve_context_window_observed_tier_outranks_config_seed():
    # config seed says claude-haiku-4-5 -> 200_000; an observation overrides it.
    assert resolve_context_window(
        "claude-haiku-4-5", observed={"claude-haiku-4-5": 999_999}
    ) == 999_999


async def test_resolve_context_window_falls_back_to_config_seed_when_not_observed():
    assert resolve_context_window("claude-haiku-4-5", observed={"some-other-model": 1}) == 200_000


async def test_resolve_context_window_none_observed_behaves_like_before():
    assert resolve_context_window("claude-opus-5", observed=None) == 1_000_000
    assert resolve_context_window("claude-opus-5") == 1_000_000


async def test_resolve_context_window_observed_does_not_match_unrelated_model():
    assert resolve_context_window("totally-unknown-model", observed={"claude-opus-5": 1}) is None
