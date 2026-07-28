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
    # 2026-07-28: openclaude/omp joined the registry for the same reason claude
    # did (wizard visibility + runtime switching + model propagation), and for
    # the same reason own NO bootstrap — provisioning stays on the generic
    # host_provisioning staging path.
    for generic in ("openclaude", "omp"):
        adapter = get_adapter(generic)
        assert adapter is not None, f"{generic} must be a registered host harness"
        assert adapter.supports_bootstrap is False
    # "openclaw" is the retired Phase-29 gateway runtime (ADR-039) — it can
    # never come back, so it is the stable stand-in for a genuinely unknown
    # harness. get_adapter must return None rather than guess.
    assert get_adapter("openclaw") is None
    assert get_adapter("nope") is None
