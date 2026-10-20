"""omp's answer budget (``OMP_MAX_TOKENS`` -> models.yml ``maxTokens``) follows
the context window for local models instead of a fixed 32768.

Incident (2026-10-20): a local DeepSeek V4.1 Flash with a 1M window thought
past 32768 tokens at the default effort ("high"). Twice the engine stopped at
``max_tokens`` while the model was still thinking, so the visible answer was
empty. MC capped every model, local or cloud, at 32768.

The rule is model- and harness-neutral (ADR-086/093): it reads only the
runtime row (window + local/cloud), never a model name.

* local runtime: a quarter of the window, at most 131072, never below the
  previous rule (half the window, at most 32768);
* cloud runtime: unchanged, half the window, at most 32768.
"""
from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_SCRIPT = REPO_ROOT / "docker" / "omp-bridge" / "render-omp-config.sh"


# ── the rule itself ──────────────────────────────────────────────────────────

# window -> (old fixed rule, new local rule)
LOCAL_TABLE = [
    (1_048_576, 32_768, 131_072),  # 1M window (the DeepSeek case)
    (524_288, 32_768, 131_072),
    (262_144, 32_768, 65_536),
    (131_072, 32_768, 32_768),
    (65_536, 32_768, 32_768),  # never lower than before
    (32_768, 16_384, 16_384),  # never lower than before
    (8_192, 4_096, 4_096),
]


@pytest.mark.parametrize("window,old,new", LOCAL_TABLE)
def test_local_budget_follows_the_window(window, old, new):
    from app.routers.internal import omp_max_output_tokens

    assert omp_max_output_tokens(window, local=True) == new
    assert new >= old  # a local model never gets less than before


@pytest.mark.parametrize("window", [8_192, 32_768, 131_072, 262_144, 1_048_576])
def test_cloud_budget_is_unchanged(window):
    from app.routers.internal import omp_max_output_tokens

    assert omp_max_output_tokens(window, local=False) == min(window // 2, 32_768)


@pytest.mark.parametrize("window", [4_096, 32_768, 100_000, 200_000, 400_000, 1_048_576, 2_097_152])
def test_local_budget_bounds(window):
    """The answer never takes more than half the window and never more than
    131072 tokens (the ceiling that bounds how long one turn holds a slot)."""
    from app.routers.internal import omp_max_output_tokens

    budget = omp_max_output_tokens(window, local=True)
    assert 0 < budget <= window // 2
    assert budget <= 131_072


# ── build_runtime_env: which runtimes count as local ─────────────────────────


def _omp_agent() -> Agent:
    return Agent(name="BudgetAgent", agent_runtime="cli-bridge", harness="omp")


def _runtime(**over) -> Runtime:
    base = dict(
        slug="budget-rt",
        display_name="budget test",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="some-local-model",
        enabled=True,
    )
    base.update(over)
    return Runtime(**base)


@pytest.mark.asyncio
async def test_local_slot_with_1m_window_gets_131072(async_session):
    """The live case: a host-bound OpenAI-compatible slot row with a 1M
    window. Before: 32768 (answers ran empty while still thinking)."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(host_id=uuid.uuid4(), max_context_len=1_048_576)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_CONTEXT_WINDOW"] == "1048576"
    assert env["OMP_MAX_TOKENS"] == "131072"


@pytest.mark.asyncio
async def test_self_hosted_engine_type_counts_as_local_without_host(async_session):
    from app.routers.internal import build_runtime_env

    rt = _runtime(runtime_type="vllm_docker", max_context_len=262_144)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_MAX_TOKENS"] == "65536"


@pytest.mark.asyncio
async def test_cloud_runtime_keeps_32768_even_with_1m_window(async_session):
    """Nothing changes for cloud endpoints: a managed API sets its own output
    cap, and a larger request could be refused there."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(
        runtime_type="cloud",
        endpoint="https://api.example.com/v1",
        max_context_len=1_048_576,
    )
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_CONTEXT_WINDOW"] == "1048576"
    assert env["OMP_MAX_TOKENS"] == "32768"


@pytest.mark.asyncio
async def test_hostless_openai_compatible_counts_as_cloud(async_session):
    """openai_compatible without a host is a managed endpoint (Runtime model
    contract: cloud/HTTP-only runtimes carry no host) -> old rule."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(endpoint="https://api.example.com/v1", max_context_len=1_048_576)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_MAX_TOKENS"] == "32768"


@pytest.mark.asyncio
async def test_preferred_window_is_used_when_max_is_unset(async_session):
    from app.routers.internal import build_runtime_env

    rt = _runtime(host_id=uuid.uuid4(), preferred_context_len=524_288)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_CONTEXT_WINDOW"] == "524288"
    assert env["OMP_MAX_TOKENS"] == "131072"


# ── renderers pass the value through, they do not cap it again ───────────────


def _render(tmp_path: Path, extra: dict[str, str]) -> str:
    import os

    home = tmp_path / "home"
    home.mkdir()
    # Never inherit runtime-shaped variables (see test_runtime_supports_vision:
    # inside an omp container OMP_ENV_FILE points at the agent's real file).
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR"))
    }
    env.update(
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL="org/local-model",
        **extra,
    )
    result = subprocess.run(
        [str(RENDER_SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return (home / ".omp" / "profiles" / "mc-agent" / "agent" / "models.yml").read_text()


def test_container_renderer_writes_the_budget_mc_sends(tmp_path):
    content = _render(tmp_path, {"OMP_CONTEXT_WINDOW": "1048576", "OMP_MAX_TOKENS": "131072"})
    assert "        contextWindow: 1048576\n" in content
    assert "        maxTokens: 131072\n" in content


def test_container_renderer_keeps_the_safe_fallback_without_a_window(tmp_path):
    """No window from MC (runtime row without one) -> unknown model -> the old
    conservative pair, never a guessed larger budget."""
    content = _render(tmp_path, {})
    assert "        contextWindow: 262144\n" in content
    assert "        maxTokens: 32768\n" in content


def test_host_renderer_writes_the_budget_mc_sends(tmp_path):
    from app.services.host_provisioning import render_omp_host_models_yml

    path = render_omp_host_models_yml(
        "budget",
        {
            "OPENAI_BASE_URL": "http://192.0.2.20:8000/v1",
            "OPENAI_MODEL": "org/local-model",
            "OMP_CONTEXT_WINDOW": "1048576",
            "OMP_MAX_TOKENS": "131072",
        },
        home=tmp_path,
    )
    content = Path(path).read_text()
    assert "        maxTokens: 131072\n" in content
