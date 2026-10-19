"""The composer's model chip shows the real model, and the picker only offers
models that can actually run.

Live 19.10.2026: every omp agent's chip read "MC model". All three renderers
of omp's ``models.yml`` (container ``render-omp-config.sh``, backend
``host_provisioning``, ``scripts/head/mc-head``) hard-coded ``name: MC model``;
omp reports that name in ``configOptions[id=model]`` and the backend passed it
through as the label. The same list also carried ~60 omp built-in
``openai/*`` models (GPT-5.x, o3 ...) that run against api.openai.com with a
placeholder key and fail with 401.

Three parts:
  * the renderers write the model id as ``name`` (new containers/heads);
  * the backend derives the label itself for MC-rendered models and for the
    old placeholder (old containers, before a re-render);
  * when the agent offers MC models, the picker offers only those (plus the
    model currently running, so the chip never loses its label).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_mc_head_parity import _load_mc_head
from tests.test_runtime_supports_vision import _run_render_script

MODEL = "org/glm53-exl3"  # what _run_render_script feeds as OPENAI_MODEL


# ── renderers ────────────────────────────────────────────────────────────────


def _model_names(models_yml: str) -> list[str]:
    return [
        line.split("name:", 1)[1].strip()
        for line in models_yml.splitlines()
        if line.startswith("        name:")
    ]


def test_container_renderer_names_the_model_by_its_id(tmp_path):
    content = _run_render_script(tmp_path, model_input=None).read_text()
    assert "MC model" not in content
    assert _model_names(content) == [MODEL]


def test_backend_host_renderer_names_the_model_by_its_id(tmp_path):
    from app.services.host_provisioning import render_omp_host_models_yml

    path = render_omp_host_models_yml(
        "rex",
        {"OPENAI_BASE_URL": "http://192.0.2.20:8000/v1", "OPENAI_MODEL": "GLM-5.3-Flash-EXL3"},
        home=tmp_path,
    )
    content = Path(path).read_text()
    assert "MC model" not in content
    assert _model_names(content) == ["GLM-5.3-Flash-EXL3"]


def test_head_renderer_names_the_model_by_its_id(tmp_path):
    mc_head = _load_mc_head()
    spec = {"base_url": "http://127.0.0.1:8000/v1", "model": "glm-5.1:cloud"}
    content = mc_head.render_omp_profile(tmp_path, spec, {}).read_text()
    assert "MC model" not in content
    assert _model_names(content) == ["glm-5.1:cloud"]


# ── backend: label + offer ───────────────────────────────────────────────────


class _StubAgent:
    def __init__(self, slug="acp-one", agent_runtime="cli-bridge", harness="omp"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness
        self.id = slug


def _state(current: str, options: list[dict]) -> dict:
    return {
        "version": 1,
        "configOptions": [
            {"id": "model", "category": "model", "currentValue": current, "options": options},
        ],
    }


# Shape recorded live (acp-chat-state.json, 19.10.2026), shortened: the MC
# model with the old placeholder name plus omp's built-in OpenAI catalogue.
_LIVE_OPTIONS = [
    {"value": "mc-openai/DeepSeek-V4.1-Flash-1017", "name": "MC model"},
    {"value": "openai/gpt-5.5", "name": "GPT-5.5"},
    {"value": "openai/o3", "name": "o3"},
    {"value": "openai/gpt-4.1-mini", "name": "GPT-4.1 mini"},
]


@pytest.fixture
def acp_agent():
    return _StubAgent()


def _use_state(monkeypatch, state):
    from app.services import agent_chat_input

    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: state)


async def test_old_placeholder_name_shows_the_real_model(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _use_state(monkeypatch, _state("mc-openai/DeepSeek-V4.1-Flash-1017", _LIVE_OPTIONS))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    labels = {o["command"]: o["label"] for o in caps["modelOptions"]}
    assert labels["mc-openai/DeepSeek-V4.1-Flash-1017"] == "DeepSeek-V4.1-Flash-1017"


async def test_mc_model_label_drops_the_provider_even_with_a_full_name(acp_agent, monkeypatch):
    """New renderers write the bare model id as name; a hand-written or
    future name that repeats the provider must not leak "mc-openai/"."""
    from app.services import agent_chat_input

    options = [{"value": "mc-openai/Qwen3.8-27B", "name": "mc-openai/Qwen3.8-27B"}]
    _use_state(monkeypatch, _state("mc-openai/Qwen3.8-27B", options))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [o["label"] for o in caps["modelOptions"]] == ["Qwen3.8-27B"]


async def test_picker_offers_only_mc_models_when_the_agent_has_them(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _use_state(monkeypatch, _state("mc-openai/DeepSeek-V4.1-Flash-1017", _LIVE_OPTIONS))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [o["command"] for o in caps["modelOptions"]] == ["mc-openai/DeepSeek-V4.1-Flash-1017"]
    assert caps["model"] == "mc-openai/DeepSeek-V4.1-Flash-1017"


async def test_picker_keeps_the_running_model_even_if_it_is_not_an_mc_model(acp_agent, monkeypatch):
    """Someone already switched to a built-in model: the chip must still name
    it, and the MC model must be offered as the way back."""
    from app.services import agent_chat_input

    _use_state(monkeypatch, _state("openai/gpt-5.5", _LIVE_OPTIONS))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [(o["command"], o["label"]) for o in caps["modelOptions"]] == [
        ("mc-openai/DeepSeek-V4.1-Flash-1017", "DeepSeek-V4.1-Flash-1017"),
        ("openai/gpt-5.5", "GPT-5.5"),
    ]


async def test_picker_is_unfiltered_without_mc_models(acp_agent, monkeypatch):
    """Any other runtime (e.g. Hermes with its own providers): nothing to
    filter on, the agent's own list and names stay as they are."""
    from app.services import agent_chat_input

    options = [
        {"value": "custom/glm-5.3", "name": "GLM 5.3"},
        {"value": "openrouter/kimi-k3", "name": "Kimi K3"},
    ]
    _use_state(monkeypatch, _state("custom/glm-5.3", options))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [(o["command"], o["label"]) for o in caps["modelOptions"]] == [
        ("custom/glm-5.3", "GLM 5.3"),
        ("openrouter/kimi-k3", "Kimi K3"),
    ]


async def test_missing_name_falls_back_to_the_part_after_the_provider(acp_agent, monkeypatch):
    from app.services import agent_chat_input

    _use_state(monkeypatch, _state("lm-studio/qwen3-8b", [{"value": "lm-studio/qwen3-8b"}]))
    caps = await agent_chat_input.model_options_capabilities(acp_agent)
    assert [o["label"] for o in caps["modelOptions"]] == ["qwen3-8b"]


async def test_set_model_refuses_a_hidden_builtin_model(acp_agent, monkeypatch):
    """The picker and the direct switch share one offer: a model the picker
    hides (it cannot run) is refused here too."""
    from app.services import agent_chat_input

    calls: list[tuple] = []

    class _Transport:  # never a real docker exec from a test
        async def config(self, id: str, value: str) -> dict:
            calls.append((id, value))
            return {"ok": True}

    monkeypatch.setattr(agent_chat_input, "transport_for", lambda agent: _Transport())
    _use_state(monkeypatch, _state("mc-openai/DeepSeek-V4.1-Flash-1017", _LIVE_OPTIONS))
    with pytest.raises(ValueError):
        await agent_chat_input.set_model(acp_agent, "openai/o3")
    assert calls == []

    await agent_chat_input.set_model(acp_agent, "mc-openai/DeepSeek-V4.1-Flash-1017")
    assert calls == [("model", "mc-openai/DeepSeek-V4.1-Flash-1017")]


@pytest.mark.parametrize("name", ["MC model", "DeepSeek-V4.1-Flash-1017", "Qwen/Qwen3.8-27B-FP8", "glm-5.1:cloud"])
@pytest.mark.parametrize("suffix,level", [(" · ◒ high", "high"), ("", "off")])
def test_tui_status_line_still_reads_the_thinking_level_with_a_real_name(name, suffix, level):
    """The native TUI prints the same name in its status line; the effort
    parser must not depend on the old placeholder."""
    from app.services.omp_chat import status_line_thinking_level

    pane = f"╭── π  > ⬢ {name}{suffix} > 📁 /workspace > ◫ 5.9%/500K ⟲ ▶──╮\n"
    assert status_line_thinking_level(pane) == level
