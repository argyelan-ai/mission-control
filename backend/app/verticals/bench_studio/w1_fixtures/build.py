"""Build the W1 contract fixtures (JSON + the TypeScript mirror).

    cd backend && python -m app.verticals.bench_studio.w1_fixtures.build

Every fixture is validated through the pydantic contract
(``w1_schemas``) and written as the exact JSON the API returns
(``model_dump(mode="json")``, defaults included). The same data is written
to ``frontend-v2/src/verticals/bench_studio/w1/fixtures.ts`` with the TS
types as annotations, so ``tsc`` checks the mirror and the vitest drift
guard (``w1/__tests__/contractFixtures.test.ts``) compares both files.

Numbers follow the live record (concept 30 rev. 2): DeepSeek-V4.1-Flash-1017
Pagoda run 65 791 ms / 6 607 tokens → 100.4 tok/s wall average, so decode is
shown as 101 (≥ wall average); Shot Sequence agent run ≈ 92 min work, 0 wait,
264 150 out / 1 117 881 in / 752 708 cache-read. Display names only, never
hosts or IPs.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import w1_schemas as s

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
TS_OUT = REPO / "frontend-v2" / "src" / "verticals" / "bench_studio" / "w1" / "fixtures.ts"

# Stable ids (fixtures only).
RT_SPARK = "11111111-1111-4111-8111-111111111111"
RT_CLOUD_DS = "22222222-2222-4222-8222-222222222222"
RT_CLOUD_PAID = "33333333-3333-4333-8333-333333333333"
RT_ASLEEP = "44444444-4444-4444-8444-444444444444"
HOST_SPARK = "55555555-5555-4555-8555-555555555555"
TPL_PAGODA = "66666666-6666-4666-8666-666666666666"
T_RUNNING = "a0000000-0000-4000-8000-000000000001"
T_GREEN = "a0000000-0000-4000-8000-000000000002"
T_TRUNC = "a0000000-0000-4000-8000-000000000003"
T_RED = "a0000000-0000-4000-8000-000000000004"
T_AUTO = "a0000000-0000-4000-8000-000000000005"
T_CUT = "a0000000-0000-4000-8000-000000000006"
RT_SWITCHING = "77777777-7777-4777-8777-777777777777"
E = "e0000000-0000-4000-8000-0000000000{:02d}".format

MODEL = "DeepSeek-V4.1-Flash-1017"  # model-catalog: allow (fixture data, never a request)
SHA = "d6e0ea9" + "0" * 57
PAGODA = {"template_id": TPL_PAGODA, "title": "Pagoda Garden", "sha256": SHA}


def _device(state: str = "free") -> dict:
    return {"name": "DGX Spark ×2", "nodes": 2, "gpu_mode": "eco", "state": state}


def _effort_ok() -> dict:
    return {
        "status": "ok",
        "options": [
            {"level": "default", "is_default": True, "merged_level": "high"},
            {"level": "off"},
            {"level": "max"},
        ],
        "probed_at": "2026-10-21T20:31:00Z",
        "default_observed": "on",
    }


def _spark_contestant() -> dict:
    return {
        "key": RT_SPARK,
        "runtime_id": RT_SPARK,
        "served_model_id": MODEL,
        "display_name": MODEL,
        "locality": "local",
        "device": _device(),
        "engine": None,
        "recipe_slug": None,
        "spec_decoding": None,
        "serving_since": "2026-10-21T20:12:00Z",
        "harnesses": [
            {
                "harness": "api", "run_class": "A", "available": True,
                "effort_settable": True, "n_runs": 3,
                "estimate": {"min_s": 180, "max_s": 260, "basis": "history"},
            },
            {
                "harness": "omp", "run_class": "B", "available": True,
                "effort_settable": False,
                "effort_note": {"code": "omp_session_default", "params": {"level": "high"}},
                "n_runs": 1,
                "estimate": {"min_s": 240, "max_s": 5520, "basis": "history"},
            },
        ],
        "effort": _effort_ok(),
        "money": {"cost_kind": "local", "hypothetical_usd": 0.04,
                  "price_source": "deepseek-api", "price_date": "2026-10-01"},
        "blockers": [],
        "last_test": {"id": T_GREEN, "title": "Pagoda Garden", "created_at": "2026-10-22T19:40:00Z"},
    }


def _cloud_sub() -> dict:
    return {
        "key": RT_CLOUD_DS,
        "runtime_id": RT_CLOUD_DS,
        "served_model_id": "deepseek-v4.1-flash",  # model-catalog: allow (fixture)
        "display_name": "deepseek-v4.1-flash",  # model-catalog: allow (fixture)
        "locality": "cloud",
        "provider": "Ollama Cloud",
        "harnesses": [
            {"harness": "api", "run_class": "A", "effort_settable": True, "n_runs": 3,
             "estimate": {"min_s": 90, "max_s": 200, "basis": "default"}},
        ],
        "effort": {"status": "unchecked", "options": [{"level": "default", "is_default": True}]},
        "money": {"cost_kind": "subscription", "hypothetical_usd": 0.04,
                  "price_source": "deepseek-api", "price_date": "2026-10-01"},
        "cloud_build_unknown": True,
    }


def _cloud_paid() -> dict:
    return {
        "key": RT_CLOUD_PAID,
        "runtime_id": RT_CLOUD_PAID,
        "served_model_id": "example-frontier-1",
        "display_name": "example-frontier-1",
        "locality": "cloud",
        "provider": "Example API",
        "harnesses": [
            {"harness": "api", "run_class": "A", "effort_settable": True, "n_runs": 3,
             "estimate": {"min_s": 60, "max_s": 150, "basis": "default"}},
        ],
        "effort": {"status": "only_on_off",
                   "options": [{"level": "default", "is_default": True}, {"level": "off"}]},
        "money": {"cost_kind": "api_price", "usd": 1.35, "price_source": "example-api",
                  "price_date": "2026-10-01", "needs_confirmation": True,
                  "second_confirmation": True},
    }


def _receipt_a() -> dict:
    return {
        "protocol_version": "v1", "run_class": "A",
        "served_model_id": MODEL, "display_name": MODEL,
        "locality": "local", "device": "DGX Spark ×2", "nodes": 2,
        "gpu_mode": "eco", "harness": "api",
        "effort_requested": "default", "effort_sent": None,
        "effort_observed": {"thinking": "on", "reasoning_tokens": 2140},
        "thinking_key": "reasoning_effort",
        "temperature": 0.7, "max_tokens": 16384,
        "system_prompt_sha": "4b1c0de" + "0" * 57, "prompt_sha256": SHA,
        "n_runs": 3, "video_run_index": 1, "attempts": 3, "failures": 0,
        "network_blocked": True, "network_attempts": 0, "libraries_inlined": [],
        "html_bytes": 17_204, "quiet_box": "ok", "serving_age_min": 1410,
        "recorded_at": "2026-10-22T19:44:10Z",
        "unknown": {"engine": "hand_made_runtime", "spec_decoding": "no_recipe",
                    "engine_version": "hand_made_runtime"},
    }


def _run(i: int, eid: int, **over) -> dict:
    base = {"entry_id": E(eid), "repeat_idx": i, "state": "done", "phase": "done",
            "finish_reason": "stop", "is_video_run": i == 1}
    base.update(over)
    return base


def _a_result(runs: list[dict], *, truncated: bool = False) -> dict:
    time = {"median": 66_000, "min": 62_000, "max": 71_000, "n": 3}
    out = {"median": 6_607, "min": 6_400, "max": 6_900, "n": 3}
    return {
        "key": RT_SPARK, "runtime_id": RT_SPARK, "served_model_id": MODEL,
        "display_name": MODEL, "locality": "local", "device_name": "DGX Spark ×2",
        "harness": "api",
        "effort": {"requested": "default", "default_level": "high", "sent": None,
                   "observed": {"thinking": "on", "reasoning_tokens": 2140}},
        "runs": runs,
        "summary": {
            "time": time, "time_kind": "gen_ms",
            "tokens_out": out,
            "tokens_reasoning": {"median": 2_140, "min": 1_980, "max": 2_400, "n": 3},
            "decode_tok_s": {"median": 101, "min": 99.5, "max": 103, "n": 3},
            "ttft_ms": {"median": 410, "min": 380, "max": 460, "n": 3},
            "cost": {"cost_kind": "local", "hypothetical_usd": 0.04,
                     "price_source": "deepseek-api", "price_date": "2026-10-01"},
            "page_check": "blank" if truncated else "loads",
        },
        "receipt": _receipt_a(), "receipt_version": 1,
        "receipt_checksum": "c" * 64,
    }


def plan() -> s.BenchPlan:
    std = {"id": "standard", "n_runs": 3, "prompt": PAGODA}
    return s.BenchPlan.model_validate({
        "preset": std,
        "presets": [
            std,
            {"id": "quick", "n_runs": 1, "prompt": PAGODA},
            {"id": "effort_ladder", "n_runs": 3, "multi_effort": True, "prompt": PAGODA},
        ],
        "contestants": [_spark_contestant()],
        "picks": [{"runtime_id": RT_SPARK, "harness": "api", "efforts": ["default"]}],
        "estimate": {"min_s": 180, "max_s": 260, "basis": "history"},
        "money": {"cost_kind": "local", "hypothetical_usd": 0.04,
                  "price_source": "deepseek-api", "price_date": "2026-10-01"},
        "blockers": [],
        "sequential": False,
    })


def contestants() -> s.ContestantList:
    return s.ContestantList.model_validate({
        "local": [_spark_contestant()],
        "cloud": [_cloud_sub(), _cloud_paid()],
    })


def test_running() -> s.BenchTestDetail:
    return s.BenchTestDetail.model_validate({
        "id": T_RUNNING, "title": "Pagoda Garden", "created_at": "2026-10-24T09:12:00Z",
        "trigger": "runtimes", "preset": "standard", "protocol_version": "v1",
        "state": "running", "section": "running",
        "prompt": PAGODA,
        "blocks": [
            {"run_class": "A", "contestants": [{
                **_a_result([
                    _run(1, 1, elapsed_ms=66_000, tokens_out=6_607, decode_tok_s=101, ttft_ms=410),
                    _run(2, 2, state="running", phase="generating", elapsed_ms=41_000,
                         tokens_out=3_100, decode_tok_s=101, finish_reason=None, ttft_ms=395),
                    _run(3, 3, state="waiting", phase="queued", finish_reason=None),
                ]),
                "receipt": None, "receipt_version": None, "receipt_checksum": None,
                "summary": {
                    "time": {"measured": False, "n": 1, "reason": {"code": "runs_pending"}},
                    "tokens_out": {"measured": False, "n": 1, "reason": {"code": "runs_pending"}},
                    "cost": {"cost_kind": "local"},
                },
            }]},
            {"run_class": "B", "starts_after": "A", "contestants": [{
                "key": f"{RT_SPARK}:omp", "runtime_id": RT_SPARK, "served_model_id": MODEL,
                "display_name": MODEL, "locality": "local", "device_name": "DGX Spark ×2",
                "harness": "omp", "effort": {"requested": "default"},
                "runs": [_run(1, 4, state="waiting", phase="queued", finish_reason=None,
                              wait={"code": "after_class_a", "params": {"after_run": 3}})],
                "summary": {
                    "time": {"measured": False, "reason": {"code": "runs_pending"}},
                    "time_kind": "active_ms",
                    "tokens_out": {"measured": False, "reason": {"code": "runs_pending"}},
                    "cost": {"cost_kind": "local"},
                },
            }]},
        ],
        "gate": {"status": "pending"},
        "allowed": ["stop"],
    })


def test_done_green() -> s.BenchTestDetail:
    runs = [_run(1, 11, elapsed_ms=66_000, tokens_out=6_607, decode_tok_s=101, ttft_ms=410),
            _run(2, 12, elapsed_ms=62_000, tokens_out=6_400, decode_tok_s=103, ttft_ms=380),
            _run(3, 13, elapsed_ms=71_000, tokens_out=6_900, decode_tok_s=99.5, ttft_ms=460)]
    res = _a_result(runs)
    return s.BenchTestDetail.model_validate({
        "id": T_GREEN, "title": "Pagoda Garden", "created_at": "2026-10-22T19:40:00Z",
        "trigger": "runtimes", "preset": "standard", "protocol_version": "v1",
        "state": "done", "section": "done",
        "prompt": PAGODA,
        "blocks": [{"run_class": "A", "contestants": [res]}],
        "gate": {"status": "green", "reasons": [], "checksum": "f" * 64},
        "verdict": {"served_model_id": MODEL, "run_class": "A", "n_runs": 3,
                    "time": res["summary"]["time"], "tokens_out": res["summary"]["tokens_out"],
                    "page_check": "loads"},
        "video": {"path": f"/shared-deliverables/bench-{T_GREEN}/grid-1.mp4",
                  "duration_s": 24.5, "run_index": 1},
        "allowed": ["kit", "rerun", "rerender", "recompose", "archive", "delete"],
    })


def test_done_truncated() -> s.BenchTestDetail:
    runs = [_run(1, 21, elapsed_ms=66_000, tokens_out=6_607, decode_tok_s=101, ttft_ms=410),
            _run(2, 22, elapsed_ms=162_000, tokens_out=16_384, decode_tok_s=101,
                 ttft_ms=400, finish_reason="length", truncated=True),
            _run(3, 23, elapsed_ms=71_000, tokens_out=6_900, decode_tok_s=99.5, ttft_ms=460)]
    res = _a_result(runs)
    res["summary"]["page_check"] = "loads"
    return s.BenchTestDetail.model_validate({
        "id": T_TRUNC, "title": "Pagoda Garden", "created_at": "2026-10-23T18:02:00Z",
        "trigger": "runtimes", "preset": "standard", "protocol_version": "v1",
        "state": "done", "section": "needs_look",
        "state_reason": {"code": "runs_truncated", "params": {"runs_cut": 1, "n_runs": 3}},
        "prompt": PAGODA,
        "blocks": [{"run_class": "A", "contestants": [res]}],
        "gate": {"status": "amber", "checksum": "e" * 64, "reasons": [
            {"rule": "amber", "severity": "amber", "code": "runs_truncated",
             "params": {"runs_cut": 1, "n_runs": 3}, "fix": "rerun_larger"},
        ]},
        "verdict": {"served_model_id": MODEL, "run_class": "A", "n_runs": 3,
                    "time": res["summary"]["time"], "tokens_out": res["summary"]["tokens_out"],
                    "page_check": "loads"},
        "video": {"path": f"/shared-deliverables/bench-{T_TRUNC}/grid-1.mp4",
                  "duration_s": 24.5, "run_index": 1},
        "truncation": {"runs_cut": 1, "n_runs": 3, "max_tokens": 16384,
                       "suggested_max_tokens": 32768, "entry_ids": [E(22)]},
        "allowed": ["kit", "rerun", "rerun_larger", "rerender", "recompose", "archive", "delete"],
    })


def test_aborted_truncated() -> s.BenchTestDetail:
    """Every run thought until max_tokens and wrote no page (high/max effort
    on a thinking model): aborted, but the private warning + "larger budget"
    re-run are offered (review MUST-1)."""
    cut = {"code": "no_html_truncated", "params": {}}
    runs = [_run(i, 30 + i, state="failed", elapsed_ms=162_000 + i * 1_000, tokens_out=16_384,
                 decode_tok_s=None, ttft_ms=400, finish_reason="length", truncated=True, error=cut)
            for i in (1, 2, 3)]
    res = _a_result(runs)
    res.update({"receipt": None, "receipt_version": None, "receipt_checksum": None})
    res["summary"] = {
        "time": {"measured": False, "n": 0, "reason": {"code": "all_runs_failed"}},
        "tokens_out": {"measured": False, "n": 0, "reason": {"code": "all_runs_failed"}},
        "cost": {"cost_kind": "local"},
    }
    return s.BenchTestDetail.model_validate({
        "id": T_CUT, "title": "Pagoda Garden", "created_at": "2026-10-24T10:05:00Z",
        "trigger": "runtimes", "preset": "effort_ladder", "protocol_version": "v1",
        "state": "aborted", "section": "needs_look",
        "state_reason": {"code": "all_runs_failed", "params": {}},
        "prompt": PAGODA,
        "blocks": [{"run_class": "A", "contestants": [res]}],
        "gate": {"status": "pending"},
        "truncation": {"runs_cut": 3, "n_runs": 3, "max_tokens": 16384,
                       "suggested_max_tokens": 32768, "entry_ids": [E(31), E(32), E(33)],
                       "runs_without_page": 3, "video_run_cut": False},
        "allowed": ["rerun", "rerun_larger", "archive", "delete"],
    })


def test_needs_look_red() -> s.BenchTestDetail:
    return s.BenchTestDetail.model_validate({
        "id": T_RED, "title": "Shot Sequence", "created_at": "2026-10-21T20:21:40Z",
        "trigger": None, "preset": None, "protocol_version": "v0",
        "state": "done_missing", "section": "needs_look",
        "state_reason": {"code": "numbers_missing", "params": {"fields": ["tokens_out"]}},
        "prompt": {"template_id": None, "title": "Shot Sequence", "sha256": "9" * 64},
        "blocks": [{"run_class": "B", "contestants": [{
            "key": f"{RT_SPARK}:omp", "runtime_id": RT_SPARK, "served_model_id": MODEL,
            "display_name": MODEL, "locality": "local", "device_name": "DGX Spark ×2",
            "harness": "omp",
            "effort": {"requested": "default",
                       "observed": {"source": "omp_session", "level": "high"}},
            "runs": [_run(1, 31, elapsed_ms=5_551_000, finish_reason=None)],
            "summary": {
                "time": {"median": 5_551_000, "min": 5_551_000, "max": 5_551_000, "n": 1},
                "time_kind": "active_ms", "wait_ms": 0,
                "tokens_out": {"measured": False, "n": 1,
                               "reason": {"code": "harvest_pending"}},
                "cost": {"cost_kind": "local"},
            },
            "receipt": {
                "protocol_version": "v0", "run_class": "B",
                "served_model_id": MODEL, "display_name": MODEL, "locality": "local",
                "device": "DGX Spark ×2", "harness": "omp",
                "effort_observed": {"source": "omp_session", "level": "high"},
                "brief_sha": "7" * 64, "tools_allowed": True, "n_runs": 1,
                "attempts": 1, "failures": 0, "network_blocked": False,
                "unknown": {"time_limit_min": "not_recorded",
                            "harness_version": "not_recorded",
                            "network": "recorded_before_block",
                            "engine": "hand_made_runtime",
                            "spec_decoding": "no_recipe"},
            },
            "receipt_version": 1, "receipt_checksum": "b" * 64,
        }]}],
        "gate": {"status": "red", "reasons": [
            {"rule": "R1", "severity": "red", "code": "headline_missing",
             "params": {"field": "tokens_out"}, "fix": "harvest"},
            {"rule": "amber", "severity": "amber", "code": "single_run", "params": {}},
            {"rule": "amber", "severity": "amber", "code": "method_v0", "params": {}},
        ]},
        "allowed": ["harvest", "rerun", "archive", "delete"],
    })


def test_list() -> s.BenchTestList:
    ds = {"display_name": "DeepSeek-V4.1", "harness": "api", "locality": "local"}  # model-catalog: allow (fixture)
    return s.BenchTestList.model_validate({
        "items": [
            {"id": T_RUNNING, "title": "Pagoda Garden", "created_at": "2026-10-24T09:12:00Z",
             "section": "running", "state": "running", "run_class": "A",
             "contestants": [ds], "progress": {"done": 1, "total": 3}},
            {"id": T_AUTO, "title": "Pagoda Garden", "created_at": "2026-10-24T09:30:00Z",
             "section": "running", "state": "waiting", "run_class": "A",
             "state_reason": {"code": "model_fresh", "params": {"until": "2026-10-24T09:45:00Z"}},
             "contestants": [ds]},
            {"id": T_RED, "title": "Shot Sequence", "created_at": "2026-10-21T20:21:40Z",
             "section": "needs_look", "state": "done_missing", "run_class": "B",
             "state_reason": {"code": "numbers_missing", "params": {"fields": ["tokens_out"]}},
             "contestants": [{**ds, "harness": "omp"}], "gate_status": "red"},
            {"id": T_TRUNC, "title": "Pagoda Garden", "created_at": "2026-10-23T18:02:00Z",
             "section": "needs_look", "state": "done", "run_class": "A",
             "state_reason": {"code": "runs_truncated", "params": {"runs_cut": 1, "n_runs": 3}},
             "contestants": [ds], "gate_status": "amber"},
            {"id": T_GREEN, "title": "Pagoda Garden", "created_at": "2026-10-22T19:40:00Z",
             "section": "done", "state": "done", "run_class": "A",
             "contestants": [ds], "gate_status": "green"},
        ],
        "counts": {"running": 2, "needs_look": 2, "done": 1},
        "total": 5,
        "last_at": "2026-10-24T09:30:00Z",
    })


def _kit_texts() -> dict:
    return {
        "settings": [
            "Setup · model only · same prompt (sha d6e0ea9) · n=3, video = run 1 · "
            "temp 0.7 · max_tokens 16384",
            f"{MODEL} · local · DGX Spark ×2 · engine: unknown (hand-made runtime) · "
            "GPU mode eco · spec-decoding: unknown · raw API",
            "effort: server default = high (thinking observed: on) · network blocked during "
            "render · method v1 · 2026-10-22 · API list price would be ≈ $0.04 (comparison only)",
        ],
        # Exactly what w1_kit.alt_text builds for this record (review N7).
        "alt_text": (
            "Result card: Pagoda Garden · same-prompt showdown · 2026-10-22. "
            f"{MODEL}, running locally on DGX Spark ×2, raw API, effort high (default): "
            "compute time 1:06 (median of 3), 6.6k output tokens, 101 tok/s decode. "
            "Median of 3 runs · same prompt."
        ),
        "facts": [
            f"{MODEL}, local, 3 runs: 1:02–1:11 compute time.",
            "Output tokens per run: 6.4k–6.9k.",
            "Decode speed: 99.5–103 tok/s, first token after 0.4 s.",
        ],
        "link": None,
        "thread_guide": [
            "Main post: your words + video.mp4",
            "Reply 1: card.png + alt text",
            "Then: one reply per settings block (each at most 280 characters)",
        ],
    }


def kit_green() -> s.XKit:
    return s.XKit.model_validate({
        "test_id": T_GREEN,
        "gate": {"status": "green", "checksum": "f" * 64},
        "downloadable": True,
        "files": [
            {"name": "video.mp4", "ready": True, "bytes": 18_400_000, "width": 1920,
             "height": 1080, "duration_s": 24.5},
            {"name": "card.png", "ready": True, "bytes": 412_000, "width": 1080, "height": 1350},
            {"name": "kit.zip", "ready": True, "bytes": 18_800_000},
        ],
        "texts": _kit_texts(),
        "footer": "@example_handle",
    })


def kit_red() -> s.XKit:
    texts = _kit_texts()
    texts["facts"] = []
    return s.XKit.model_validate({
        "test_id": T_RED,
        "gate": {"status": "red", "reasons": [
            {"rule": "R1", "severity": "red", "code": "headline_missing",
             "params": {"field": "tokens_out"}, "fix": "harvest"}]},
        "downloadable": False,
        "blocked_reason": {"code": "gate_red", "params": {"rule": "R1"}},
        "files": [
            {"name": "video.mp4", "ready": True, "width": 1920, "height": 1080},
            {"name": "card.png", "ready": False},
            {"name": "kit.zip", "ready": False},
        ],
        "texts": texts,
        "footer": "@example_handle",
    })


def runtime_summary() -> list[s.RuntimeTestSummary]:
    return [
        s.RuntimeTestSummary.model_validate({
            "runtime_id": RT_SPARK, "testable": True, "primary": True,
            "last_test": {"id": T_GREEN, "title": "Pagoda Garden",
                          "created_at": "2026-10-22T19:40:00Z"}}),
        s.RuntimeTestSummary.model_validate({
            "runtime_id": RT_ASLEEP, "testable": False,
            "reason": {"code": "no_model"}}),
        s.RuntimeTestSummary.model_validate({"runtime_id": RT_CLOUD_DS, "testable": True}),
        s.RuntimeTestSummary.model_validate({"runtime_id": RT_CLOUD_PAID, "testable": True}),
        s.RuntimeTestSummary.model_validate({
            "runtime_id": RT_SWITCHING, "testable": False, "reason": {"code": "no_model"},
            "planned_test": {"id": T_AUTO, "not_before": "2026-10-24T09:45:00Z"}}),
    ]


def auto_test() -> s.AutoTestResponse:
    return s.AutoTestResponse.model_validate({
        "test_id": T_AUTO, "state": "waiting", "not_before": "2026-10-24T09:45:00Z"})


def cost_confirmation() -> s.CostConfirmation:
    return s.CostConfirmation.model_validate({"step": 2, "usd": 1.35})


def cost_confirmation_unknown() -> s.CostConfirmation:
    return s.CostConfirmation.model_validate({"step": 1, "usd": None, "price_unknown": True})


# name -> (builder, TS type annotation)
FIXTURES = {
    "plan_standard": (plan, "BenchPlan"),
    "contestants": (contestants, "ContestantList"),
    "test_running": (test_running, "BenchTestDetail"),
    "test_done_green": (test_done_green, "BenchTestDetail"),
    "test_done_truncated": (test_done_truncated, "BenchTestDetail"),
    "test_aborted_truncated": (test_aborted_truncated, "BenchTestDetail"),
    "test_needs_look_red": (test_needs_look_red, "BenchTestDetail"),
    "test_list": (test_list, "BenchTestList"),
    "kit_green": (kit_green, "XKit"),
    "kit_red": (kit_red, "XKit"),
    "runtime_summary": (runtime_summary, "RuntimeTestSummary[]"),
    "auto_test": (auto_test, "AutoTestResponse"),
    "cost_confirmation": (cost_confirmation, "CostConfirmation"),
    "cost_confirmation_unknown": (cost_confirmation_unknown, "CostConfirmation"),
}


def dump(value) -> object:
    if isinstance(value, list):
        return [v.model_dump(mode="json") for v in value]
    return value.model_dump(mode="json")


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p.title() for p in rest) + "Fixture"


def main() -> None:
    types = sorted({t.rstrip("[]") for _, t in FIXTURES.values()})
    ts = [
        "// GENERATED by backend/app/verticals/bench_studio/w1_fixtures/build.py —",
        "// do not edit by hand. The W1 contract fixtures, typed so tsc checks the",
        "// mirror; w1/__tests__/contractFixtures.test.ts compares them with the",
        "// backend JSON files.",
        f"import type {{ {', '.join(types)} }} from \"./types\";",
        "",
    ]
    for name, (builder, ts_type) in FIXTURES.items():
        data = dump(builder())
        (HERE / f"{name}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        ts.append(f"export const {_camel(name)}: {ts_type} = "
                  f"{json.dumps(data, indent=2, ensure_ascii=False)};")
        ts.append("")
    TS_OUT.write_text("\n".join(ts), encoding="utf-8")
    print(f"wrote {len(FIXTURES)} fixtures + {TS_OUT}")


if __name__ == "__main__":
    main()
