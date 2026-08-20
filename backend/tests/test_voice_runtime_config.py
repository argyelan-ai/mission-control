"""Jarvis' voice provider is a runtime binding, like every other agent (ADR-074).

Why these tests exist:

    Before this, WHICH provider Jarvis spoke to lived only in the voice-worker's
    container env. Changing it meant editing docker-compose and rebuilding, and
    nothing in MC showed the current state. ADR-060 had ruled the DB out because
    there was no host-switch machinery yet — HOST_ADAPTERS closed that gap.

The load-bearing property is that a voice runtime is NOT an openai runtime.
Both talk to api.openai.com, but the wire protocol is the realtime speech
socket, not chat completions. If the classification ever fell through to
"openai", every openai-speaking CLI harness (openclaude, omp, hermes) would
suddenly look compatible with Jarvis' voice rows and the picker would offer
nonsense bindings.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.runtime import Runtime
from app.services.harness_compat import (
    VOICE_RUNTIME_TYPES,
    is_compatible,
    runtime_protocol,
)
from app.services.runtime_naming import CURATED_RUNTIME_TYPES

SEED_PATH = Path(__file__).resolve().parents[1] / "config" / "runtimes.json"


def _rt(runtime_type: str, slug: str = "probe", **kw) -> Runtime:
    kw.setdefault("display_name", "Probe")
    kw.setdefault("model_identifier", "some-model")
    return Runtime(slug=slug, runtime_type=runtime_type, **kw)


def _seed_rows() -> list[dict]:
    return json.loads(SEED_PATH.read_text())


# ── Protocol classification ───────────────────────────────────────────────


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_runtime_types_map_to_the_voice_protocol(runtime_type: str):
    assert runtime_protocol(_rt(runtime_type)) == "voice"


def test_jarvis_accepts_voice_runtimes():
    for runtime_type in VOICE_RUNTIME_TYPES:
        assert is_compatible("jarvis", _rt(runtime_type)) is True


@pytest.mark.parametrize("harness", ["claude", "openclaude", "omp", "hermes", "grok", "kimi"])
def test_no_cli_harness_may_bind_a_voice_runtime(harness: str):
    """The regression this guards: voice_openai falling through to "openai".

    openclaude/omp/hermes all speak the openai protocol. If the voice check
    were removed (or placed after the _OPENAI_TYPES check), voice_openai would
    classify as "openai" and every one of them would report compatible.
    """
    assert is_compatible(harness, _rt("voice_openai")) is False
    assert is_compatible(harness, _rt("voice_xai")) is False


def test_jarvis_may_not_bind_a_chat_runtime():
    """The reverse direction — Jarvis is not a CLI and cannot run a chat model."""
    assert is_compatible("jarvis", _rt("vllm_docker")) is False
    assert is_compatible("jarvis", _rt("cloud")) is False
    assert is_compatible("jarvis", _rt("grok")) is False


# ── Seed rows ─────────────────────────────────────────────────────────────


def test_seed_carries_one_row_per_voice_runtime_type():
    by_type = {r["runtime_type"]: r for r in _seed_rows() if r["runtime_type"] in VOICE_RUNTIME_TYPES}
    assert set(by_type) == set(VOICE_RUNTIME_TYPES), (
        "every declared voice runtime type needs a seed row, otherwise the "
        "provider is selectable in code but invisible in the picker"
    )


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_seed_rows_carry_a_model_identifier(runtime_type: str):
    """Empty model_identifier makes the switch probe the live endpoint.

    ensure_runtime_model_identifier() reaches out to the runtime's endpoint to
    discover a model when the row has none. For api.openai.com that is a
    network call on every switch — slow, and it fails closed without a key.
    """
    row = next(r for r in _seed_rows() if r["runtime_type"] == runtime_type)
    assert (row.get("model_identifier") or "").strip(), (
        f"seed row '{row['id']}' must name its model explicitly"
    )


@pytest.mark.parametrize("runtime_type", sorted(VOICE_RUNTIME_TYPES))
def test_voice_display_names_are_protected_from_the_naming_rule(runtime_type: str):
    """api.openai.com is a known provider host — without curation the seeder
    would silently rename the row after it, and the picker would show two
    entries that read like chat models rather than voice arms."""
    assert runtime_type in CURATED_RUNTIME_TYPES


def test_voice_rows_are_not_single_instance():
    """single_instance would let only one agent hold the binding. Jarvis is
    alone today, but the flag also drives a 422 on the create path — no reason
    to inherit grok-cloud's constraint here."""
    for row in _seed_rows():
        if row["runtime_type"] in VOICE_RUNTIME_TYPES:
            assert row.get("single_instance") is False
