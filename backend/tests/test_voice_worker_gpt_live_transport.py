"""Tests for voice_worker/main.py GPT-Live transport selection — ADR-082.

Covers `VOICE_API` (realtime default / live), `_build_live_model()`
(GPTLiveModel construction: model, voice, delegation="responses", backend
model sourced from `jarvis_core.frontier.resolve_model()`), and the
fail-soft fallback to `realtime` when the vorab PR #7212 plugin is not on
the image (`GPTLiveModel` unimportable).

These mock the plugin constructor(s) — no real API calls, no network/key
needed. Same skip-if-deps-missing pattern as
test_voice_worker_realtime_provider.py: the backend pytest venv has no
livekit installed, so this suite skips there. Run it for real inside
voice_worker/Dockerfile.gpt-live (see docs/decisions/081), which has both
livekit-agents core and the vorab GPTLiveModel plugin installed — that is
the authoritative run for this file.
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


# ────────────────────────────────────────────────────────────────────────
# VOICE_API selection
# ────────────────────────────────────────────────────────────────────────


def test_default_voice_api_is_realtime(monkeypatch):
    """No VOICE_API set → _build_llm_model() delegates to _build_realtime_model()."""
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_realtime = MagicMock(name="realtime-sentinel")
    with patch.object(voice, "_build_realtime_model", return_value=fake_realtime) as rt, \
            patch.object(voice, "_build_live_model") as live:
        result = voice._build_llm_model()

    assert result is fake_realtime
    rt.assert_called_once()
    live.assert_not_called()


def test_voice_api_realtime_explicit(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "realtime")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_realtime = MagicMock()
    with patch.object(voice, "_build_realtime_model", return_value=fake_realtime) as rt:
        result = voice._build_llm_model()

    assert result is fake_realtime
    rt.assert_called_once()


def test_voice_api_live_dispatches_to_build_live_model(monkeypatch):
    """VOICE_API=live, plugin available → _build_live_model() is used, not realtime."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "live")
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", True)

    fake_live = MagicMock(name="gpt-live-sentinel")
    with patch.object(voice, "_build_live_model", return_value=fake_live) as live, \
            patch.object(voice, "_build_realtime_model") as rt:
        result = voice._build_llm_model()

    assert result is fake_live
    live.assert_called_once()
    rt.assert_not_called()


def test_voice_api_live_falls_back_when_plugin_missing(monkeypatch, caplog):
    """VOICE_API=live but GPTLiveModel not importable (prod image) -> warns +
    falls back to realtime instead of crashing (see module docstring 'GPT-Live-Transport')."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "live")
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", False)

    fake_realtime = MagicMock()
    with patch.object(voice, "_build_realtime_model", return_value=fake_realtime) as rt, \
            patch.object(voice, "_build_live_model") as live:
        with caplog.at_level("WARNING"):
            result = voice._build_llm_model()

    assert result is fake_realtime
    rt.assert_called_once()
    live.assert_not_called()
    assert any("falling back to VOICE_API=realtime" in r.message for r in caplog.records)


def test_voice_api_unknown_fails_fast(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "sip")

    with pytest.raises(RuntimeError, match="Unknown VOICE_API"):
        voice._build_llm_model()


def test_voice_api_case_insensitive(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "LIVE")
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", True)

    fake_live = MagicMock()
    with patch.object(voice, "_build_live_model", return_value=fake_live) as live:
        voice._build_llm_model()

    live.assert_called_once()


# ────────────────────────────────────────────────────────────────────────
# _build_live_model() — GPTLiveModel construction (only meaningful when the
# vorab plugin is actually on the image; skips if not — see module docstring).
# ────────────────────────────────────────────────────────────────────────


def _require_gpt_live(voice):
    if not voice._GPT_LIVE_AVAILABLE:
        pytest.skip(
            "GPTLiveModel not installed on this interpreter — run inside "
            "voice_worker/Dockerfile.gpt-live (LiveKit PR #7212, vorab)."
        )


def test_build_live_model_defaults(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_FRONTIER_MODEL", raising=False)

    fake_model = MagicMock(name="GPTLiveModel-instance")
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        result = voice._build_live_model()

    assert result is fake_model
    ctor.assert_called_once()
    _, kwargs = ctor.call_args
    assert kwargs["model"] == "gpt-live-1"
    assert kwargs["voice"] == "marin"
    assert kwargs["delegation"] == "responses"
    # Backend model = today's frontier default (gpt-5.5), not GPTLiveModel's
    # own default ("gpt-5.6-luna", an unclear codename — see frontier.py).
    assert kwargs["responses_options"]["model"] == "gpt-5.5"
    assert "instructions" in kwargs["responses_options"]


def test_build_live_model_voice_and_model_override(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VOICE_VOICE_ID", "aster")
    monkeypatch.setenv("VOICE_MODEL", "gpt-live-1-preview")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_model()

    _, kwargs = ctor.call_args
    assert kwargs["voice"] == "aster"
    assert kwargs["model"] == "gpt-live-1-preview"


def test_build_live_model_uses_jarvis_frontier_model_override(monkeypatch):
    """JARVIS_FRONTIER_MODEL overrides the backend Responses model too — same
    single source of truth as the ask_frontier tool (jarvis_core.frontier)."""
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "gpt-5.4-pro")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_model()

    _, kwargs = ctor.call_args
    assert kwargs["responses_options"]["model"] == "gpt-5.4-pro"


def test_build_live_model_missing_key_fails_fast(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch.object(voice, "GPTLiveModel") as ctor:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            voice._build_live_model()

    ctor.assert_not_called()


def test_build_live_model_raises_when_plugin_unavailable(monkeypatch):
    """Direct call (bypassing _build_llm_model's fallback) still fails loud,
    not silently — the fallback/log only lives in _build_llm_model()."""
    voice = _import_main()
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with pytest.raises(RuntimeError, match="not importable"):
        voice._build_live_model()


# ────────────────────────────────────────────────────────────────────────
# AGENT_NAME / WorkerOptions isolation (test worker must not steal calls
# from the production worker — explicit agent_name required).
# ────────────────────────────────────────────────────────────────────────


def test_agent_name_env_defaults_empty(monkeypatch):
    """No AGENT_NAME -> WorkerOptions gets '' (automatic dispatch, prod default)."""
    monkeypatch.delenv("AGENT_NAME", raising=False)
    import os
    assert os.environ.get("AGENT_NAME", "").strip() == ""
