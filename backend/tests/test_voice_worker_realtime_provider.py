"""Tests for voice_worker/main.py `_build_realtime_model()` — ADR-060.

Covers the env-based provider switch (OpenAI Realtime default, xAI Realtime
fallback): correct plugin selected, voice defaults per provider,
VOICE_VOICE_ID override, VOICE_MODEL override, and fail-fast when the
relevant API key is missing. The livekit plugin constructors are mocked so
no real API calls happen and no network/key is required to run the suite.
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


# ────────────────────────────────────────────────────────────────────────
# Provider selection + defaults
# ────────────────────────────────────────────────────────────────────────


def test_default_provider_is_openai(monkeypatch):
    """No VOICE_PROVIDER set → defaults to openai (ADR-060)."""
    voice = _import_main()
    monkeypatch.delenv("VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock(name="RealtimeModel-instance")
    with patch.object(voice.openai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        result = voice._build_realtime_model()

    assert result is fake_model
    ctor.assert_called_once_with(
        model="gpt-realtime-2.1",
        voice="marin",
        turn_detection=voice._TURN_DETECTION,
    )


def test_openai_provider_explicit(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)

    fake_model = MagicMock()
    with patch.object(voice.openai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once_with(
        model="gpt-realtime-2.1",
        voice="marin",
        turn_detection=voice._TURN_DETECTION,
    )


def test_openai_provider_voice_override(monkeypatch):
    """VOICE_VOICE_ID overrides the openai default voice ('marin')."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VOICE_VOICE_ID", "cedar")
    monkeypatch.delenv("VOICE_MODEL", raising=False)

    fake_model = MagicMock()
    with patch.object(voice.openai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once_with(
        model="gpt-realtime-2.1",
        voice="cedar",
        turn_detection=voice._TURN_DETECTION,
    )


def test_openai_provider_model_override(monkeypatch):
    """VOICE_MODEL overrides the default 'gpt-realtime-2.1'."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VOICE_MODEL", "gpt-realtime")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)

    fake_model = MagicMock()
    with patch.object(voice.openai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once_with(
        model="gpt-realtime",
        voice="marin",
        turn_detection=voice._TURN_DETECTION,
    )


def test_xai_provider_fallback(monkeypatch):
    """VOICE_PROVIDER=xai keeps the pre-ADR-060 behaviour unchanged."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)

    fake_model = MagicMock()
    with patch.object(voice.xai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        result = voice._build_realtime_model()

    assert result is fake_model
    ctor.assert_called_once_with(
        voice="ara",
        turn_detection=voice._TURN_DETECTION,
    )


def test_xai_provider_voice_override(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("VOICE_VOICE_ID", "Eve")

    fake_model = MagicMock()
    with patch.object(voice.xai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once_with(
        voice="Eve",
        turn_detection=voice._TURN_DETECTION,
    )


def test_provider_case_insensitive(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "OpenAI")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)

    fake_model = MagicMock()
    with patch.object(voice.openai.realtime, "RealtimeModel", return_value=fake_model) as ctor:
        voice._build_realtime_model()

    ctor.assert_called_once()


# ────────────────────────────────────────────────────────────────────────
# Fail-fast without key
# ────────────────────────────────────────────────────────────────────────


def test_openai_missing_key_fails_fast(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch.object(voice.openai.realtime, "RealtimeModel") as ctor:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            voice._build_realtime_model()

    ctor.assert_not_called()


def test_xai_missing_key_fails_fast(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "xai")
    monkeypatch.delenv("XAI_API_KEY", raising=False)

    with patch.object(voice.xai.realtime, "RealtimeModel") as ctor:
        with pytest.raises(RuntimeError, match="XAI_API_KEY"):
            voice._build_realtime_model()

    ctor.assert_not_called()


def test_unknown_provider_fails_fast(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "elevenlabs")

    with pytest.raises(RuntimeError, match="Unknown VOICE_PROVIDER"):
        voice._build_realtime_model()
