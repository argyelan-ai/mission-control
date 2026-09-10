"""voice_worker/main.py::_build_realtime_model — plugin construction only.

The DECISION (which provider/model/voice, and all the never-go-silent
fallbacks) lives in jarvis_core/voice_provider.py and is covered by
test_voice_provider_choice.py, which runs in the ordinary backend job.

What is left here is the part that genuinely needs livekit: that the chosen
VoiceChoice reaches the right plugin constructor with the right arguments.
These tests SKIP where livekit is absent — including this backend venv and
CI. That is acceptable now precisely because the decision rules are tested
elsewhere; it was NOT acceptable when this file held the rules too (memory
2026-08-21: ten tests, all silently skipped, reported green).

Run them against the real voice-worker image (Wirk-Beweis, not a skip):

    docker run --rm \
      -v "$PWD/voice_worker:/w/voice_worker:ro" \
      -v "$PWD/jarvis_core:/w/jarvis_core:ro" \
      -v "$PWD/backend/tests/test_voice_worker_realtime_provider.py:\
/w/backend/tests/test_voice_worker_realtime_provider.py:ro" \
      -w /w mission-control-voice-worker \
      sh -c "pip -q install pytest pytest-asyncio; python -m pytest backend/tests -q -rs -p no:cacheprovider"
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


def _choice(voice_main, **kw):
    kw.setdefault("provider", "openai")
    kw.setdefault("model", "gpt-realtime-2.1")
    kw.setdefault("voice", "marin")
    kw.setdefault("source", "mc")
    return voice_main.VoiceChoice(**kw)


def test_openai_arm_reaches_the_openai_plugin():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-realtime-2.1", voice="marin")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(choice)

    xai_ctor.assert_not_called()
    assert openai_ctor.call_args.kwargs == {
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "turn_detection": voice._TURN_DETECTION,
    }


def test_xai_arm_reaches_the_xai_plugin():
    voice = _import_main()
    choice = _choice(voice, provider="xai", model="grok-voice-fast-1.0", voice="ara")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_realtime_model(choice)

    openai_ctor.assert_not_called()
    assert xai_ctor.call_args.kwargs == {
        "voice": "ara",
        "turn_detection": voice._TURN_DETECTION,
    }


def test_openai_arm_defaults_the_model_when_choice_has_none():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model=None, voice="marin")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(choice)

    assert ctor.call_args.kwargs["model"] == "gpt-realtime-2.1"


def test_unknown_provider_raises_rather_than_silently_picking_one():
    voice = _import_main()
    choice = _choice(voice, provider="does-not-exist")

    with pytest.raises(RuntimeError):
        voice._build_realtime_model(choice)


# ── api registry (ADR-082 follow-up) ────────────────────────────────────
#
# Only "realtime" has a transport builder in this image today. "live"
# (OpenAI's Live API) is a value classify_voice_api can produce but this PR
# deliberately does not implement a LiveTransport — entrypoint() is supposed
# to catch that BEFORE calling _build_realtime_model, so reaching this
# function with api="live" is the defensive-last-line case, not a normal
# path (see entrypoint()'s guard, covered separately by unit tests on
# resolve_voice_choice + report_voice_unsupported in the backend job).


def test_only_realtime_has_a_registered_transport():
    voice = _import_main()
    assert set(voice._API_TRANSPORTS) == {"realtime"}


def test_realtime_api_dispatches_through_the_registry():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-realtime-2.1", voice="marin", api="realtime")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_realtime_model(choice)

    ctor.assert_called_once()


def test_live_api_has_no_transport_and_raises_defensively():
    """This is the LAST line of defense, not the normal refusal path — the
    normal path is entrypoint() falling back BEFORE this is ever called with
    api="live". If this ever fires in production it means that guard was
    skipped, so it must be loud (RuntimeError), never a silent wrong-endpoint
    connect."""
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-live-1", voice="marin", api="live")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        with pytest.raises(RuntimeError, match="live"):
            voice._build_realtime_model(choice)

    openai_ctor.assert_not_called()
    xai_ctor.assert_not_called()
