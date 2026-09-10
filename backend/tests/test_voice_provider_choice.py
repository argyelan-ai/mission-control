"""Tests for jarvis_core.voice_provider.resolve_voice_choice (ADR-082).

Deliberately no livekit import anywhere in this file or in voice_provider.py
itself — that split is the whole point (see module docstring): the decision
of WHICH provider/model/voice to use must be testable in the ordinary backend
test job, not skip silently because livekit isn't installed here.
"""
from __future__ import annotations

import pytest

from jarvis_core.voice_provider import classify_voice_api, resolve_voice_choice


def _env(**overrides) -> dict[str, str]:
    base = {
        "OPENAI_API_KEY": "sk-openai-test",
        "XAI_API_KEY": "xai-test",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


# ── MC binding wins when it is a real one ───────────────────────────────────


def test_mc_binding_is_used_when_present():
    cfg = {"provider": "xai", "model": "grok-voice-think-fast-1.0", "voice_id": None, "runtime_slug": "voice-xai"}
    choice = resolve_voice_choice(cfg, env=_env())

    assert choice.provider == "xai"
    assert choice.model == "grok-voice-think-fast-1.0"
    assert choice.source == "mc"


def test_mc_fallback_default_is_not_treated_as_a_real_binding():
    """voice_runtime.resolve_voice_config()'s fail-soft default names
    provider="openai" even with nothing bound — runtime_slug is None then.
    That must NOT look like an operator chose openai; the env decides."""
    cfg = {"provider": "openai", "model": None, "voice_id": None, "runtime_slug": None}
    choice = resolve_voice_choice(cfg, env=_env(VOICE_PROVIDER="xai"))

    assert choice.provider == "xai"
    assert choice.source == "env"


def test_no_mc_response_at_all_falls_back_to_env():
    choice = resolve_voice_choice(None, env=_env(VOICE_PROVIDER="openai", VOICE_MODEL="gpt-realtime-2.1"))

    assert choice.provider == "openai"
    assert choice.model == "gpt-realtime-2.1"
    assert choice.source == "env"


# ── Unknown provider ─────────────────────────────────────────────────────


def test_unknown_mc_provider_falls_back_to_env_default():
    """An image older than the backend might not know a provider name MC
    sends — never pass it through to the plugin factory."""
    cfg = {"provider": "anthropic-voice-nonsense", "model": None, "voice_id": None, "runtime_slug": "x"}
    choice = resolve_voice_choice(cfg, env=_env())

    assert choice.provider == "openai"
    assert choice.source == "env-fallback"


# ── Missing key on the chosen arm ───────────────────────────────────────


def test_missing_key_on_chosen_arm_switches_to_the_other():
    cfg = {"provider": "xai", "model": "grok-voice-think-fast-1.0", "voice_id": None, "runtime_slug": "voice-xai"}
    choice = resolve_voice_choice(cfg, env=_env(XAI_API_KEY=""))

    assert choice.provider == "openai"
    assert choice.source == "key-fallback"
    # The model belonged to the xai arm — it must not leak onto openai.
    assert choice.model != "grok-voice-think-fast-1.0"


def test_no_key_anywhere_raises():
    cfg = {"provider": "openai", "model": None, "voice_id": None, "runtime_slug": "voice-openai"}
    with pytest.raises(RuntimeError):
        resolve_voice_choice(cfg, env=_env(OPENAI_API_KEY="", XAI_API_KEY=""))


# ── Voice + model isolation per arm ──────────────────────────────────────


def test_voice_id_from_mc_is_used():
    cfg = {"provider": "openai", "model": None, "voice_id": "cedar", "runtime_slug": "voice-openai"}
    choice = resolve_voice_choice(cfg, env=_env())

    assert choice.voice == "cedar"


def test_voice_id_falls_back_to_per_arm_env_then_hardcoded_default():
    cfg = None
    choice = resolve_voice_choice(cfg, env=_env(VOICE_PROVIDER="xai", VOICE_XAI_VOICE_ID="ara-custom"))
    assert choice.voice == "ara-custom"

    choice2 = resolve_voice_choice(cfg, env=_env(VOICE_PROVIDER="xai"))
    assert choice2.voice == "ara"  # hardcoded xai default


def test_model_defaults_to_plugin_default_when_nothing_names_one():
    cfg = None
    choice = resolve_voice_choice(cfg, env=_env(VOICE_PROVIDER="xai"))

    assert choice.model is None  # xai plugin picks its own default


# ── api classification (ADR-082 follow-up: OpenAI Live API) ────────────────
#
# gpt-live-1 speaks OpenAI's Live API (v1/live/sessions), NOT the Realtime
# API this worker's livekit plugin builds. Same provider, disjoint wire
# protocol — checked live against OpenAI's docs 2026-09-10.


def test_classify_voice_api_realtime_default():
    assert classify_voice_api("openai", "gpt-realtime-2.1") == "realtime"
    assert classify_voice_api("openai", None) == "realtime"
    assert classify_voice_api("xai", "grok-voice-think-fast-1.0") == "realtime"


def test_classify_voice_api_live_model():
    assert classify_voice_api("openai", "gpt-live-1") == "live"
    assert classify_voice_api("openai", "GPT-Live-1") == "live"  # case-insensitive


def test_classify_voice_api_live_prefix_is_openai_only():
    """A hypothetical xai model that merely CONTAINS "live" must not trip
    the rule — only OpenAI is known to have shipped a disjoint Live API."""
    assert classify_voice_api("xai", "grok-live-fast") == "realtime"


def test_classify_voice_api_unknown_model_defaults_to_realtime():
    """A model name the rule has never heard of (typo, brand-new release,
    future SKU) must default to "realtime" — the api this worker already
    knows how to build — rather than an unrecognized value that would make
    every future model silently unusable until this function is updated."""
    assert classify_voice_api("openai", "gpt-9000-turbo-ultra") == "realtime"
    assert classify_voice_api("openai", "some-random-model-xyz") == "realtime"


def test_resolve_voice_choice_reflects_a_live_binding():
    cfg = {"provider": "openai", "model": "gpt-live-1", "voice_id": None, "runtime_slug": "voice-openai"}
    choice = resolve_voice_choice(cfg, env=_env())

    assert choice.api == "live"
    assert choice.provider == "openai"
    assert choice.model == "gpt-live-1"


def test_resolve_voice_choice_default_api_is_realtime():
    choice = resolve_voice_choice(None, env=_env())
    assert choice.api == "realtime"


def test_key_fallback_reclassifies_api_on_the_new_arm():
    """If MC bound a (hypothetical) live-speaking xai model and the xai key is
    missing, the fallback lands on openai's realtime default — api must
    reflect the ARM THAT WAS ACTUALLY CHOSEN, not the original request."""
    cfg = {"provider": "xai", "model": "grok-live-fast", "voice_id": None, "runtime_slug": "voice-xai"}
    choice = resolve_voice_choice(cfg, env=_env(XAI_API_KEY=""))

    assert choice.provider == "openai"
    assert choice.api == "realtime"
