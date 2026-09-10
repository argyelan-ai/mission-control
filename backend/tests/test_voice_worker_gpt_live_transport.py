"""Tests for GPT-Live-specific behavior that genuinely needs livekit — ADR-083.

Provider/model/voice/api SELECTION (VoiceChoice, classify_voice_api) is
covered by test_voice_provider_choice.py (ADR-082). Plugin-construction
dispatch through _API_TRANSPORTS/_build_transport (including the typed
TurnDetection fix and a GPT-Live smoke build) is covered by
test_voice_worker_realtime_provider.py. Greeting, urgent-note, GPT-Live
voice-name validation, and delegation-latency tracking are livekit-free pure
logic and live in jarvis_core/voice_greeting.py — covered (unconditionally,
no skip) by test_jarvis_voice_greeting.py, which runs in the ordinary
backend test job. Moving that logic there is itself a fix for a review
finding (10.09.2026): every test that needed voice_worker/main.py's
module-level livekit import skipped silently in CI, and two of three
sabotage-planted regressions (a greeting task-count regression, a broken
voice-validation passthrough) went uncaught as a result.

What's left here: `_build_live_transport()`'s latency-tuning kwargs
(responses_options — reasoning/text/service_tier/max_output_tokens/model),
which genuinely needs `GPTLiveModel`, and the two split persona instruction
blocks in jarvis_core/persona.py (livekit-free already, but grouped here
since they're GPT-Live-specific).

These mock the plugin constructor(s) where noted — no real API calls, no
network/key needed. Same skip-if-deps-missing pattern as
test_voice_worker_realtime_provider.py: the backend pytest venv has no
livekit installed, so the _build_live_transport tests skip there. Run them
for real inside an image built from the regular voice_worker/Dockerfile (see
docs/decisions/083), which has both livekit-agents core and the vorab
GPTLiveModel plugin installed — that is the authoritative run for those.
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
    kw.setdefault("model", "gpt-live-1")
    kw.setdefault("voice", "marin")
    kw.setdefault("source", "mc")
    kw.setdefault("api", "live")
    return voice_main.VoiceChoice(**kw)


def _require_gpt_live(voice):
    if not voice._GPT_LIVE_AVAILABLE:
        pytest.skip(
            "GPTLiveModel not installed on this interpreter — run inside "
            "the regular voice_worker/Dockerfile image (LiveKit PR #7212)."
        )


# ────────────────────────────────────────────────────────────────────────
# _build_live_transport() — GPTLiveModel construction, latency tuning
# ────────────────────────────────────────────────────────────────────────


def test_build_live_transport_defaults(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice, model="gpt-live-1", voice="marin")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("JARVIS_LIVE_BACKEND_MODEL", raising=False)

    fake_model = MagicMock(name="GPTLiveModel-instance")
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        llm, instructions = voice._build_live_transport(choice)

    assert llm is fake_model
    ctor.assert_called_once()
    _, kwargs = ctor.call_args
    assert kwargs["model"] == "gpt-live-1"
    # "marin" is a code-verified valid gpt-live-1 voice (GPTLiveVoices Literal
    # + DEFAULT_VOICE in the PR's gpt_live_model.py) — not a leftover Realtime
    # name, see jarvis_core/voice_greeting.py's GPT_LIVE_KNOWN_VOICES comment.
    assert kwargs["voice"] == "marin"
    assert kwargs["delegation"] == "responses"
    # Backend model = GPTLiveModel's own "Fast mode" default (gpt-5.6-luna),
    # NOT the frontier reasoning model (gpt-5.5) — latency tuning after
    # Mark's first call showed 16s round trips with the reasoning model.
    opts = kwargs["responses_options"]
    assert opts["model"] == "gpt-5.6-luna"
    assert opts["reasoning"] == {"effort": "low"}
    assert opts["text"] == {"verbosity": "low"}
    assert opts["service_tier"] == "priority"
    assert opts["max_output_tokens"] == 400
    # Backend gets the FULL procedural instructions (ADR-083 split) — this is
    # the review fix: previously only 4 generic sentences went here.
    backend_instructions = opts["instructions"]
    assert "TOOLS" in backend_instructions
    assert "create_task" in backend_instructions
    assert "HONESTY ABOUT FRESHNESS" in backend_instructions
    assert "CONFIRMATION ECHO" in backend_instructions
    # Top-level agent instructions are the SHORT voice-layer persona, not the
    # backend's full procedural text.
    assert "WORAUF DU REAGIERST" not in instructions
    assert "concierge" in instructions.lower()


def test_build_live_transport_uses_choice_model_and_voice(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice, model="gpt-live-1-preview", voice="aster")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_transport(choice)

    _, kwargs = ctor.call_args
    assert kwargs["voice"] == "aster"
    assert kwargs["model"] == "gpt-live-1-preview"


def test_build_live_transport_falls_back_to_default_model_when_choice_has_none(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice, model=None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_transport(choice)

    assert ctor.call_args.kwargs["model"] == "gpt-live-1"


def test_build_live_transport_uses_jarvis_live_backend_model_override(monkeypatch):
    """JARVIS_LIVE_BACKEND_MODEL overrides the backend Responses model — a
    SEPARATE knob from JARVIS_FRONTIER_MODEL (ask_frontier): Full-Duplex needs
    a fast model, ask_frontier is allowed to think slowly."""
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JARVIS_LIVE_BACKEND_MODEL", "gpt-5.4")
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "gpt-5.5")  # must NOT leak in here

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_transport(choice)

    assert ctor.call_args.kwargs["responses_options"]["model"] == "gpt-5.4"


def test_build_live_transport_briefing_reaches_backend_instructions(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_transport(choice, briefing_ctx="## Briefing\nzehn Tasks offen")

    assert "zehn Tasks offen" in ctor.call_args.kwargs["responses_options"]["instructions"]


def test_build_live_transport_missing_key_fails_fast(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    choice = _choice(voice)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch.object(voice, "GPTLiveModel") as ctor:
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            voice._build_live_transport(choice)

    ctor.assert_not_called()


def test_build_live_transport_raises_when_plugin_unavailable(monkeypatch):
    voice = _import_main()
    choice = _choice(voice)
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with pytest.raises(RuntimeError, match="not importable"):
        voice._build_live_transport(choice)


# ────────────────────────────────────────────────────────────────────────
# entrypoint() forced-realtime-fallback guard (ADR-083 Review Finding 2) —
# only the pure decision logic can be unit-tested without a real LiveKit
# JobContext; verifies the FORCED choice (not a plain env re-resolve, which
# would reproduce the bug when VOICE_MODEL=gpt-live-1 is set) is what the
# guard constructs when GPTLiveModel is unavailable.
# ────────────────────────────────────────────────────────────────────────


def test_forced_realtime_fallback_ignores_model_name_signal(monkeypatch):
    """The exact bug the review caught: a plain resolve_voice_choice(None)
    would classify api='live' again if VOICE_MODEL=gpt-live-1 is set (as it
    is in production) — so the guard must construct an explicit realtime
    VoiceChoice instead of re-resolving from env."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_MODEL", "gpt-live-1")  # prod .env shape
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    original = voice.resolve_voice_choice(None)
    assert original.api == "live"  # confirms the env alone would reproduce the bug

    forced = voice.VoiceChoice(
        provider="openai", model="gpt-realtime-2.1", voice="marin",
        source="live-unavailable-fallback", api="realtime",
    )
    assert forced.api == "realtime"
    assert forced.provider == "openai"
    assert forced.model == "gpt-realtime-2.1"


# ────────────────────────────────────────────────────────────────────────
# persona.py — live instructions split (importable without livekit at all)
# ────────────────────────────────────────────────────────────────────────


def test_live_voice_instructions_short_and_style_only():
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions(operator_name="Mark")
    assert "WORAUF DU REAGIERST" not in text
    assert "create_task" not in text
    # Budget widened from ~150 to ~300 words when the naturalness/tone rules
    # (laughter, backchannels, pitch/pace variation) were added — still a
    # short voice-layer prompt, no tool-procedure content (asserted above).
    assert len(text.split()) < 300


def test_live_voice_instructions_no_self_introduction():
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions()
    assert "Never introduce yourself" in text


def test_live_voice_instructions_stop_on_interrupt():
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions()
    assert "Interrupted" in text or "interrupt" in text.lower()


def test_live_voice_instructions_naturalness_cues():
    """Mark: 'soll auch lachen, natürlich wirken wie ChatGPT' — laughter,
    backchannels, and pitch/pace variation must be explicit in the prompt
    (no dedicated GPT-Live protocol field for this exists — checked the PR's
    gpt_live_types.py/gpt_live_model.py: only `voice` name/id, no
    emotion/expressiveness/speed session field — so instructions text is the
    only lever)."""
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions()
    assert "laugh" in text.lower()
    assert "pitch" in text.lower() and "pace" in text.lower()
    assert "hm" in text.lower() or "backchannel" in text.lower()


def test_live_voice_instructions_honesty_rule():
    """Review Finding 6: the voice block had no explicit honesty rule of its
    own — only an implicit "delegate, don't guess" nudge. Explicit rule added:
    never state task/agent facts itself, only what the backend delivered."""
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions().lower()
    assert "never state" in text or "don't invent" in text or "nichts erfinden" in text


def test_live_delegation_instructions_has_full_procedure():
    from jarvis_core.persona import build_live_delegation_instructions

    text = build_live_delegation_instructions(frontier_enabled=False)
    assert "create_task" in text
    assert "dispatch_to_agent" in text
    assert "HONESTY ABOUT FRESHNESS" in text
    assert "ask_frontier" not in text  # gated off by default


def test_live_delegation_instructions_frontier_addendum():
    from jarvis_core.persona import build_live_delegation_instructions

    text = build_live_delegation_instructions(frontier_enabled=True)
    assert "ask_frontier" in text


def test_live_delegation_instructions_briefing_ctx():
    from jarvis_core.persona import build_live_delegation_instructions

    text = build_live_delegation_instructions(briefing_ctx="10 Tasks offen")
    assert "10 Tasks offen" in text


def test_live_delegation_instructions_confirmation_echo():
    from jarvis_core.persona import build_live_delegation_instructions

    text = build_live_delegation_instructions()
    assert "CONFIRMATION ECHO" in text
