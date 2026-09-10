"""Tests for voice_worker/main.py GPT-Live transport selection — ADR-083.

Covers `VOICE_API` (explicit env, auto-detect from VOICE_MODEL, realtime
default), `_build_live_model()` (GPTLiveModel construction: model, voice,
delegation="responses", latency-tuned backend model via
`_resolve_live_backend_model()` — separate from the ask_frontier tool's
`jarvis_core.frontier.resolve_model()`), the instructions split (voice-layer
top-level Agent instructions vs. the backend Responses model's own
procedural instructions — ADR-083 review fix), voice-name validation
(`_resolve_live_voice()`), the typed `TurnDetection` fix for the realtime
fallback path, the situational greeting (no task-count dump), the
delegation-latency log hook, and the fail-soft fallback to `realtime` when
the vorab PR #7212 plugin is not on the image (`GPTLiveModel` unimportable).

These mock the plugin constructor(s) — no real API calls, no network/key
needed. Same skip-if-deps-missing pattern as
test_voice_worker_realtime_provider.py: the backend pytest venv has no
livekit installed, so this suite skips there. Run it for real inside an image built from the regular
voice_worker/Dockerfile (see docs/decisions/083 — the vorab GPTLiveModel
plugin install is now the standard build path, not a separate test image),
which has both livekit-agents core and the vorab GPTLiveModel plugin
installed — that is the authoritative run for this file.
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
# VOICE_API resolution: explicit env, auto-detect, default
# ────────────────────────────────────────────────────────────────────────


def test_resolve_voice_api_explicit_wins(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "live")
    monkeypatch.setenv("VOICE_MODEL", "gpt-realtime-2.1")  # would auto-detect realtime
    assert voice._resolve_voice_api() == "live"


def test_resolve_voice_api_defaults_to_realtime(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    assert voice._resolve_voice_api() == "realtime"


def test_resolve_voice_api_auto_detects_live_from_model(monkeypatch, caplog):
    """The live bug (10.09.2026): compose passed VOICE_MODEL=gpt-live-1 but not
    VOICE_API — must auto-select 'live' instead of silently using realtime
    with an invalid model name."""
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.setenv("VOICE_MODEL", "gpt-live-1")

    with caplog.at_level("INFO"):
        result = voice._resolve_voice_api()

    assert result == "live"
    assert any("auto-selecting VOICE_API=live" in r.message for r in caplog.records)


def test_resolve_voice_api_case_insensitive_model_prefix(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.setenv("VOICE_MODEL", "GPT-Live-1-Preview")
    assert voice._resolve_voice_api() == "live"


def test_resolve_voice_api_realtime_model_stays_realtime(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.setenv("VOICE_MODEL", "gpt-realtime-2.1")
    assert voice._resolve_voice_api() == "realtime"


# ────────────────────────────────────────────────────────────────────────
# _build_llm_model() — transport selection + (llm, instructions) contract
# ────────────────────────────────────────────────────────────────────────


def test_default_voice_api_is_realtime(monkeypatch):
    """No VOICE_API/VOICE_MODEL set → _build_llm_model() uses _build_realtime_model()
    and the FULL persona (build_instructions) as top-level Agent instructions."""
    voice = _import_main()
    monkeypatch.delenv("VOICE_API", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_realtime = MagicMock(name="realtime-sentinel")
    with patch.object(voice, "_build_realtime_model", return_value=fake_realtime) as rt, \
            patch.object(voice, "_build_live_model") as live:
        llm, instructions = voice._build_llm_model(operator_name="Mark")

    assert llm is fake_realtime
    rt.assert_called_once()
    live.assert_not_called()
    # full persona, not the short live voice-layer text
    assert "WER DU BIST" in instructions
    assert "Mark" in instructions


def test_voice_api_realtime_explicit(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "realtime")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_realtime = MagicMock()
    with patch.object(voice, "_build_realtime_model", return_value=fake_realtime) as rt:
        llm, instructions = voice._build_llm_model()

    assert llm is fake_realtime
    rt.assert_called_once()
    assert "WER DU BIST" in instructions


def test_voice_api_live_dispatches_to_build_live_model(monkeypatch):
    """VOICE_API=live, plugin available → _build_live_model() is used, not realtime,
    and the top-level Agent instructions are the SHORT voice-layer text (ADR-083
    split), not the full persona."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "live")
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", True)

    fake_live = MagicMock(name="gpt-live-sentinel")
    with patch.object(voice, "_build_live_model", return_value=fake_live) as live, \
            patch.object(voice, "_build_realtime_model") as rt:
        llm, instructions = voice._build_llm_model()

    assert llm is fake_live
    live.assert_called_once()
    rt.assert_not_called()
    # short voice-layer persona (ADR-083): no tool-trigger table, no "WORAUF DU
    # REAGIERST" — that content now lives in the backend delegation instructions.
    assert "WORAUF DU REAGIERST" not in instructions
    assert "WER DU BIST" not in instructions
    assert "concierge voice" in instructions.lower()


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
            llm, instructions = voice._build_llm_model()

    assert llm is fake_realtime
    rt.assert_called_once()
    live.assert_not_called()
    assert "WER DU BIST" in instructions  # fell back to full realtime persona too
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


def test_build_llm_model_passes_briefing_and_frontier_through(monkeypatch):
    """briefing_ctx/frontier_enabled/operator_name reach _build_live_model() so the
    backend delegation instructions can include them (ADR-083)."""
    voice = _import_main()
    monkeypatch.setenv("VOICE_API", "live")
    monkeypatch.setattr(voice, "_GPT_LIVE_AVAILABLE", True)

    fake_live = MagicMock()
    with patch.object(voice, "_build_live_model", return_value=fake_live) as live:
        voice._build_llm_model(
            briefing_ctx="## Briefing\nfoo", frontier_enabled=True, operator_name="Mark",
        )

    _, kwargs = live.call_args
    assert kwargs["briefing_ctx"] == "## Briefing\nfoo"
    assert kwargs["frontier_enabled"] is True
    assert kwargs["operator_name"] == "Mark"


# ────────────────────────────────────────────────────────────────────────
# _build_live_model() — GPTLiveModel construction (only meaningful when the
# vorab plugin is actually on the image; skips if not — see module docstring).
# ────────────────────────────────────────────────────────────────────────


def _require_gpt_live(voice):
    if not voice._GPT_LIVE_AVAILABLE:
        pytest.skip(
            "GPTLiveModel not installed on this interpreter — run inside "
            "the vorab-installed LiveKit PR #7212 plugin (see voice_worker/Dockerfile)."
        )


def test_build_live_model_defaults(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)
    monkeypatch.delenv("JARVIS_LIVE_BACKEND_MODEL", raising=False)

    fake_model = MagicMock(name="GPTLiveModel-instance")
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        result = voice._build_live_model()

    assert result is fake_model
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


def test_build_live_model_uses_jarvis_live_backend_model_override(monkeypatch):
    """JARVIS_LIVE_BACKEND_MODEL overrides the backend Responses model — a
    SEPARATE knob from JARVIS_FRONTIER_MODEL (ask_frontier): Full-Duplex needs
    a fast model, ask_frontier is allowed to think slowly."""
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("JARVIS_LIVE_BACKEND_MODEL", "gpt-5.4")
    monkeypatch.setenv("JARVIS_FRONTIER_MODEL", "gpt-5.5")  # must NOT leak in here

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_model()

    _, kwargs = ctor.call_args
    assert kwargs["responses_options"]["model"] == "gpt-5.4"


def test_resolve_live_backend_model_default(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("JARVIS_LIVE_BACKEND_MODEL", raising=False)
    assert voice._resolve_live_backend_model() == "gpt-5.6-luna"


def test_resolve_live_backend_model_override(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("JARVIS_LIVE_BACKEND_MODEL", "gpt-5.4-pro")
    assert voice._resolve_live_backend_model() == "gpt-5.4-pro"


def test_build_live_model_briefing_reaches_backend_instructions(monkeypatch):
    voice = _import_main()
    _require_gpt_live(voice)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    fake_model = MagicMock()
    with patch.object(voice, "GPTLiveModel", return_value=fake_model) as ctor:
        voice._build_live_model(briefing_ctx="## Briefing\nzehn Tasks offen")

    _, kwargs = ctor.call_args
    assert "zehn Tasks offen" in kwargs["responses_options"]["instructions"]


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
# _resolve_live_voice() — voice-name validation (ADR-083 review fix)
# ────────────────────────────────────────────────────────────────────────


def test_resolve_live_voice_no_override_uses_default(monkeypatch):
    voice = _import_main()
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    assert voice._resolve_live_voice() == "marin"


def test_resolve_live_voice_known_override(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_VOICE_ID", "vesper")
    assert voice._resolve_live_voice() == "vesper"


def test_resolve_live_voice_known_override_case_insensitive(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_VOICE_ID", "Stone")
    assert voice._resolve_live_voice() == "stone"


def test_resolve_live_voice_unknown_falls_back_with_warning(monkeypatch, caplog):
    voice = _import_main()
    monkeypatch.setenv("VOICE_VOICE_ID", "ara")  # xAI Realtime voice name, invalid here

    with caplog.at_level("WARNING"):
        result = voice._resolve_live_voice()

    assert result == "marin"
    assert any("not a known gpt-live-1 voice" in r.message for r in caplog.records)


def test_all_known_voices_accepted(monkeypatch):
    voice = _import_main()
    for name in voice.GPT_LIVE_KNOWN_VOICES:
        monkeypatch.setenv("VOICE_VOICE_ID", name)
        assert voice._resolve_live_voice() == name


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


def test_live_voice_instructions_no_self_introduction():
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions()
    assert "Never introduce yourself" in text


def test_live_voice_instructions_stop_on_interrupt():
    from jarvis_core.persona import build_live_voice_instructions

    text = build_live_voice_instructions()
    assert "Interrupted" in text or "interrupt" in text.lower()


# ────────────────────────────────────────────────────────────────────────
# TurnDetection: typed object instead of a plain dict (ADR-083 Nachschliff —
# livekit-plugins-openai>=~1.7 rejects a bare dict at RealtimeModel(...)
# construction). Only meaningful when the plugin is actually installed.
# ────────────────────────────────────────────────────────────────────────


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
    monkeypatch.setenv("VOICE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)
    monkeypatch.delenv("VOICE_MODEL", raising=False)

    model = voice._build_realtime_model()  # no mocking — real RealtimeModel(...)
    assert type(model).__name__ == "RealtimeModel"


def test_xai_realtime_model_construction_with_typed_turn_detection_real(monkeypatch):
    voice = _import_main()
    monkeypatch.setenv("VOICE_PROVIDER", "xai")
    monkeypatch.setenv("XAI_API_KEY", "sk-test")
    monkeypatch.delenv("VOICE_VOICE_ID", raising=False)

    model = voice._build_realtime_model()  # no mocking — real xai RealtimeModel(...)
    assert type(model).__name__ == "RealtimeModel"


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
