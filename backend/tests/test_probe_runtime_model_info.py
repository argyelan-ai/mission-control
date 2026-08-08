"""``probe_runtime_model_info`` — model id AND served context window (PR9).

No network: every case runs against an httpx.MockTransport, mirroring the
pattern in tests/test_provider_model_catalog.py.

The pairing rule is the point of this suite. ``/v1/models`` can list several
entries, and ``select_probed_model`` deliberately does NOT always pick the
first one (it keeps the currently bound model if the engine still serves it).
Reading ``max_model_len`` off "the first entry" instead of "the picked entry"
would therefore pair a model with another model's window — a subtle version of
exactly the stale-window bug this feature exists to fix.
"""
from __future__ import annotations

import httpx
import pytest

from app.models.runtime import Runtime
from app.services import agent_runtime_switch as ars


@pytest.fixture
def mock_models_endpoint(monkeypatch):
    """Serve one ``/v1/models`` payload; return the list of requested URLs."""
    seen: list[str] = []

    def _install(payload, *, status_code=200):
        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(status_code, json=payload)

        transport = httpx.MockTransport(_handler)
        original = httpx.AsyncClient

        def fake_async_client(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", fake_async_client)
        return seen

    return _install


def _rt(model: str | None = None) -> Runtime:
    return Runtime(
        slug="probe-rt", display_name="probe-rt", runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", model_identifier=model, enabled=True,
    )


@pytest.mark.asyncio
async def test_reads_max_model_len_next_to_the_model_id(mock_models_endpoint):
    """The live vLLM shape from 08.08. — the response DID carry 262144."""
    mock_models_endpoint(
        {"data": [{"id": "deepseek-v4-flash-0731-spark", "max_model_len": 262144}]}
    )
    probed = await ars.probe_runtime_model_info(_rt())
    assert probed.model_id == "deepseek-v4-flash-0731-spark"
    assert probed.context_len == 262144


@pytest.mark.asyncio
async def test_window_comes_from_the_PICKED_entry_not_the_first(mock_models_endpoint):
    """The bound model is still served, so select_probed_model keeps it (entry
    two) — its window must come with it, not entry one's."""
    mock_models_endpoint({"data": [
        {"id": "some-other-model", "max_model_len": 8192},
        {"id": "deepseek-v4-flash-0731-spark", "max_model_len": 262144},
    ]})
    probed = await ars.probe_runtime_model_info(
        _rt(model="deepseek-v4-flash-0731-spark")
    )
    assert probed.model_id == "deepseek-v4-flash-0731-spark"
    assert probed.context_len == 262144


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["context_length", "max_context_length"])
async def test_accepts_the_lmstudio_style_spellings(mock_models_endpoint, key):
    mock_models_endpoint({"data": [{"id": "qwen-local", key: 131072}]})
    assert (await ars.probe_runtime_model_info(_rt())).context_len == 131072


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [0, -1, "262144", None, True],
    ids=["zero", "negative", "string", "null", "bool"],
)
async def test_implausible_windows_are_reported_as_unknown(
    mock_models_endpoint, value
):
    """A shim quirk must read as "did not say", never as a real window. ``True``
    is in here on purpose: bools are ints in Python, and a truthy flag silently
    becoming a 1-token context window is the kind of bug that only shows up in
    production."""
    mock_models_endpoint({"data": [{"id": "quirky-shim", "max_model_len": value}]})
    probed = await ars.probe_runtime_model_info(_rt())
    assert probed.model_id == "quirky-shim"
    assert probed.context_len is None


@pytest.mark.asyncio
async def test_endpoint_that_omits_the_window_still_reports_the_model(
    mock_models_endpoint,
):
    """Cloud providers list no window. That must not cost us the model id."""
    mock_models_endpoint({"data": [{"id": "glm-5.1", "object": "model"}]})
    probed = await ars.probe_runtime_model_info(_rt())
    assert probed.model_id == "glm-5.1"
    assert probed.context_len is None


@pytest.mark.asyncio
async def test_nothing_chat_capable_reports_neither(mock_models_endpoint):
    mock_models_endpoint(
        {"data": [{"id": "text-embedding-3-large", "max_model_len": 8192}]}
    )
    assert await ars.probe_runtime_model_info(_rt()) == ars.ProbedModel(None, None)


@pytest.mark.asyncio
async def test_unreachable_endpoint_reports_neither(mock_models_endpoint):
    mock_models_endpoint({"error": "nope"}, status_code=503)
    assert await ars.probe_runtime_model_info(_rt()) == ars.ProbedModel(None, None)


@pytest.mark.asyncio
async def test_runtime_without_endpoint_is_not_probed(mock_models_endpoint):
    seen = mock_models_endpoint({"data": [{"id": "x", "max_model_len": 1}]})
    rt = _rt()
    rt.endpoint = None
    assert await ars.probe_runtime_model_info(rt) == ars.ProbedModel(None, None)
    assert seen == []


@pytest.mark.asyncio
async def test_legacy_wrapper_keeps_its_string_contract(mock_models_endpoint):
    """Every caller that only wants identity keeps working unchanged."""
    mock_models_endpoint({"data": [{"id": "glm-5.1", "max_model_len": 131072}]})
    assert await ars.probe_runtime_model(_rt()) == "glm-5.1"
