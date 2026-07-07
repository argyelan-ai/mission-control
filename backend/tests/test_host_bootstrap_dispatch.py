import pytest
from app.models.runtime import Runtime
from app.models.agent import Agent


@pytest.mark.asyncio
async def test_incompatible_host_harness_is_rejected():
    """claude harness cannot bind an openai-protocol runtime (no shim)."""
    from app.services.harness_compat import is_compatible
    rt = Runtime(slug="vllm", display_name="vllm", runtime_type="vllm_docker",
                 endpoint="http://192.0.2.10:8000/v1", model_identifier="m", enabled=True)
    assert is_compatible("hermes", rt) is True
    assert is_compatible("claude", rt) is False


@pytest.mark.asyncio
async def test_unknown_host_harness_has_no_adapter():
    from app.services.host_harness_adapter import get_adapter
    assert get_adapter("claude") is None   # claude host adapter is design-only (Phase 2)
    assert get_adapter("hermes") is not None
