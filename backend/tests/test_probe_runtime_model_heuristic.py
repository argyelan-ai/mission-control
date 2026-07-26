"""Issue #161 — chat-capable model selection heuristic for `/models` probes.

`probe_runtime_model` used to take `data[0].id` blindly. LM Studio routinely
serves an embedding model alongside the chat model with no guaranteed order,
so the response's position decided what became a runtime's
`model_identifier` — observed live: `nemotron-super` and `qwen-coder-lms`
both landed on `text-embedding-nomic-embed-text-v1.5`.

Coverage:
  - select_probed_model (pure function): denylist filtering, current-model
    stability (including when current is NOT first in the list — the actual
    bug), first-candidate fallback, all-embedding → None, empty → None,
    non-string/whitespace robustness.
  - The exact regression: embedding-first list → chat model wins.
  - The exact stability guarantee: current model is kept even when a
    different chat model appears earlier in the list.
  - probe_runtime_model end-to-end against a mocked httpx.AsyncClient.
  - Denylist is not an accidental allowlist — plausible new model names must
    survive.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.runtime import Runtime
from app.services.agent_runtime_switch import (
    probe_runtime_model,
    select_probed_model,
)


# ── select_probed_model — pure function ────────────────────────────────────


def test_select_probed_model_filters_embedding_models():
    result = select_probed_model(
        ["text-embedding-nomic-embed-text-v1.5", "qwen3-coder-next"],
        current=None,
    )
    assert result == "qwen3-coder-next"


def test_select_probed_model_regression_embedding_first():
    """The exact bug reported live: embedding model is data[0]."""
    ids = ["text-embedding-nomic-embed-text-v1.5", "qwen3-coder-next"]
    assert select_probed_model(ids, current=None) == "qwen3-coder-next"


def test_select_probed_model_keeps_current_when_not_first():
    """The stability rule: a probe confirms, it does not re-pick.

    If the runtime's current model is still being served — even buried
    behind another chat-capable candidate — the probe must not silently
    repoint the runtime at whatever now sits at data[0].
    """
    ids = ["some-other-chat-model", "qwen3-coder-next"]
    result = select_probed_model(ids, current="qwen3-coder-next")
    assert result == "qwen3-coder-next"


def test_select_probed_model_keeps_current_at_position_zero_too():
    ids = ["qwen3-coder-next", "some-other-chat-model"]
    result = select_probed_model(ids, current="qwen3-coder-next")
    assert result == "qwen3-coder-next"


def test_select_probed_model_falls_back_to_first_chat_capable_without_current():
    ids = ["chat-model-a", "chat-model-b"]
    assert select_probed_model(ids, current=None) == "chat-model-a"


def test_select_probed_model_falls_back_when_current_no_longer_served():
    ids = ["chat-model-a", "chat-model-b"]
    result = select_probed_model(ids, current="chat-model-gone")
    assert result == "chat-model-a"


def test_select_probed_model_all_embedding_returns_none():
    ids = ["text-embedding-nomic-embed-text-v1.5", "bge-rerank-base"]
    assert select_probed_model(ids, current=None) is None


def test_select_probed_model_empty_list_returns_none():
    assert select_probed_model([], current=None) is None
    assert select_probed_model([], current="whatever") is None


def test_select_probed_model_robust_to_whitespace_and_non_strings():
    ids = ["  ", "", None, 42, "  qwen3-coder-next  "]
    result = select_probed_model(ids, current=None)  # type: ignore[arg-type]
    assert result == "qwen3-coder-next"


def test_select_probed_model_current_matches_after_stripping():
    ids = ["chat-model-a", "  qwen3-coder-next  "]
    result = select_probed_model(ids, current="qwen3-coder-next")
    assert result == "qwen3-coder-next"


def test_select_probed_model_denylist_not_an_allowlist_for_new_models():
    """A denylist must reject known non-chat families, not gatekeep on an
    allowlist of already-known chat names — otherwise every newly released
    model name gets filtered out until the code is updated.
    """
    ids = [
        "claude-opus-5",
        "k3-256k",
        "poolside/Laguna-S-2.1-NVFP4",
        "text-embedding-nomic-embed-text-v1.5",
    ]
    result = select_probed_model(ids, current=None)
    assert result == "claude-opus-5"

    # Each individually should also survive when it's the only candidate.
    for plausible in ("claude-opus-5", "k3-256k", "poolside/Laguna-S-2.1-NVFP4"):
        assert select_probed_model([plausible], current=None) == plausible


def test_select_probed_model_filters_rerank_whisper_tts_clip_stable_diffusion():
    ids = [
        "bge-rerank-base",
        "whisper-large-v3",
        "coqui-tts-v2",
        "elevenlabs-tts",
        "stable-diffusion-xl",
        "clip-vit-base",
        "qwen3-coder-next",
    ]
    assert select_probed_model(ids, current=None) == "qwen3-coder-next"


# ── probe_runtime_model — end-to-end against mocked httpx ─────────────────


def _mock_httpx_get(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


def _rt(*, model_identifier=None, endpoint="http://localhost:1234/v1") -> Runtime:
    return Runtime(
        slug="probe-rt",
        display_name="PROBE-RT",
        runtime_type="lmstudio",
        endpoint=endpoint,
        model_identifier=model_identifier,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_probe_runtime_model_mixed_list_picks_chat_model():
    runtime = _rt(model_identifier=None)
    payload = {
        "data": [
            {"id": "text-embedding-nomic-embed-text-v1.5"},
            {"id": "qwen3-coder-next"},
        ]
    }
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(payload)):
        result = await probe_runtime_model(runtime)
    assert result == "qwen3-coder-next"


@pytest.mark.asyncio
async def test_probe_runtime_model_only_embedding_returns_none():
    runtime = _rt(model_identifier=None)
    payload = {"data": [{"id": "text-embedding-nomic-embed-text-v1.5"}]}
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(payload)):
        result = await probe_runtime_model(runtime)
    assert result is None


@pytest.mark.asyncio
async def test_probe_runtime_model_non_200_returns_none():
    runtime = _rt(model_identifier=None)
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get({}, status_code=500)):
        result = await probe_runtime_model(runtime)
    assert result is None


@pytest.mark.asyncio
async def test_probe_runtime_model_keeps_current_over_new_first_candidate():
    runtime = _rt(model_identifier="qwen3-coder-next")
    payload = {
        "data": [
            {"id": "some-other-chat-model"},
            {"id": "qwen3-coder-next"},
        ]
    }
    with patch("httpx.AsyncClient", return_value=_mock_httpx_get(payload)):
        result = await probe_runtime_model(runtime)
    assert result == "qwen3-coder-next"


@pytest.mark.asyncio
async def test_probe_runtime_model_no_endpoint_returns_none():
    runtime = _rt(model_identifier=None, endpoint=None)
    result = await probe_runtime_model(runtime)
    assert result is None
