"""Bench W1 — lane ``lib:three-local`` (backlog "three.js lokal", 25.10.2026).

The recorder answers a page's CDN request for three.js with MC's one pinned
copy (docker/mc-playwright/record_three.py) instead of blocking it. The
recorder reports that as ``lib_track = "three-local"`` plus what it served
and what the page asked for. Here: the record keeps it, the receipt carries
it, R6 stays quiet for those requests (they were never blocked) but stays red
for anything else, the version mismatch and a test that mixes lanes are
printed (amber) instead of compared silently, and the chips on the video /
card say "three.js r170".
"""
from __future__ import annotations

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.models.bench import BenchEntry  # noqa: E402
from app.verticals.bench_studio import orchestrator as orch  # noqa: E402
from app.verticals.bench_studio import w1_kit, w1_receipt, w1_runner, w1_tests  # noqa: E402

from tests.test_bench_w1_fleet import (  # noqa: E402,F401
    PAGE, engines, factory, no_sleep, seed_fleet, world,
)

SERVED = {"lib_track": "three-local", "libraries_provided": ["three.js r170"],
          "three_requested": ["r160"], "three_version_mismatch": True, "three_import_map_injected": False}


def _recorder(monkeypatch, **net):
    async def record(entry, challenge):
        out = orch.entry_dir(entry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "recording.mp4").write_bytes(b"rec")
        return {"video_path": str(out / "recording.mp4"), "screenshot_path": "", "network_blocked": True,
                "network_attempts": 0, "network_blocked_scripts": 0,
                "libraries_inlined": [], "html_bytes": len(PAGE), "page_blank": False, **net}

    monkeypatch.setattr(orch, "record_entry", record)


async def _run(session, monkeypatch, **net):
    _recorder(monkeypatch, **net)
    fleet = await seed_fleet(session)
    ch = await w1_tests.create_test(
        session, picks=[{"runtime_id": str(fleet["slot"].id), "harness": "api", "efforts": ["default"]}],
        preset="standard", trigger="runtimes", user_id=None)
    await w1_runner.run_test(ch.id, factory, sleep=no_sleep)
    await session.refresh(ch)
    return ch, await w1_tests.test_detail(session, ch)


def _codes(d):
    return {(r["severity"], r["code"]) for r in d["gate"]["reasons"]}


@pytest.mark.asyncio
async def test_a_served_three_page_is_not_red_and_says_its_lane(session, engines, world, monkeypatch):
    ch, d = await _run(session, monkeypatch, **SERVED)
    codes = _codes(d)
    assert ("red", "needs_network") not in codes
    assert d["gate"]["status"] != "red"
    a = d["blocks"][0]["contestants"][0]
    rec = a["receipt"]
    assert rec["lib_track"] == "three-local"
    assert rec["libraries_provided"] == ["three.js r170"]
    assert rec["three_requested"] == ["r160"] and rec["three_version_mismatch"] is True
    # the served copy is not a pasted library
    assert rec["libraries_inlined"] == []
    assert a["lib_track"] == "three-local"
    assert a["summary"]["page_check"] == "loads"
    # the mismatch is printed, not hidden
    assert ("amber", "three_version_differs") in codes
    assert "three_version_differs" in w1_kit.PUBLIC_AMBER
    assert w1_kit.kit(ch, d)["downloadable"] is True


@pytest.mark.asyncio
async def test_the_lane_is_on_the_video_chips_the_card_and_the_settings_text(session, engines, world, monkeypatch):
    _ch, d = await _run(session, monkeypatch, **SERVED)
    frame = w1_kit.frame_payload(d)
    assert "three.js r170" in frame["panes"][0]["chips"]
    card = w1_kit.card_payload(d)
    assert "three.js r170" in card["contestants"][0]["chips"]
    settings = " ".join(w1_kit.settings_blocks(d))
    assert "three.js r170 served offline by the recorder" in settings
    assert "page asked for r160" in settings
    # the hint for anyone who opens the page elsewhere: it needs three.js
    assert "page loads three.js from a CDN" in settings


@pytest.mark.asyncio
async def test_same_version_is_no_amber(session, engines, world, monkeypatch):
    _ch, d = await _run(session, monkeypatch, **{**SERVED, "three_requested": ["r170"],
                                                  "three_version_mismatch": False})
    assert ("amber", "three_version_differs") not in _codes(d)
    assert "page asked for" not in " ".join(w1_kit.settings_blocks(d))


@pytest.mark.asyncio
async def test_anything_else_blocked_stays_red(session, engines, world, monkeypatch):
    """three served, but an addon MC does not ship (GLTFLoader) was blocked."""
    _ch, d = await _run(session, monkeypatch, **{**SERVED, "network_attempts": 1, "network_blocked_scripts": 1})
    assert ("red", "needs_network") in _codes(d)


@pytest.mark.asyncio
async def test_a_page_without_three_is_lane_none_and_has_no_three_chip(session, engines, world, monkeypatch):
    _ch, d = await _run(session, monkeypatch, lib_track="none", libraries_provided=[])
    a = d["blocks"][0]["contestants"][0]
    assert a["receipt"]["lib_track"] == "none" and a["lib_track"] == "none"
    assert "three.js" not in w1_kit.frame_payload(d)["panes"][0]["chips"]
    assert not {"three_version_differs", "lib_tracks_mixed"} & {c for _s, c in _codes(d)}


def test_the_record_keeps_the_lane_and_an_older_sidecar_drops_it():
    entry = BenchEntry(model_label="A", metrics={})
    orch._apply_record_result(entry, {"video_path": "/v.mp4", "network_blocked": True, "network_attempts": 0,
                                      "network_blocked_scripts": 0, **SERVED})
    for key, value in SERVED.items():
        assert entry.metrics[key] == value
    orch._apply_record_result(entry, {"video_path": "/v.mp4", "network_blocked": True, "network_attempts": 0})
    assert not set(SERVED) & set(entry.metrics)


def _result(key, lane):
    summary = {"time": {"measured": True, "min": 1, "max": 2, "median": 1}, "tokens_out": {"measured": True},
               "cost": {"cost_kind": "local"}, "time_kind": "gen_ms", "page_check": "loads"}
    receipt = {"run_class": "A", "network_blocked": True, "network_attempts": 0, "n_runs": 3,
               "effort_observed": {"source": "probe"}}
    if lane:
        receipt["lib_track"] = lane
    result = {"key": key, "summary": summary, "served_model_id": key, "display_name": key, "run_class": "A",
              "receipt": receipt, "lib_track": lane}
    return result, receipt


def _gate(*pairs):
    blocks = [{"run_class": "A", "contestants": [r for r, _rec in pairs]}]
    return w1_receipt.evaluate_gate(protocol_version="v1", prompt_sha=None, blocks=blocks,
                                    receipts=[(r, rec, "x") for r, rec in pairs],
                                    public_texts=[], private_words=[], truncation=None, finished=True)


def test_a_test_that_mixes_lanes_says_so():
    gate = _gate(_result("a", "three-local"), _result("b", "none"))
    mixed = [r for r in gate["reasons"] if r["code"] == "lib_tracks_mixed"]
    assert len(mixed) == 1 and mixed[0]["severity"] == "amber"
    assert mixed[0]["params"] == {"tracks": ["none", "three-local"]}
    assert "lib_tracks_mixed" in w1_kit.PUBLIC_AMBER


def test_one_lane_is_not_mixed_and_an_old_receipt_is_lane_none():
    assert not [r for r in _gate(_result("a", "three-local"), _result("b", "three-local"))["reasons"]
                if r["code"] == "lib_tracks_mixed"]
    # receipts from before the lane existed count as "none" (no three was served)
    assert not [r for r in _gate(_result("a", None), _result("b", "none"))["reasons"]
                if r["code"] == "lib_tracks_mixed"]
    assert [r for r in _gate(_result("a", None), _result("b", "three-local"))["reasons"]
            if r["code"] == "lib_tracks_mixed"]


def test_an_old_receipt_keeps_its_checksum():
    """No lane reported -> no new receipt keys (old receipts are not re-versioned)."""
    from app.models.bench import BenchChallenge

    ch = BenchChallenge(title="t", prompt_text="p", protocol_version="v1")
    head = BenchEntry(model_label="A", repeat_idx=1, status="rendered",
                      metrics={"network_blocked": True, "network_attempts": 0})
    rec = w1_receipt.build_receipt(ch, {"run_class": "A", "video": True}, [head])
    assert not {"lib_track", "libraries_provided", "three_requested", "three_version_mismatch",
                "three_import_map_injected"} & set(rec)
