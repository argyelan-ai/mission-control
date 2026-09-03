"""jarvis_core/voice_provider.py — which provider/model/voice a call uses.

These run in the ORDINARY backend job. That is the point of the module: the
same rules previously lived next to the livekit plugin construction, so their
tests skipped silently wherever livekit was absent — including CI. Ten tests,
green, proving nothing. If anyone reverses "MC beats env" now, this job fails.

The rule under test: Jarvis does not go silent. Every unclear state resolves to
something that can still talk; only a total absence of keys raises.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jarvis_core.voice_provider import PROVIDERS, resolve_voice_choice  # noqa: E402

BOTH_KEYS = {"OPENAI_API_KEY": "sk-test", "XAI_API_KEY": "xai-test"}


def _env(**kw) -> dict[str, str]:
    return {**BOTH_KEYS, **kw}


# ── MC beats env — the point of the rebuild ───────────────────────────────


def test_mc_provider_beats_env():
    """env always says openai (it is the compose default). If env won, the
    runtime picker would be decoration."""
    choice = resolve_voice_choice(provider="xai", env=_env(VOICE_PROVIDER="openai"))

    assert choice.provider == "xai"
    assert choice.source == "mc"


def test_mc_model_is_passed_through():
    choice = resolve_voice_choice(provider="openai", model="gpt-realtime-2", env=_env())

    assert choice.model == "gpt-realtime-2"


def test_env_is_used_when_mc_says_nothing():
    choice = resolve_voice_choice(env=_env(VOICE_PROVIDER="xai"))

    assert choice.provider == "xai"
    assert choice.source == "env"


def test_default_is_openai_with_its_default_model():
    choice = resolve_voice_choice(env=_env())

    assert (choice.provider, choice.model, choice.voice) == ("openai", "gpt-realtime-2.1", "marin")


def test_provider_is_case_insensitive():
    assert resolve_voice_choice(provider="OpenAI", env=_env()).provider == "openai"


def test_empty_model_from_mc_is_treated_as_absent():
    """resolve_voice_config returns None for a blank model_identifier, but a
    stray empty string must not become model="" on the constructor."""
    choice = resolve_voice_choice(provider="openai", model="", env=_env())

    assert choice.model == "gpt-realtime-2.1"


# ── A model never travels to a foreign arm ────────────────────────────────
#
# Every case here ends the same way if it regresses: the provider rejects an
# unknown model name at connect, VoiceAssistant.__init__ raises, session.start
# never happens — a silent Jarvis, for a reason invisible from the phone.


def test_key_fallback_drops_the_model_mc_named():
    choice = resolve_voice_choice(
        provider="xai", model="grok-voice-fast-1.0",
        env={"OPENAI_API_KEY": "sk-test", "VOICE_PROVIDER": "openai"},
    )

    assert choice.provider == "openai"
    assert choice.model == "gpt-realtime-2.1"
    assert choice.source == "key-fallback"


def test_unknown_provider_fallback_drops_the_model_too():
    """The gap the review found: the key fallback cleared the model, the
    unknown-provider fallback did not. Reachable as soon as the backend knows
    an arm this image does not — exactly the "voice_local is one entry plus one
    seed row" path the ADR proposes.
    """
    choice = resolve_voice_choice(
        provider="local", model="parakeet-v3", env=_env(VOICE_PROVIDER="openai"),
    )

    assert choice.provider == "openai"
    assert choice.model == "gpt-realtime-2.1", "a foreign model must not survive an arm switch"


def test_env_model_does_not_follow_across_arms():
    """The second half of the same gap: VOICE_MODEL belongs to the env arm. On
    a key fallback away from it, re-reading VOICE_MODEL put the foreign name
    back — undoing the clearing one line above it.
    """
    choice = resolve_voice_choice(
        env={
            "OPENAI_API_KEY": "sk-test",
            "VOICE_PROVIDER": "xai",
            "VOICE_MODEL": "grok-voice-fast-1.0",
        },
    )

    assert choice.provider == "openai"
    assert choice.model == "gpt-realtime-2.1"


def test_env_model_applies_to_its_own_arm():
    choice = resolve_voice_choice(
        env=_env(VOICE_PROVIDER="xai", VOICE_MODEL="grok-voice-fast-1.0"),
    )

    assert (choice.provider, choice.model) == ("xai", "grok-voice-fast-1.0")


def test_xai_without_a_model_leaves_the_plugin_default():
    """None, not "" — the plugin treats NOT_GIVEN and None differently."""
    assert resolve_voice_choice(provider="xai", env=_env()).model is None


# ── Never silent ──────────────────────────────────────────────────────────


def test_unknown_provider_does_not_raise():
    assert resolve_voice_choice(provider="elevenlabs", env=_env()).provider in PROVIDERS


def test_unknown_env_provider_falls_back_to_openai():
    assert resolve_voice_choice(env=_env(VOICE_PROVIDER="elevenlabs")).provider == "openai"


def test_missing_key_switches_arms():
    choice = resolve_voice_choice(provider="xai", env={"OPENAI_API_KEY": "sk-test"})

    assert choice.provider == "openai"


def test_blank_key_counts_as_missing():
    """An empty OPENAI_API_KEY in .env is not a configured key; treating it as
    one would fail at connect instead of falling back."""
    choice = resolve_voice_choice(provider="openai", env={"OPENAI_API_KEY": "  ", "XAI_API_KEY": "xai-test"})

    assert choice.provider == "xai"


def test_no_key_at_all_raises():
    """The one remaining silent case, and it is honest."""
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY|XAI_API_KEY"):
        resolve_voice_choice(provider="openai", env={"VOICE_PROVIDER": "openai"})


# ── Voices are per arm ────────────────────────────────────────────────────


def test_voice_defaults_are_per_arm():
    assert resolve_voice_choice(provider="openai", env=_env()).voice == "marin"
    assert resolve_voice_choice(provider="xai", env=_env()).voice == "ara"


def test_voices_do_not_leak_between_arms():
    env = _env(VOICE_OPENAI_VOICE_ID="cedar")

    assert resolve_voice_choice(provider="xai", env=env).voice == "ara"
    assert resolve_voice_choice(provider="openai", env=env).voice == "cedar"


# ── The log line the live gate reads ──────────────────────────────────────


def test_log_line_carries_provider_model_and_source():
    """Before this there was no such line: 976 log lines, zero occurrences of
    "gpt-realtime". Without it a switch cannot be proven to have arrived."""
    line = resolve_voice_choice(provider="openai", model="gpt-realtime-2.1", env=_env()).as_log()

    assert "provider=openai" in line
    assert "model=gpt-realtime-2.1" in line
    assert "source=mc" in line
