"""Ratchet: harness-/runtime_type-name comparisons must not spread further
outside the harness layer PRINCIPLES §3.4 and ARCHITECTURE.md name
(``harness_compat.py``, ``harness_catalog.py``).

This test does not forbid harness-name comparisons outright — some already
exist outside that layer and moving all of them is a separate, larger job.
It only holds today's count per file as a ceiling: a new comparison (or a
new file growing one) is a regression and fails the build. Lowering a
file's count below its baseline is always allowed and encouraged; the
failure message below says how.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.services.harness_comparison_scan import scan_repo

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).resolve().parent / "data" / "harness_comparison_baseline.json"


def _load_baseline() -> dict[str, int]:
    with BASELINE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_baseline_file_is_valid_json_map_of_nonnegative_ints():
    baseline = _load_baseline()
    assert isinstance(baseline, dict)
    assert baseline, "baseline must not be empty — the scan currently finds comparisons"
    for path, count in baseline.items():
        assert isinstance(path, str)
        assert isinstance(count, int) and count > 0, path


def test_harness_comparisons_outside_the_harness_layer_do_not_increase():
    baseline = _load_baseline()
    current = scan_repo(REPO_ROOT)

    regressions = []
    for path, count in sorted(current.items()):
        allowed = baseline.get(path, 0)
        if count > allowed:
            regressions.append(f"  {path}: {count} (was {allowed})")

    if regressions:
        raise AssertionError(
            "Harness-/runtime_type-name comparison ratchet failed.\n"
            "New or growing comparisons against a harness or runtime_type "
            "string outside the harness layer (PRINCIPLES §3.4: "
            "harness_compat.py, harness_catalog.py):\n"
            + "\n".join(regressions)
            + "\n\nHow to fix: move the branching into harness_compat.py / "
            "harness_catalog.py (the one place allowed to know harness "
            "names), or express it as a registry/adapter lookup instead of "
            "a literal string comparison (see HOST_ADAPTERS in "
            "host_harness_adapter.py for the pattern). If the count cannot "
            "go down yet, that is an existing gap, not a new one — do not "
            "raise the ceiling in "
            "backend/tests/data/harness_comparison_baseline.json to hide a "
            "new comparison; only lower a file's number once the code "
            "backing it was actually removed."
        )


def test_removing_a_known_comparison_line_turns_the_ratchet_red():
    """Sabotage probe, run inline so CI always re-proves the ratchet works.

    Takes one real comparison line out of the scanned text (in memory, not
    on disk) and re-counts: the ratchet must now see fewer comparisons than
    the baseline allows for *some* other file once we inject one instead —
    this test injects a NEW comparison line into a file's text and asserts
    the scanner's count for that file would exceed its baseline, proving
    the pattern that guards task_runner.py actually fires.

    Covers both the bare name (``runtime_type == "omp"``) and the
    prefixed-name style (``effective_harness == "omp"``), since that
    prefixed/suffixed snake_case form is the dominant way this codebase
    actually writes these comparisons (task_runner.py, agent_runtime_switch.py,
    runtime_propagation.py all use it) — a sabotage probe that only tries the
    bare form would pass even if the pattern silently missed that shape.
    """
    baseline = _load_baseline()
    from app.services.harness_comparison_scan import _PATTERN

    target = "backend/app/services/task_runner.py"
    task_runner_path = REPO_ROOT / target
    original_text = task_runner_path.read_text(encoding="utf-8")
    baseline_count = baseline.get(target, 0)

    for sabotage_line in (
        '\n    if runtime_type == "omp":\n        pass\n',
        '\n    if effective_harness == "omp":\n        pass\n',
    ):
        injected_count = len(_PATTERN.findall(original_text + sabotage_line))
        assert injected_count > baseline_count, (
            f"sabotage line {sabotage_line!r} did not increase the match "
            "count — the pattern is not catching this shape"
        )
