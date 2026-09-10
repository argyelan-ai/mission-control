"""voice_worker/main.py::_build_transport — plugin construction only.

The DECISION (which provider/model/voice/api, and all the never-go-silent
fallbacks) lives in jarvis_core/voice_provider.py and is covered by
test_voice_provider_choice.py, which runs in the ordinary backend job.

What is left here is the part that genuinely needs livekit: that the chosen
VoiceChoice reaches the right plugin constructor with the right arguments,
and (ADR-083) that "live" now actually builds a GPTLiveModel instead of
raising. These tests SKIP where livekit is absent — including this backend
venv and CI. That is acceptable now precisely because the decision rules are
tested elsewhere; it was NOT acceptable when this file held the rules too
(memory 2026-08-21: ten tests, all silently skipped, reported green).

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
from unittest.mock import MagicMock, patch

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


# ── realtime transport: plugin kwargs ───────────────────────────────────


def test_openai_arm_reaches_the_openai_plugin():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-realtime-2.1", voice="marin")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        llm, instructions = voice._build_transport(choice)

    xai_ctor.assert_not_called()
    assert openai_ctor.call_args.kwargs == {
        "model": "gpt-realtime-2.1",
        "voice": "marin",
        "turn_detection": voice._TURN_DETECTION,
    }
    assert llm is openai_ctor.return_value
    assert "WER DU BIST" in instructions  # full persona, realtime is unsplit


def test_xai_arm_reaches_the_xai_plugin_and_passes_the_bound_model():
    """Bug found in review (2026-09-10): the xai branch built its kwargs
    without ever reading choice.model, so a real MC binding (e.g.
    grok-voice-think-fast-1.0 on the voice-xai seed row) never reached the
    plugin — MC would show the model as bound while the worker spoke
    whichever model the plugin defaults to. model MUST be in the kwargs when
    choice.model is set."""
    voice = _import_main()
    choice = _choice(voice, provider="xai", model="grok-voice-fast-1.0", voice="ara")

    with patch.object(voice.openai.realtime, "RealtimeModel") as openai_ctor, \
            patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_transport(choice)

    openai_ctor.assert_not_called()
    assert xai_ctor.call_args.kwargs == {
        "model": "grok-voice-fast-1.0",
        "voice": "ara",
        "turn_detection": voice._TURN_DETECTION,
    }


def test_xai_arm_omits_model_when_choice_has_none():
    """The plugin's own default is NOT_GIVEN, not None — its type hint does
    not accept None for `model` the way it does for `voice`. When nothing
    names a model (xai's own _MODEL_DEFAULT in voice_provider.py is None),
    the kwarg must be left out entirely rather than passed as None."""
    voice = _import_main()
    choice = _choice(voice, provider="xai", model=None, voice="ara")

    with patch.object(voice.xai.realtime, "RealtimeModel") as xai_ctor:
        voice._build_transport(choice)

    assert "model" not in xai_ctor.call_args.kwargs


def test_openai_arm_defaults_the_model_when_choice_has_none():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model=None, voice="marin")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_transport(choice)

    assert ctor.call_args.kwargs["model"] == "gpt-realtime-2.1"


def test_unknown_provider_raises_rather_than_silently_picking_one():
    voice = _import_main()
    choice = _choice(voice, provider="does-not-exist")

    with pytest.raises(RuntimeError):
        voice._build_transport(choice)


# ── typed TurnDetection (ADR-083 Nachschliff) ───────────────────────────
#
# livekit-plugins-openai>=~1.7 (incl. 1.8.0, which the GPT-Live build step
# force-installs) tightened turn_detection from a plain dict to a typed
# openai.types.beta.realtime.session.TurnDetection object — a bare dict now
# raises AttributeError at construction. Both plugins import the same class.


def test_turn_detection_is_typed_object_when_plugin_available():
    voice = _import_main()
    if voice._TurnDetectionType is None:
        pytest.skip("openai.types.beta.realtime.session.TurnDetection not importable")
    assert isinstance(voice._TURN_DETECTION, voice._TurnDetectionType)
    assert voice._TURN_DETECTION.type == "server_vad"
    assert voice._TURN_DETECTION.threshold == 0.6


def test_realtime_model_construction_with_typed_turn_detection_real(monkeypatch):
    """Unmocked construction against the REAL plugin class — this is the
    actual regression: a plain dict raised AttributeError here on
    livekit-plugins-openai 1.8.0, live reproduced 10.09.2026."""
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-realtime-2.1", voice="marin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    llm, _ = voice._build_transport(choice)  # no mocking — real RealtimeModel(...)
    assert type(llm).__name__ == "RealtimeModel"


def test_xai_realtime_model_construction_with_typed_turn_detection_real(monkeypatch):
    voice = _import_main()
    choice = _choice(voice, provider="xai", model=None, voice="ara")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")

    llm, _ = voice._build_transport(choice)  # no mocking — real xai RealtimeModel(...)
    assert type(llm).__name__ == "RealtimeModel"


# ── api registry (ADR-082 + ADR-083) ────────────────────────────────────


def test_both_realtime_and_live_have_registered_transports():
    """ADR-083: "live" now has a real builder — this used to assert
    {"realtime"} only, with "live" as a deliberate, documented gap
    (test_live_api_has_no_transport_and_raises_defensively below, now
    removed/flipped). GPT-Live is Jarvis' production transport since
    10.09.2026."""
    voice = _import_main()
    assert set(voice._API_TRANSPORTS) == {"realtime", "live"}


def test_realtime_api_dispatches_through_the_registry():
    voice = _import_main()
    choice = _choice(voice, provider="openai", model="gpt-realtime-2.1", voice="marin", api="realtime")

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        voice._build_transport(choice)

    ctor.assert_called_once()


def test_live_api_dispatches_through_the_registry_and_builds_gpt_live(monkeypatch):
    """Flipped from the pre-ADR-083 "live api has no transport and raises
    defensively" test: GPT-Live now has a real builder. Skips cleanly on an
    image without the vorab LiveKit PR #7212 plugin block."""
    voice = _import_main()
    if not voice._GPT_LIVE_AVAILABLE:
        pytest.skip("GPTLiveModel not installed on this interpreter")
    choice = _choice(voice, provider="openai", model="gpt-live-1", voice="marin", api="live")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock(name="GPTLiveModel-instance")
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        llm, instructions = voice._build_transport(choice)

    ctor.assert_called_once()
    assert llm is fake_model
    assert "WORAUF DU REAGIERST" not in instructions  # short voice-layer persona
