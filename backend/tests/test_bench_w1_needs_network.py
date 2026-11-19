"""Bench W1 — a page that needed the internet is not postable (live finding
25.10.2026).

The recorder blocks every request (W0.9). A page that loads its library from
a CDN (``import * as THREE from 'three'`` via an import map) therefore never
draws its scene: the video shows only the CSS background. The gate must say
so: a blocked script/module = red ``needs_network`` (the video is empty, the
system prompt demanded an offline page); other blocked requests only (fonts,
images, stylesheets) = amber ``network_assets_blocked``. A record from a
sidecar that does not report the resource type (no
``network_blocked_scripts``) with attempts > 0 is treated as red — it cannot
prove the scene drew.
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


def _recorder(monkeypatch, **net):
    async def record(entry, challenge):
        out = orch.entry_dir(entry)
        out.mkdir(parents=True, exist_ok=True)
        (out / "recording.mp4").write_bytes(b"rec")
        return {"video_path": str(out / "recording.mp4"), "screenshot_path": "", "network_blocked": True,
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
async def test_a_blocked_script_turns_the_gate_red(session, engines, world, monkeypatch):
    """The live case: three.js from a CDN, blocked — empty scene, not postable."""
    ch, d = await _run(session, monkeypatch, network_attempts=2, network_blocked_scripts=1)
    assert d["gate"]["status"] == "red"
    red = next(r for r in d["gate"]["reasons"] if r["code"] == "needs_network")
    assert red["severity"] == "red" and red["rule"] == "R6"
    a = d["blocks"][0]["contestants"][0]
    assert a["receipt"]["network_blocked_scripts"] == 1
    # The page did not really "load": its scene never drew.
    assert a["summary"]["page_check"] != "loads"
    kit = w1_kit.kit(ch, d)
    assert kit["downloadable"] is False and kit["blocked_reason"]["code"] == "gate_red"
    assert kit["blocked_reason"]["params"]["rule"] == "R6"


@pytest.mark.asyncio
async def test_only_blocked_assets_are_amber_and_printed(session, engines, world, monkeypatch):
    """Fonts/images from the internet: the scene still draws — amber, said."""
    ch, d = await _run(session, monkeypatch, network_attempts=3, network_blocked_scripts=0)
    assert d["gate"]["status"] == "amber"
    assert ("amber", "network_assets_blocked") in _codes(d)
    assert not any(code == "needs_network" for _sev, code in _codes(d))
    assert d["blocks"][0]["contestants"][0]["summary"]["page_check"] == "loads"
    assert w1_kit.kit(ch, d)["downloadable"] is True
    assert "network_assets_blocked" in w1_kit.PUBLIC_AMBER


@pytest.mark.asyncio
async def test_a_record_without_resource_types_and_attempts_is_red(session, engines, world, monkeypatch):
    """An older sidecar does not say what was blocked: attempts > 0 cannot
    prove the scene drew, so it is treated like a blocked script."""
    _ch, d = await _run(session, monkeypatch, network_attempts=2)
    assert d["gate"]["status"] == "red"
    assert ("red", "needs_network") in _codes(d)


@pytest.mark.asyncio
async def test_an_offline_page_has_no_network_reason(session, engines, world, monkeypatch):
    _ch, d = await _run(session, monkeypatch, network_attempts=0, network_blocked_scripts=0)
    assert not {"needs_network", "network_assets_blocked"} & {code for _s, code in _codes(d)}
    assert d["gate"]["status"] in ("green", "amber")


def test_the_record_keeps_the_blocked_script_count():
    entry = BenchEntry(model_label="A", metrics={"network_blocked_scripts": 4})
    orch._apply_record_result(entry, {"video_path": "/v.mp4", "network_blocked": True,
                                      "network_attempts": 2, "network_blocked_scripts": 1})
    assert entry.metrics["network_blocked_scripts"] == 1
    # An older sidecar's answer drops the old value instead of keeping it.
    orch._apply_record_result(entry, {"video_path": "/v.mp4", "network_blocked": True, "network_attempts": 2})
    assert "network_blocked_scripts" not in entry.metrics


def test_gate_rule_needs_no_receipt_field_when_nothing_was_blocked():
    """A receipt without the new field and without attempts stays as it was
    (no new receipt version for old offline tests)."""
    summary = {"time": {"measured": True}, "tokens_out": {"measured": True},
               "cost": {"cost_kind": "local"}, "time_kind": "gen_ms", "page_check": "loads"}
    result = {"summary": summary, "served_model_id": "m", "display_name": "m", "run_class": "A"}
    receipt = {"run_class": "A", "network_blocked": True, "network_attempts": 0, "n_runs": 3,
               "effort_observed": {"source": "probe"}}
    gate = w1_receipt.evaluate_gate(protocol_version="v1", prompt_sha=None, blocks=[], receipts=[(result, receipt, "x")],
                                    public_texts=[], private_words=[], truncation=None, finished=True)
    assert gate["status"] == "green", gate
