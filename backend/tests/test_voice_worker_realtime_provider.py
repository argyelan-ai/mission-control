"""Tests for voice_worker/main.py `_build_realtime_model()` — ADR-060 + ADR-074.

Which provider Jarvis speaks to now comes from the runtime binding in MC,
handed in per call; the env vars remain as the emergency default for when the
backend does not answer. These tests pin both directions of that precedence.

The governing rule is **Jarvis does not go silent**. Every unclear state falls
back to something that can still talk — a wrong provider is an annoyance, a
dead voice assistant is an outage. Only a total absence of API keys raises.
That is a deliberate reversal of the original fail-fast behaviour, so the cases
that used to raise are asserted NOT to raise here.

Running these: livekit must be importable, otherwise every test below skips
silently. In the backend venv it is not installed — run them against the voice
image instead:

    docker run --rm \
      -v "$PWD/voice_worker:/w/voice_worker:ro" \
      -v "$PWD/jarvis_core:/w/jarvis_core:ro" \
      -v "$PWD/backend/tests/test_voice_worker_realtime_provider.py:\
/w/backend/tests/test_voice_worker_realtime_provider.py:ro" \
      -w /w mission-control-voice-worker \
      sh -c "pip -q install pytest; python -m pytest backend/tests -q -rs -p no:cacheprovider"
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# Make the voice_worker package importable in the backend test env. The
# repo layout has voice_worker/ at the top level, sibling to backend/.
VOICE_DIR = Path(__file__).resolve().parents[2] / "voice_worker"
if str(VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_DIR))


def _import_main():
    """Lazy import — livekit deps might not be installed in CI; skip
    cleanly if they're absent (same pattern as test_voice_worker_deliver)."""
    try:
        import main as voice_main  # type: ignore
    except ImportError as exc:
        pytest.skip(f"voice_worker deps not installed: {exc}")
    return voice_main


@pytest.fixture
def clean_env(monkeypatch):
    """Neutral starting point: both keys present, no overrides set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    for var in (
        "VOICE_PROVIDER", "VOICE_MODEL", "VOICE_VOICE_ID",
        "VOICE_OPENAI_VOICE_ID", "VOICE_XAI_VOICE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── MC wins over env — the point of the whole rebuild ─────────────────────


def test_provider_from_mc_wins_over_env(clean_env):
    """The load-bearing test.

    env says openai (it always will — that is the compose default). MC says
    xai. If env won, the runtime picker would be decoration: the UI would show
    Grok while Jarvis kept speaking to OpenAI.
    """
    voice = _import_main()
    clean_env.setenv("VOICE_PROVIDER", "openai")

    with patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor, \
            patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor:
        voice._build_realtime_model(provider="xai")

    xai_ctor.assert_called_once()
    openai_ctor.assert_not_called()


def test_model_from_mc_is_passed_through(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="openai", model="gpt-realtime-2")

    assert ctor.call_args.kwargs["model"] == "gpt-realtime-2"


def test_xai_model_from_mc_is_passed_through(clean_env):
    """The installed livekit-plugins-xai accepts model=, so the xai arm is
    genuinely steerable rather than informational."""
    voice = _import_main()

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="xai", model="grok-voice-fast-1.0")

    assert ctor.call_args.kwargs["model"] == "grok-voice-fast-1.0"


def test_xai_without_a_model_leaves_the_plugin_default(clean_env):
    """Passing model=None must not turn into model=None on the constructor —
    the plugin has its own default and NOT_GIVEN is not the same as None."""
    voice = _import_main()

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="xai", model=None)

    assert "model" not in ctor.call_args.kwargs


# ── env remains the fallback ──────────────────────────────────────────────


def test_env_is_used_when_mc_gives_nothing(clean_env):
    """Backend down, or nothing bound yet (today's state: runtime_id NULL)."""
    voice = _import_main()
    clean_env.setenv("VOICE_PROVIDER", "xai")

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once()


def test_default_provider_is_openai(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once_with(
        model="gpt-realtime-2.1",
        voice="marin",
        turn_detection=voice._TURN_DETECTION,
    )


def test_env_model_override_still_works(clean_env):
    voice = _import_main()
    clean_env.setenv("VOICE_MODEL", "gpt-realtime")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model()

    assert ctor.call_args.kwargs["model"] == "gpt-realtime"


def test_provider_case_insensitive(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="OpenAI")

    ctor.assert_called_once()


# ── Voices are per arm, never shared ──────────────────────────────────────


def test_voice_defaults_are_per_arm(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor:
        voice._build_realtime_model(provider="openai")
    with patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="xai")

    assert openai_ctor.call_args.kwargs["voice"] == "marin"
    assert xai_ctor.call_args.kwargs["voice"] == "ara"


def test_voices_do_not_leak_between_arms(clean_env):
    """The #333 'per-arm fields' lesson applied to voice.

    The two providers' voice names are disjoint — 'cedar' does not exist at
    xAI. A single shared VOICE_VOICE_ID meant setting a voice for one arm broke
    the other one at connect time, with an error that names neither.
    """
    voice = _import_main()
    clean_env.setenv("VOICE_OPENAI_VOICE_ID", "cedar")

    with patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="xai")
    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor:
        voice._build_realtime_model(provider="openai")

    assert xai_ctor.call_args.kwargs["voice"] == "ara"
    assert openai_ctor.call_args.kwargs["voice"] == "cedar"


def test_each_arm_reads_its_own_voice_var(clean_env):
    voice = _import_main()
    clean_env.setenv("VOICE_XAI_VOICE_ID", "Eve")

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="xai")

    assert ctor.call_args.kwargs["voice"] == "Eve"


# ── Never go silent ───────────────────────────────────────────────────────


def test_unknown_provider_falls_back_instead_of_raising(clean_env):
    """A typo in the DB, or a runtime type we do not know yet, must not end
    the call. It used to raise — that would now mean a silent Jarvis for a
    reason Mark cannot see from the phone."""
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="elevenlabs")

    ctor.assert_called_once()


def test_unknown_env_provider_falls_back_to_openai(clean_env):
    voice = _import_main()
    clean_env.setenv("VOICE_PROVIDER", "elevenlabs")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once()


def test_missing_key_falls_back_to_the_other_arm(clean_env):
    """The silent-Jarvis trap: MC says xai, but only the OpenAI key is set.

    Raising here would take the voice assistant down over a configuration
    mismatch that the other arm can absorb.
    """
    voice = _import_main()
    clean_env.delenv("XAI_API_KEY", raising=False)

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="xai")

    openai_ctor.assert_called_once()
    xai_ctor.assert_not_called()


def test_key_fallback_drops_the_foreign_model(clean_env):
    """Falling back to openai while still passing xai's model name would fail
    at connect — with an error about the model, not about the missing key."""
    voice = _import_main()
    clean_env.delenv("XAI_API_KEY", raising=False)

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="xai", model="grok-voice-fast-1.0")

    assert ctor.call_args.kwargs["model"] == "gpt-realtime-2.1"


def test_no_key_at_all_still_raises(clean_env):
    """The one remaining silent case, and it is honest: with no key there is
    nothing to fall back to."""
    voice = _import_main()
    clean_env.delenv("OPENAI_API_KEY", raising=False)
    clean_env.delenv("XAI_API_KEY", raising=False)

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY|XAI_API_KEY"):
            voice._build_realtime_model(provider="openai")

    openai_ctor.assert_not_called()
    xai_ctor.assert_not_called()


# ── The switch must be visible in the log ─────────────────────────────────


def test_provider_and_source_are_logged(clean_env, caplog):
    """Before this, the worker never logged which model it used — 976 log
    lines with zero occurrences of 'gpt-realtime'. Without this line the live
    gate has no way to prove a switch arrived, and a typo in the model name
    surfaces only as a generic connection error.
    """
    voice = _import_main()

    with caplog.at_level("INFO"):
        with patch.object(voice.openai.realtime, "RealtimeModel"):
            voice._build_realtime_model(provider="openai", model="gpt-realtime-2.1")

    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "provider=openai" in line
    assert "model=gpt-realtime-2.1" in line
    assert "source=mc" in line


def test_env_sourced_calls_are_marked_as_such(clean_env, caplog):
    voice = _import_main()

    with caplog.at_level("INFO"):
        with patch.object(voice.openai.realtime, "RealtimeModel"):
            voice._build_realtime_model()

    assert "source=env" in "\n".join(r.getMessage() for r in caplog.records)
