"""jarvis_core/voice_greeting.py — greeting, GPT-Live voice validation, and
delegation-latency tracking, all livekit-free (ADR-083 Review-Fund,
10.09.2026).

Runs in the ORDINARY backend test job (no skip, no livekit dependency) —
this is precisely the coverage gap a review sabotage-probe found:
backend/tests/test_voice_worker_gpt_live_transport.py's equivalent tests all
import voice_worker/main.py, which imports livekit on module level, so they
silently skip in CI (no livekit in backend/requirements.lock). Two of three
planted regressions there went uncaught as a result — the greeting's
task-count regression and a broken voice-validation passthrough. Both are
covered here without any skip condition.

voice_worker/main.py re-exports these under their old names
(GPT_LIVE_KNOWN_VOICES, _validate_live_voice, _urgent_note, _build_greeting)
for callers/tests that need the livekit-touching entrypoint too — see
test_voice_worker_gpt_live_transport.py for the (skip-if-no-livekit)
integration-shaped tests exercising those re-exports.
"""
from __future__ import annotations

from jarvis_core.voice_greeting import (
    GPT_LIVE_DEFAULT_VOICE,
    GPT_LIVE_KNOWN_VOICES,
    build_greeting,
    track_latency,
    urgent_note,
    validate_live_voice,
)


# ────────────────────────────────────────────────────────────────────────
# validate_live_voice()
# ────────────────────────────────────────────────────────────────────────


def test_validate_live_voice_empty_uses_default():
    assert validate_live_voice("") == GPT_LIVE_DEFAULT_VOICE
    assert validate_live_voice(None) == GPT_LIVE_DEFAULT_VOICE


def test_validate_live_voice_known_passthrough():
    assert validate_live_voice("vesper") == "vesper"


def test_validate_live_voice_known_case_insensitive():
    assert validate_live_voice("Stone") == "stone"


def test_validate_live_voice_unknown_falls_back_with_warning(caplog):
    """The exact regression a review sabotage-probe planted (letting an
    unknown voice like a leftover xAI Realtime name pass through
    unvalidated) and confirmed CI did NOT catch via the livekit-gated test."""
    with caplog.at_level("WARNING"):
        result = validate_live_voice("ara")  # xAI Realtime voice name, invalid here

    assert result == GPT_LIVE_DEFAULT_VOICE
    assert any("not a known gpt-live-1 voice" in r.message for r in caplog.records)


def test_all_known_voices_accepted():
    for name in GPT_LIVE_KNOWN_VOICES:
        assert validate_live_voice(name) == name


# ────────────────────────────────────────────────────────────────────────
# urgent_note()
# ────────────────────────────────────────────────────────────────────────


def test_urgent_note_none_for_plain_open_tasks():
    briefing = {"open_tasks": [{"status": "inbox"}, {"status": "in_progress"}], "open_approvals_count": 0}
    assert urgent_note(briefing) is None


def test_urgent_note_none_when_empty():
    assert urgent_note({"open_tasks": [], "open_approvals_count": 0}) is None


def test_urgent_note_single_approval():
    note = urgent_note({"open_tasks": [], "open_approvals_count": 1})
    assert note is not None
    assert "Approval" in note


def test_urgent_note_multi_approvals_mentions_count():
    note = urgent_note({"open_tasks": [], "open_approvals_count": 3})
    assert "3" in note


def test_urgent_note_blocked_task_named():
    briefing = {
        "open_tasks": [{"status": "blocked", "title": "Fix deploy pipeline"}],
        "open_approvals_count": 0,
    }
    note = urgent_note(briefing)
    assert note is not None
    assert "Fix deploy pipeline" in note


def test_urgent_note_blocked_task_long_title_generic():
    briefing = {
        "open_tasks": [{
            "status": "blocked",
            "title": "This is a very long task title that exceeds forty characters easily",
        }],
        "open_approvals_count": 0,
    }
    note = urgent_note(briefing)
    assert note is not None
    assert "This is a very long" not in note  # generic phrasing, not the raw title


# ────────────────────────────────────────────────────────────────────────
# build_greeting() — including the exact regression a review sabotage-probe
# planted and confirmed CI did NOT catch via the livekit-gated test.
# ────────────────────────────────────────────────────────────────────────


def test_build_greeting_never_mentions_task_counts():
    briefing = {
        "open_tasks": [{"status": "inbox"}] * 10,
        "open_approvals_count": 0,
    }
    for _ in range(20):  # random.choice — sample the whole pool
        text = build_greeting(briefing, operator_name="Mark")
        assert "10" not in text


def test_build_greeting_mentions_urgent_approval():
    briefing = {"open_tasks": [], "open_approvals_count": 1}
    text = build_greeting(briefing, operator_name="Mark")
    assert "Approval" in text


def test_build_greeting_fallback_without_briefing():
    text = build_greeting(None, operator_name="Mark")
    assert "Mark" in text


def test_build_greeting_never_says_abend_in_the_morning():
    """Review Finding 5: an earlier version drew 'Abend' via random.choice
    regardless of the actual time of day. Greeting wording must respect
    briefing['current_time_of_day_de'] (backend/app/routers/vault.py's
    _time_of_day_de bucket) — no 'Abend'/'abends' when the briefing says
    'morgens'."""
    briefing = {"open_tasks": [], "open_approvals_count": 0, "current_time_of_day_de": "morgens"}
    for _ in range(30):
        text = build_greeting(briefing, operator_name="Mark").lower()
        assert "abend" not in text


def test_build_greeting_uses_morning_wording_when_appropriate():
    briefing = {"open_tasks": [], "open_approvals_count": 0, "current_time_of_day_de": "morgens"}
    # Sample many draws — at least one should hit the time-specific pool,
    # proving it's actually wired in (not dead code).
    seen_morning_word = False
    for _ in range(50):
        text = build_greeting(briefing, operator_name="Mark").lower()
        if "morgen" in text:
            seen_morning_word = True
            break
    assert seen_morning_word


def test_build_greeting_uses_evening_wording_when_appropriate():
    briefing = {"open_tasks": [], "open_approvals_count": 0, "current_time_of_day_de": "abends"}
    seen_evening_word = False
    for _ in range(50):
        text = build_greeting(briefing, operator_name="Mark").lower()
        if "abend" in text:
            seen_evening_word = True
            break
    assert seen_evening_word


def test_build_greeting_never_says_morgen_in_the_evening():
    briefing = {"open_tasks": [], "open_approvals_count": 0, "current_time_of_day_de": "abends"}
    for _ in range(30):
        text = build_greeting(briefing, operator_name="Mark").lower()
        assert "morgen" not in text


# ────────────────────────────────────────────────────────────────────────
# track_latency() — pure delegation-latency arithmetic
# ────────────────────────────────────────────────────────────────────────


def test_track_latency_user_then_assistant():
    last_user_at, latency = track_latency(None, "user", 100.0)
    assert last_user_at == 100.0
    assert latency is None

    last_user_at, latency = track_latency(last_user_at, "assistant", 116.3)
    assert last_user_at is None
    assert round(latency, 1) == 16.3


def test_track_latency_assistant_without_prior_user_is_noop():
    last_user_at, latency = track_latency(None, "assistant", 50.0)
    assert last_user_at is None
    assert latency is None


def test_track_latency_unknown_role_passes_through_unchanged():
    last_user_at, latency = track_latency(42.0, "system", 50.0)
    assert last_user_at == 42.0
    assert latency is None


def test_track_latency_resets_after_reporting():
    """A second assistant turn without a fresh user turn must not report a
    stale latency again."""
    last_user_at, _ = track_latency(None, "user", 10.0)
    last_user_at, latency1 = track_latency(last_user_at, "assistant", 13.0)
    assert latency1 == 3.0
    last_user_at, latency2 = track_latency(last_user_at, "assistant", 20.0)
    assert latency2 is None
