import uuid
import pytest
from app.models.runtime import Runtime
from app.services.harness_compat import (
    HARNESSES,
    derive_harness,
    incompat_reason,
    is_compatible,
    runtime_protocol,
)


def _rt(slug="test-rt", runtime_type="vllm_docker", **kw):
    return Runtime(
        id=uuid.uuid4(), slug=slug, display_name=slug,
        runtime_type=runtime_type, endpoint="http://test", enabled=True, **kw,
    )


def test_protocol_anthropic_by_slug_prefix():
    assert runtime_protocol(_rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")) == "anthropic"

def test_protocol_openai_types():
    for t in ("vllm_docker", "lmstudio", "openai_compatible", "unsloth", "cloud", "omp"):
        assert runtime_protocol(_rt(runtime_type=t)) == "openai", t

def test_protocol_slug_prefix_is_exact_anthropic_claude():
    """The slug arm must match the exact legacy prefix 'anthropic-claude-', not
    a broad 'anthropic-'. An 'anthropic-proxy-*' OpenAI-compatible shim on an
    openai runtime_type must classify as openai, not anthropic (regression)."""
    rt = _rt(slug="anthropic-proxy-x", runtime_type="openai_compatible")
    assert runtime_protocol(rt) == "openai"

def test_protocol_unknown_type_is_none():
    assert runtime_protocol(_rt(runtime_type="hermes")) is None
    assert runtime_protocol(None) is None

def test_matrix_v1():
    openai_rt = _rt(runtime_type="cloud")
    anthropic_rt = _rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")
    assert is_compatible("omp", openai_rt) is True
    assert is_compatible("openclaude", openai_rt) is True
    assert is_compatible("claude", openai_rt) is False
    assert is_compatible("claude", anthropic_rt) is True
    assert is_compatible("omp", anthropic_rt) is False
    assert is_compatible("openclaude", anthropic_rt) is False
    assert is_compatible(None, openai_rt) is False
    assert is_compatible("omp", None) is False

def test_incompat_reason_german_and_none_when_ok():
    openai_rt = _rt(runtime_type="cloud")
    assert incompat_reason("omp", openai_rt) is None
    reason = incompat_reason("claude", openai_rt)
    assert reason and "Claude Code" in reason

def test_derive_harness_legacy():
    assert derive_harness(_rt(runtime_type="omp")) == "omp"
    assert derive_harness(_rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth")) == "claude"
    assert derive_harness(_rt(runtime_type="lmstudio")) == "openclaude"
    assert derive_harness(_rt(runtime_type="hermes")) is None
    assert derive_harness(None) is None

def test_harnesses_tuple():
    assert HARNESSES == ("claude", "openclaude", "omp", "kimi")


def test_kimi_protocol_fixed():
    """Kimi ist protocol-fixed wie grok: nur runtime_type 'kimi' passt."""
    from app.services.harness_compat import is_compatible, runtime_protocol

    kimi_rt = _rt(slug="kimi-cloud", runtime_type="kimi")
    assert runtime_protocol(kimi_rt) == "kimi"
    assert is_compatible("kimi", kimi_rt)
    # Kein anderes Harness akzeptiert eine kimi-Runtime …
    assert not is_compatible("claude", kimi_rt)
    assert not is_compatible("openclaude", kimi_rt)
    assert not is_compatible("omp", kimi_rt)
    # … und kimi akzeptiert keine fremden Protokolle.
    assert not is_compatible("kimi", _rt(runtime_type="lmstudio"))
    assert not is_compatible("kimi", _rt(slug="anthropic-claude-opus", runtime_type="anthropic_oauth"))
    assert derive_harness(_rt(runtime_type="kimi")) == "kimi"
