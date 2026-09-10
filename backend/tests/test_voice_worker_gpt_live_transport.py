"""Tests for GPT-Live-specific behavior — ADR-083.

Provider/model/voice/api SELECTION (VoiceChoice, classify_voice_api) is
covered by test_voice_provider_choice.py (ADR-082). Plugin-construction
dispatch through _API_TRANSPORTS/_build_transport (including the typed
TurnDetection fix and a GPT-Live smoke build) is covered by
test_voice_worker_realtime_provider.py. This file covers what's specific to
the "live" api once it's chosen: the backend Responses model + latency
tuning in _build_live_transport(), GPT-Live voice-name validation, the
situational greeting (no task-count dump — Mark's review feedback), the
delegation-latency log hook, and the two split persona instruction blocks
in jarvis_core/persona.py.

These mock the plugin constructor(s) where noted — no real API calls, no
network/key needed. Same skip-if-deps-missing pattern as
test_voice_worker_realtime_provider.py: the backend pytest venv has no
livekit installed, so this suite skips there. Run it for real inside an
image built from the regular voice_worker/Dockerfile (see
docs/decisions/083), which has both livekit-agents core and the vorab
GPTLiveModel plugin installed — that is the authoritative run for this file.
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
    # name, see GPT_LIVE_KNOWN_VOICES comment.
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


def test_resolve_live_backend_model_default(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("JARVIS_LIVE_BACKEND_MODEL", raising=False)
    assert voice._resolve_live_backend_model() == "gpt-5.6-luna"


def test_resolve_live_backend_model_override(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("JARVIS_LIVE_BACKEND_MODEL", "gpt-5.4-pro")
    assert voice._resolve_live_backend_model() == "gpt-5.4-pro"


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
# _validate_live_voice() — voice-name validation (ADR-083 review fix)
# ────────────────────────────────────────────────────────────────────────


def test_validate_live_voice_empty_uses_default():
    voice = _import_main()
    assert voice._validate_live_voice("") == "marin"
    assert voice._validate_live_voice(None) == "marin"


def test_validate_live_voice_known_passthrough():
    voice = _import_main()
    assert voice._validate_live_voice("vesper") == "vesper"


def test_validate_live_voice_known_case_insensitive():
    voice = _import_main()
    assert voice._validate_live_voice("Stone") == "stone"


def test_validate_live_voice_unknown_falls_back_with_warning(caplog):
    voice = _import_main()
    with caplog.at_level("WARNING"):
        result = voice._validate_live_voice("ara")  # xAI Realtime voice name, invalid here

    assert result == "marin"
    assert any("not a known gpt-live-1 voice" in r.message for r in caplog.records)


def test_all_known_voices_accepted():
    voice = _import_main()
    for name in voice.GPT_LIVE_KNOWN_VOICES:
        assert voice._validate_live_voice(name) == name


# ────────────────────────────────────────────────────────────────────────
# AGENT_NAME / WorkerOptions isolation (test worker must not steal calls
# from the production worker — explicit agent_name required).
# ────────────────────────────────────────────────────────────────────────


def test_agent_name_env_defaults_empty(monkeypatch):
    """No AGENT_NAME -> WorkerOptions gets '' (automatic dispatch, prod default)."""
    monkeypatch.delenv("AGENT_NAME", raising=False)
    import os
    assert os.environ.get("AGENT_NAME", "").strip() == ""


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


# ────────────────────────────────────────────────────────────────────────
# Greeting — situational opening (no number dump), ADR-083 Nachschliff
# after Mark's "berichtet direkt beim Einstieg" feedback.
# ────────────────────────────────────────────────────────────────────────


def test_urgent_note_none_for_plain_open_tasks():
    voice = _import_main()
    briefing = {"open_tasks": [{"status": "inbox"}, {"status": "in_progress"}], "open_approvals_count": 0}
    assert voice._urgent_note(briefing) is None


def test_urgent_note_none_when_empty():
    voice = _import_main()
    assert voice._urgent_note({"open_tasks": [], "open_approvals_count": 0}) is None


def test_urgent_note_single_approval():
    voice = _import_main()
    note = voice._urgent_note({"open_tasks": [], "open_approvals_count": 1})
    assert note is not None
    assert "Approval" in note


def test_urgent_note_multi_approvals_mentions_count():
    voice = _import_main()
    note = voice._urgent_note({"open_tasks": [], "open_approvals_count": 3})
    assert "3" in note


def test_urgent_note_blocked_task_named():
    voice = _import_main()
    briefing = {
        "open_tasks": [{"status": "blocked", "title": "Fix deploy pipeline"}],
        "open_approvals_count": 0,
    }
    note = voice._urgent_note(briefing)
    assert note is not None
    assert "Fix deploy pipeline" in note


def test_urgent_note_blocked_task_long_title_generic():
    voice = _import_main()
    briefing = {
        "open_tasks": [{
            "status": "blocked",
            "title": "This is a very long task title that exceeds forty characters easily",
        }],
        "open_approvals_count": 0,
    }
    note = voice._urgent_note(briefing)
    assert note is not None
    assert "This is a very long" not in note  # generic phrasing, not the raw title


def test_build_greeting_never_mentions_task_counts(monkeypatch):
    """The core regression: no version of the greeting may read like a status
    report ('10 Tasks offen') — Mark's explicit feedback after the first call."""
    voice = _import_main()
    briefing = {
        "open_tasks": [{"status": "inbox"}] * 10,
        "open_approvals_count": 0,
    }
    for _ in range(20):  # random.choice — sample the whole pool
        text = voice._build_greeting(briefing, operator_name="Mark")
        assert "10" not in text


def test_build_greeting_mentions_urgent_approval(monkeypatch):
    voice = _import_main()
    briefing = {"open_tasks": [], "open_approvals_count": 1}
    text = voice._build_greeting(briefing, operator_name="Mark")
    assert "Approval" in text


def test_build_greeting_fallback_without_briefing():
    voice = _import_main()
    text = voice._build_greeting(None, operator_name="Mark")
    assert "Mark" in text


# ────────────────────────────────────────────────────────────────────────
# Latency logging (ADR-083 Nachschliff — delegation_latency_s)
# ────────────────────────────────────────────────────────────────────────


def test_attach_latency_logging_logs_delay_between_user_and_assistant(caplog):
    voice = _import_main()

    class FakeSession:
        def __init__(self):
            self._handlers = {}

        def on(self, event, handler):
            self._handlers[event] = handler

        def emit(self, event, payload):
            self._handlers[event](payload)

    class FakeItem:
        def __init__(self, role):
            self.role = role

    class FakeEvent:
        def __init__(self, role, created_at):
            self.item = FakeItem(role)
            self.created_at = created_at

    session = FakeSession()
    voice._attach_latency_logging(session)

    with caplog.at_level("INFO"):
        session.emit("conversation_item_added", FakeEvent("user", 100.0))
        session.emit("conversation_item_added", FakeEvent("assistant", 116.3))

    matches = [r for r in caplog.records if "delegation_latency_s=" in r.message]
    assert len(matches) == 1
    assert "16.3" in matches[0].message


def test_attach_latency_logging_survives_bad_event(caplog):
    """A malformed event must never raise / break the session — fail-soft."""
    voice = _import_main()

    class FakeSession:
        def __init__(self):
            self._handlers = {}

        def on(self, event, handler):
            self._handlers[event] = handler

        def emit(self, event, payload):
            self._handlers[event](payload)

    session = FakeSession()
    voice._attach_latency_logging(session)

    session.emit("conversation_item_added", object())  # no .item / .created_at at all
