"""voice_worker/main.py::_build_realtime_model — plugin construction only.

The DECISION (which provider/model/voice, and all the never-go-silent
fallbacks) lives in jarvis_core/voice_provider.py and is covered by
test_voice_provider_choice.py, which runs in the ordinary backend job.

What is left here is the part that genuinely needs livekit: that the chosen arm
reaches the right plugin constructor with the right arguments. Those tests skip
where livekit is absent — including CI. That is acceptable now precisely
because the rules are tested elsewhere; it was not acceptable when this file
held the rules too (ten tests, skipped, reported green).

Run them against the voice image:

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
from unittest.mock import patch

import pytest

VOICE_DIR = Path(__file__).resolve().parents[2] / "voice_worker"
if str(VOICE_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_DIR))


def _import_main():
    try:
        import main as voice_main  # type: ignore
    except ImportError as exc:
        pytest.skip(f"voice_worker deps not installed: {exc}")
    return voice_main


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    for var in (
        "VOICE_PROVIDER", "VOICE_MODEL",
        "VOICE_OPENAI_VOICE_ID", "VOICE_XAI_VOICE_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_openai_arm_reaches_the_openai_plugin(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="openai")

    xai_ctor.assert_not_called()
    assert openai_ctor.call_args.kwargs == {
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "turn_detection": voice._TURN_DETECTION,
    }


def test_xai_arm_reaches_the_xai_plugin(clean_env):
    voice = _import_main()

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="xai", model="grok-voice-fast-1.0")

    openai_ctor.assert_not_called()
    assert xai_ctor.call_args.kwargs == {
        "model": "grok-voice-fast-1.0",
        "voice": "ara",
        "turn_detection": voice._TURN_DETECTION,
    }


def test_xai_without_a_model_omits_the_argument(clean_env):
    """The plugin's default is NOT_GIVEN, which is not the same as None —
    passing None explicitly would override the default with nothing."""
    voice = _import_main()

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(provider="xai")

    assert "model" not in ctor.call_args.kwargs


def test_the_binding_from_mc_decides_which_plugin_is_built(clean_env):
    """End-to-end through the real module: env says openai, MC says xai."""
    voice = _import_main()
    clean_env.setenv("VOICE_PROVIDER", "openai")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(provider="xai")

    xai_ctor.assert_called_once()
    openai_ctor.assert_not_called()


def test_session_start_logs_the_choice(clean_env, caplog):
    """The live gate greps for this line."""
    voice = _import_main()

    with caplog.at_level("INFO"):
        with patch.object(voice.openai.realtime, "RealtimeModel"):
            voice._build_realtime_model(provider="openai", model="gpt-realtime-2.1")

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "provider=openai" in text and "model=gpt-realtime-2.1" in text and "source=mc" in text
