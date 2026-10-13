"""Live voice — the 12 scenarios (voice PR 6, bauplan part C) and where each is pinned.

The scenarios come from the Grok behaviour table (talking while the agent
works) plus OpenAI's failure modes. Each one is an automated test — backend
(pytest, the guards MC enforces) or frontend (vitest, the call driven through
a fake browser). This register fails when a scenario loses its test, so the
list cannot rot silently.

Not automated: whether the real model *says* "done" too early or ignores a
stop in audio (that needs a live call with OpenAI and costs money). The
guards that catch it are pinned here (S3, S6); the live check is in the PR's
live test plan.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
FRONTEND = BACKEND.parent.parent / "frontend-v2" / "src" / "components" / "voice" / "live"

# (scenario, [(file, test name)]) — backend names are function names,
# frontend names are the `it("…")` titles (prefix match).
SCENARIOS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "S1 a request while the agent works is added, never a cancel",
        [
            ("py:test_voice_live_tools.py", "test_send_queues_on_busy_acp_agent_with_verbatim_and_marker"),
            ("py:test_voice_live_stop.py", "test_normal_request_still_passes_on_while_working"),
        ],
    ),
    (
        "S2 'Stopp' on its own stops the turn (Stop button path)",
        [
            ("py:test_voice_live_stop.py", "test_stop_runs_the_stop_button_path_on_acp"),
            ("py:test_voice_live_stop.py", "test_stop_on_tui_sends_escape"),
        ],
    ),
    (
        "S3 'Stopp den Container' is a request, never a stop",
        [
            ("py:test_voice_live_stop.py", "test_stop_refused_when_transcript_is_not_a_pure_stop"),
            ("py:test_voice_live_stop.py", "test_not_stop_phrases"),
        ],
    ),
    (
        "S4 'what are you doing?' is answered without disturbing the agent",
        [
            ("py:test_voice_live_tools.py", "test_status_is_read_only_and_redacted"),
            ("ts:VoiceLive.test.tsx", "get_agent_status: no transcript wait, no echo"),
        ],
    ),
    (
        "S5 a spoken 'ja' never approves",
        [
            ("py:test_voice_live_stop.py", "test_spoken_yes_is_never_typed_into_an_open_prompt"),
            ("ts:VoiceLive.test.tsx", "a spoken 'ja' while an approval waits never reaches an approval endpoint"),
        ],
    ),
    (
        "S6 the voice claims success too early → correction",
        [("ts:VoiceLive.test.tsx", "no-success guard: the voice says done before the agent did")],
    ),
    (
        "S7 talking over the voice silences only the voice",
        [
            ("py:test_voice_live_stop.py", "test_barge_in_is_server_vad_interrupt_only"),
            ("ts:VoiceLive.test.tsx", "talking over the voice silences only the voice"),
        ],
    ),
    (
        "S8 agent offline → 'NOT delivered', said honestly",
        [
            ("py:test_voice_live_tools.py", "test_delivery_failure_is_said_honestly"),
            ("ts:VoiceLive.test.tsx", "not delivered (MC refuses / agent offline)"),
        ],
    ),
    (
        "S9 OpenAI quota / 429 at the start → honest message, no call",
        [
            ("py:test_voice_live.py", "test_upstream_errors_map_to_reason_without_key"),
            ("ts:VoiceLive.test.tsx", "S9 OpenAI 429 on the offer"),
        ],
    ),
    (
        "S10 OpenAI quota mid-call → honest message, clean hang-up, still booked",
        [
            ("ts:VoiceLive.test.tsx", "S10 quota mid-call"),
            ("ts:VoiceLive.test.tsx", "S10 quota reported only as a failed response"),
        ],
    ),
    (
        "S11 two minutes of silence → warning, then hang-up",
        [
            ("ts:VoiceLive.test.tsx", "S11 silence"),
            ("ts:VoiceLive.test.tsx", "S11 the voice talking counts as activity"),
        ],
    ),
    (
        "S12 daily budget and max length end the call (browser and server)",
        [
            ("py:test_voice_live_usage.py", "test_budget_reached_refuses_new_call_before_openai"),
            ("py:test_voice_live_usage.py", "test_usage_tells_a_running_call_to_hang_up_on_budget"),
            ("py:test_voice_live_usage.py", "test_tools_refused_after_max_length"),
            ("py:test_voice_live_usage.py", "test_server_timer_hangs_up_at_max_length"),
            ("ts:VoiceLive.test.tsx", "S12 budget used up during a call"),
            ("ts:VoiceLive.test.tsx", "S12 budget reached before the call"),
        ],
    ),
]


def test_twelve_scenarios():
    assert len(SCENARIOS) == 12
    assert [s[0].split()[0] for s in SCENARIOS] == [f"S{i}" for i in range(1, 13)]


@pytest.mark.parametrize("scenario,tests", SCENARIOS, ids=[s[0].split()[0] for s in SCENARIOS])
def test_every_scenario_has_its_tests(scenario, tests):
    for where, name in tests:
        kind, fname = where.split(":", 1)
        if kind == "py":
            src = (BACKEND / fname).read_text(encoding="utf-8")
            assert re.search(rf"^(async )?def {re.escape(name)}\(", src, re.M), f"{scenario}: {fname}::{name} missing"
        else:
            path = FRONTEND / fname
            if not path.exists():
                pytest.skip("frontend sources not in this checkout (backend-only image)")
            src = path.read_text(encoding="utf-8")
            assert f'it("{name}' in src, f"{scenario}: {fname} it({name!r}) missing"
