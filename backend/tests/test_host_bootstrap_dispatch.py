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
    # 2026-07-25 (model sanitation): claude IS registered now — that registry
    # entry is what lets runtime.model_identifier propagate to boss-host. It
    # deliberately owns NO bootstrap, so provisioning still goes through the
    # generic host_provisioning staging path.
    claude = get_adapter("claude")
    assert claude is not None
    assert claude.supports_bootstrap is False
    assert get_adapter("hermes") is not None
    assert get_adapter("openclaude") is None  # genuinely unknown host harness
