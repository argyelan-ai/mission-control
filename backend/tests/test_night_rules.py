"""Night shift — pure rules: window edges, order, box lanes, cloud share,
blocked detection, morning report text (app/services/heads/night.py)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.heads import night
from app.services.heads.night import CLOUD_LANE, Candidate, NightConfig


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC) if "+" not in s else datetime.fromisoformat(s)


CFG = NightConfig(enabled=True, start="22:00", end="06:00", timezone="UTC", cloud_share=30)


# ── Window edges ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "now,night_key",
    [
        ("2026-09-24T21:59:59", None),  # one second before start
        ("2026-09-24T22:00:00", "2026-09-24"),  # start is inside
        ("2026-09-24T23:59:59", "2026-09-24"),
        ("2026-09-25T00:00:00", "2026-09-24"),  # after midnight: still the night that began yesterday
        ("2026-09-25T05:59:59", "2026-09-24"),
        ("2026-09-25T06:00:00", None),  # end is outside
        ("2026-09-25T12:00:00", None),
    ],
)
def test_window_edges_across_midnight(now, night_key):
    w = night.current_window(at(now), CFG)
    assert (w.night if w else None) == night_key


def test_window_same_day():
    cfg = NightConfig(start="01:00", end="05:00", timezone="UTC")
    assert night.current_window(at("2026-09-25T00:59:00"), cfg) is None
    assert night.current_window(at("2026-09-25T01:00:00"), cfg).night == "2026-09-25"
    assert night.current_window(at("2026-09-25T05:00:00"), cfg) is None


def test_window_is_read_in_the_operator_zone():
    cfg = NightConfig(start="22:00", end="06:00", timezone="Europe/Berlin")
    # 20:30 UTC = 22:30 in Berlin (summer time, UTC+2) → inside
    assert night.current_window(at("2026-09-24T20:30:00"), cfg).night == "2026-09-24"
    # 21:30 UTC in winter = 22:30 Berlin (UTC+1) → inside; 20:30 UTC → outside
    assert night.current_window(at("2026-12-01T21:30:00"), cfg).night == "2026-12-01"
    assert night.current_window(at("2026-12-01T20:30:00"), cfg) is None
    w = night.window_of_night("2026-09-24", cfg)
    assert w.starts_at == at("2026-09-24T20:00:00") and w.ends_at == at("2026-09-25T04:00:00")


def test_next_window():
    assert night.next_window(at("2026-09-25T12:00:00"), CFG).night == "2026-09-25"
    assert night.next_window(at("2026-09-25T03:00:00"), CFG).night == "2026-09-24"  # inside the 24th


@pytest.mark.parametrize(
    "over,bad",
    [
        ({}, []),
        ({"start": "24:00"}, ["start"]),
        ({"end": "6:00"}, ["end"]),
        ({"start": "06:00", "end": "06:00"}, ["end"]),
        ({"timezone": "Mars/Base"}, ["timezone"]),
        ({"cloud_share": 101}, ["cloud_share"]),
        ({"cloud_share": -1}, ["cloud_share"]),
    ],
)
def test_validate_config(over, bad):
    base = {"enabled": True, "start": "22:00", "end": "06:00", "timezone": "Europe/Berlin", "cloud_share": 30}
    assert night.validate_config(NightConfig(**{**base, **over})) == bad


# ── Order, lanes, cloud share ──────────────────────────────────────────────


def cand(tid, order, lanes=("box-a",), locality="local"):
    return Candidate(task_id=tid, locality=locality, lanes=list(lanes), order=order)


def test_marking_order_decides():
    # ids sort the other way round: only the marking time may decide
    q = [cand("b-second", 2), cand("c-first", 1), cand("a-third", 3, lanes=("box-b",))]
    pick = night.pick_next(q, set(), night_total=3, cloud_started=0, cloud_share=30)
    assert pick.ready == ["c-first", "b-second", "a-third"] and pick.task_id == "c-first"


def test_busy_box_waits_and_a_free_box_overtakes():
    q = [cand("a1", 1, lanes=("box-a",)), cand("b1", 2, lanes=("box-b",))]
    pick = night.pick_next(q, {"box-a"}, night_total=2, cloud_started=0, cloud_share=30)
    assert pick.task_id == "b1"
    assert pick.waiting == {"a1": "lane_busy"}


def test_duo_recipe_needs_every_box_free():
    q = [cand("duo", 1, lanes=("box-a", "box-b"))]
    assert night.pick_next(q, {"box-b"}, night_total=1, cloud_started=0, cloud_share=30).task_id is None


def test_cloud_share_is_floor_of_the_night_but_at_least_one():
    cloud = cand("c", 1, lanes=(CLOUD_LANE,), locality="cloud")
    # 1 or 3 marks at 30 % → floor = 0, but one cloud start per night is allowed
    assert night.pick_next([cloud], set(), night_total=1, cloud_started=0, cloud_share=30).task_id == "c"
    assert night.pick_next([cloud], set(), night_total=3, cloud_started=0, cloud_share=30).task_id == "c"
    assert night.pick_next([cloud], set(), night_total=3, cloud_started=1, cloud_share=30).waiting == {"c": "cloud_share"}
    # 0 % = no cloud at all
    assert night.pick_next([cloud], set(), night_total=10, cloud_started=0, cloud_share=0).waiting == {"c": "cloud_share"}
    # 10 marks at 30 % → 3; two started → one more allowed, three started → none
    assert night.pick_next([cloud], set(), night_total=10, cloud_started=2, cloud_share=30).task_id == "c"
    assert night.pick_next([cloud], set(), night_total=10, cloud_started=3, cloud_share=30).task_id is None
    # 100 % = no limit
    assert night.pick_next([cloud], set(), night_total=1, cloud_started=5, cloud_share=100).task_id == "c"


def test_cloud_share_never_holds_back_local_pairs():
    q = [cand("local", 1)]
    assert night.pick_next(q, set(), night_total=1, cloud_started=9, cloud_share=0).task_id == "local"


# ── Blocked ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,silent,hb,kind",
    [
        ("running", 60, 10, None),
        ("running", 14 * 60 + 59, 10, None),
        ("running", 15 * 60, 10, "silent"),  # no output for 15 min
        ("running", 30, 15 * 60, "silent"),  # no heartbeat for 15 min
        ("starting", None, None, None),
        ("needs_you", None, None, "needs_you"),  # waits on a question
        ("passed", 99999, 99999, None),
        ("failed", None, None, None),
    ],
)
def test_blocked_kind(state, silent, hb, kind):
    assert night.blocked_kind(state, silent_s=silent, heartbeat_age_s=hb) == kind


# ── Morning report ─────────────────────────────────────────────────────────

BASE = "https://mc.example"


def test_report_groups_every_outcome_with_links():
    entries = [
        {"task_id": "t1", "title": "Fix retry", "started": True, "state": "passed", "pr_url": "https://github.com/o/r/pull/7"},
        {"task_id": "t2", "title": "Refactor", "started": True, "state": "failed", "reason": "time_limit"},
        {"task_id": "t3", "title": "Remove endpoint", "started": True, "state": "needs_you"},
        {"task_id": "t4", "title": "Docs", "started": True, "state": "running", "blocked": "silent"},
        {"task_id": "t5", "title": "Later", "started": False, "reason": "engine_not_ready"},
        {"task_id": "t6", "title": "Stopped one", "started": True, "state": "stopped", "reason": "stopped"},
    ]
    text = night.format_report("2026-09-24", entries, base_url=BASE + "/", lang="en")
    assert text.splitlines()[0] == "Night shift 2026-09-24: 1 passed · 2 failed · 1 needs you · 2 blocked"
    assert "- Fix retry https://mc.example/tasks?task=t1 PR: https://github.com/o/r/pull/7" in text
    assert "- Refactor (time_limit) https://mc.example/tasks?task=t2" in text
    assert "Needs you (1)\n- Remove endpoint https://mc.example/tasks?task=t3" in text
    assert "- Docs (silent) https://mc.example/tasks?task=t4" in text
    assert "- Later (not started, engine_not_ready) https://mc.example/tasks?task=t5" in text
    assert "Still running" not in text


def test_report_still_running_and_german():
    entries = [{"task_id": "t1", "title": "Long job", "started": True, "state": "running"}]
    text = night.format_report("2026-09-24", entries, base_url=BASE, lang="de")
    assert text.startswith("Nachtschicht 2026-09-24: 0 bestanden · 0 fehlgeschlagen · 0 braucht dich · 0 blockiert · 1 läuft noch")
    assert "Läuft noch (1)" in text


def test_report_empty_night():
    assert night.format_report("2026-09-24", [], base_url=BASE) == "Night shift 2026-09-24: nothing was marked for tonight."


def test_notice_text():
    text = night.format_notice("silent", title="Docs", task_id="t4", base_url=BASE, silent_s=17 * 60)
    assert text == "Night shift: head silent for 17 min — Docs\nhttps://mc.example/tasks?task=t4"
    assert night.format_notice("needs_you", title="X", task_id="t", base_url=BASE, silent_s=None,
                               lang="de").startswith("Nachtschicht: Head braucht dich — X")


# ── Who may be marked / started ────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,run_control,ok",
    [
        ("inbox", None, True),
        ("inbox", "manual_hold", True),
        ("review", "manual_hold", True),
        ("inbox", "stopped", False),
        ("in_progress", None, False),
        ("review", None, False),
        ("blocked", None, False),
        ("done", "manual_hold", False),
        ("aborted", None, False),
    ],
)
def test_markable_only_when_nobody_works_on_it(status, run_control, ok):
    assert night.markable(status, run_control) is ok


@pytest.mark.parametrize(
    "status,run_control,ok",
    [
        ("inbox", "manual_hold", True),
        ("review", "manual_hold", True),
        ("inbox", None, False),  # hold lifted by day: the fleet may take it any moment
        ("in_progress", None, False),
        ("done", "manual_hold", False),
    ],
)
def test_still_ours_needs_the_hold(status, run_control, ok):
    assert night.still_ours(status, run_control) is ok


# ── Report length ──────────────────────────────────────────────────────────


def test_long_report_stays_below_the_chat_limit_and_says_how_many_are_left_out():
    entries = [{"task_id": f"00000000-0000-0000-0000-{i:012d}", "title": f"Job number {i} " + "x" * 60,
                "started": False, "reason": "lane_busy"} for i in range(60)]
    text = night.format_report("2026-09-24", entries, base_url="https://mc.example")
    assert len(text) <= night.REPORT_MAX_CHARS
    assert text.splitlines()[0].endswith("60 blocked")
    shown = sum(1 for line in text.splitlines() if line.startswith("- "))
    assert 0 < shown < 60
    assert text.splitlines()[-1] == f"… and {60 - shown} more: https://mc.example/tasks"


def test_short_report_is_not_cut():
    entries = [{"task_id": "00000000-0000-0000-0000-000000000001", "title": "Job", "started": False}]
    text = night.format_report("2026-09-24", entries, base_url="https://mc.example")
    assert "more:" not in text
