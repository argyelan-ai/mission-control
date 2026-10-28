"""omp caps a reply's THINKING so the reply always keeps room for text and a
tool call (``OMP_THINKING_BUDGET`` -> models.yml ``compat.extraBody.thinking_budget``).

Incident (2026-10-28): on the local slot (262144 window, answer budget 32768)
a bench run at effort "high" thought through all 32768 output tokens seven
times in a row: ``finish=length``, no text, no tool call, about 8.5 min each;
omp gave up after three length retries. Raising the answer budget is no fix
there: the engine reserves prompt + budget in one shared pool per request.

The engine closes the think block itself when the request carries
``thinking_budget`` (live, direct request: budget 600 -> 601 reasoning tokens,
then a real answer; without it 3711 reasoning tokens). omp 18.10.1 has no own
field for it, but sends a model's ``compat.extraBody`` verbatim on every
request (checked in a throwaway container at every thinking level).

Rule (model- and harness-neutral, ADR-086/093; reads only the runtime row):
* only a LOCAL runtime that REASONS (``supports_reasoning``) gets a budget.
  Cloud APIs refuse unknown request fields; a non-reasoning model has
  nothing to cap.
* thinking budget = answer budget minus the answer room, the answer room is
  half the answer budget and at least 8192 tokens. No budget below 1024.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from app.models.agent import Agent
from app.models.runtime import Runtime

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_SCRIPT = REPO_ROOT / "docker" / "omp-bridge" / "render-omp-config.sh"
ENTRYPOINT = REPO_ROOT / "docker" / "omp-bridge" / "entrypoint.sh"


# ── the rule itself ──────────────────────────────────────────────────────────

# answer budget -> thinking budget (local, reasoning)
TABLE = [
    (131_072, 65_536),  # the 1M slot's answer budget
    (65_536, 32_768),
    (32_768, 16_384),  # the 262k slot: 16384 thinking, 16384 answer room
    (16_384, 8_192),
    (12_288, 4_096),  # the 8192 answer-room floor wins over half
    (9_216, 1_024),
    (9_000, None),  # less than 1024 left to think: no cap at all
    (8_192, None),
    (4_096, None),
]


@pytest.mark.parametrize("max_tokens,expected", TABLE)
def test_thinking_budget_leaves_answer_room(max_tokens, expected):
    from app.routers.internal import omp_thinking_budget

    assert omp_thinking_budget(max_tokens, local=True, reasoning=True) == expected


@pytest.mark.parametrize("max_tokens", [4_096, 9_216, 12_288, 32_768, 65_536, 131_072, 262_144])
def test_a_reply_always_keeps_room_for_text_and_a_tool_call(max_tokens):
    """A single-file write of ~35 KB is ~11k tokens. Wherever a budget is set,
    the answer keeps half the output budget and never less than 8192."""
    from app.routers.internal import omp_thinking_budget

    budget = omp_thinking_budget(max_tokens, local=True, reasoning=True)
    if budget is None:
        return
    answer_room = max_tokens - budget
    assert answer_room >= 8_192
    assert answer_room >= max_tokens // 2
    assert budget >= 1_024


@pytest.mark.parametrize("local,reasoning", [(False, True), (True, False), (False, False)])
def test_no_budget_unless_local_and_reasoning(local, reasoning):
    from app.routers.internal import omp_thinking_budget

    assert omp_thinking_budget(32_768, local=local, reasoning=reasoning) is None


# ── build_runtime_env: who gets it ───────────────────────────────────────────


def _omp_agent() -> Agent:
    return Agent(name="ThinkAgent", agent_runtime="cli-bridge", harness="omp")


def _runtime(**over) -> Runtime:
    base = dict(
        slug="think-rt",
        display_name="think test",
        runtime_type="openai_compatible",
        endpoint="http://192.0.2.10:8000/v1",
        model_identifier="some-local-model",
        enabled=True,
    )
    base.update(over)
    return Runtime(**base)


@pytest.mark.asyncio
async def test_local_reasoning_slot_gets_a_thinking_budget(async_session):
    """The live case: host-bound slot row, 262144 window, reasoning model.
    The answer budget stays 32768 (the engine's pool), thinking gets half."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(host_id=uuid.uuid4(), max_context_len=262_144, supports_reasoning=True)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_MAX_TOKENS"] == "32768"
    assert env["OMP_THINKING_BUDGET"] == "16384"


@pytest.mark.asyncio
async def test_local_slot_with_1m_window_gets_half_of_its_budget(async_session):
    from app.routers.internal import build_runtime_env

    rt = _runtime(host_id=uuid.uuid4(), max_context_len=1_048_576, supports_reasoning=True)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert env["OMP_MAX_TOKENS"] == "131072"
    assert env["OMP_THINKING_BUDGET"] == "65536"


@pytest.mark.asyncio
async def test_local_runtime_without_a_window_uses_the_fallback_budget(async_session):
    """No window on the row: omp's models.yml falls back to maxTokens 32768,
    so the thinking budget follows that same number."""
    from app.routers.internal import build_runtime_env

    rt = _runtime(runtime_type="vllm_docker", supports_reasoning=True)
    env = await build_runtime_env(rt, async_session, agent=_omp_agent())

    assert "OMP_MAX_TOKENS" not in env
    assert env["OMP_THINKING_BUDGET"] == "16384"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "over",
    [
        # local, but MC does not know it reasons
        dict(host_id=uuid.uuid4(), max_context_len=262_144, supports_reasoning=False),
        # cloud type: a managed API refuses unknown request fields
        dict(runtime_type="cloud", endpoint="https://api.example.com/v1",
             max_context_len=262_144, supports_reasoning=True),
        # host-less openai_compatible = managed endpoint
        dict(endpoint="https://api.example.com/v1", max_context_len=262_144,
             supports_reasoning=True),
    ],
)
async def test_no_budget_for_cloud_or_non_reasoning_runtimes(async_session, over):
    """The key is still written, empty: the host agent.env sync merges over
    the old file, so a missing key would keep the previous runtime's budget
    after a runtime switch."""
    from app.routers.internal import build_runtime_env

    env = await build_runtime_env(_runtime(**over), async_session, agent=_omp_agent())

    assert env["OMP_THINKING_BUDGET"] == ""


# ── renderers: models.yml carries it as compat.extraBody ────────────────────

COMPAT_BLOCK = (
    "        compat:\n"
    "          extraBody:\n"
    "            thinking_budget: {n}\n"
)


def _render(tmp_path: Path, extra: dict[str, str]) -> str:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR"))
    }
    # no real omp on PATH: this checks the files, not omp's settings
    env["PATH"] = "/usr/bin:/bin"
    env.update(
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OMP_RENDER_MOUNTINFO=str(tmp_path / "no-mountinfo"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL="org/local-model",
        **extra,
    )
    result = subprocess.run(
        ["bash", str(RENDER_SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return (home / ".omp" / "profiles" / "mc-agent" / "agent" / "models.yml").read_text()


def test_container_renderer_writes_the_thinking_budget(tmp_path):
    content = _render(tmp_path, {"OMP_MAX_TOKENS": "32768", "OMP_THINKING_BUDGET": "16384"})
    assert COMPAT_BLOCK.format(n=16384) in content
    assert "        maxTokens: 32768\n" in content


@pytest.mark.parametrize("value", [None, "", "abc", "0", "-5", "12 34"])
def test_container_renderer_writes_no_budget_without_a_valid_one(tmp_path, value):
    """Empty (cloud / non-reasoning), missing (old backend) or garbage: no
    compat block at all — an engine never gets a field MC did not decide."""
    extra = {} if value is None else {"OMP_THINKING_BUDGET": value}
    content = _render(tmp_path, extra)
    assert "compat:" not in content
    assert "thinking_budget" not in content


def test_container_renderer_output_is_valid_yaml_with_the_budget(tmp_path):
    import yaml

    content = _render(tmp_path, {"OMP_THINKING_BUDGET": "16384"})
    model = yaml.safe_load(content)["providers"]["mc-openai"]["models"][0]
    assert model["compat"] == {"extraBody": {"thinking_budget": 16384}}
    assert model["input"] == ["text"]


def test_host_renderer_writes_the_thinking_budget(tmp_path):
    import yaml

    from app.services.host_provisioning import render_omp_host_models_yml

    path = render_omp_host_models_yml(
        "think",
        {
            "OPENAI_BASE_URL": "http://192.0.2.20:8000/v1",
            "OPENAI_MODEL": "org/local-model",
            "OMP_MAX_TOKENS": "32768",
            "OMP_THINKING_BUDGET": "16384",
        },
        home=tmp_path,
    )
    content = Path(path).read_text()
    assert COMPAT_BLOCK.format(n=16384) in content
    model = yaml.safe_load(content)["providers"]["mc-openai"]["models"][0]
    assert model["compat"] == {"extraBody": {"thinking_budget": 16384}}


@pytest.mark.parametrize("value", [None, "", "abc", "0"])
def test_host_renderer_writes_no_budget_without_a_valid_one(tmp_path, value):
    from app.services.host_provisioning import render_omp_host_models_yml

    env = {"OPENAI_BASE_URL": "http://192.0.2.20:8000/v1", "OPENAI_MODEL": "m"}
    if value is not None:
        env["OMP_THINKING_BUDGET"] = value
    content = Path(render_omp_host_models_yml("think", env, home=tmp_path)).read_text()
    assert "thinking_budget" not in content
    assert "compat:" not in content


# ── the env path from MC to the renderer ─────────────────────────────────────


def test_entrypoint_passes_the_budget_from_bootstrap():
    """The entrypoint takes only allow-listed keys from MC's bootstrap answer
    and re-exports them for the renderer."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    allow = next(ln for ln in text.splitlines() if '"OMP_MAX_TOKENS"' in ln and "if k in" in ln)
    assert '"OMP_THINKING_BUDGET"' in allow
    assert "export OMP_THINKING_BUDGET=" in text


def test_reload_fetches_the_budget_from_bootstrap():
    """`docker exec … render-omp-config.sh` (reload) takes its values from
    the bootstrap answer, not from the container's start env."""
    text = RENDER_SCRIPT.read_text(encoding="utf-8")
    fetch = text.split("fetch_bootstrap() {", 1)[1].split("\n}\n", 1)[0]
    assert '"OMP_THINKING_BUDGET"' in fetch
    # a reload onto a runtime without a budget must drop the old one
    assert "unset OMP_THINKING_BUDGET" in fetch
