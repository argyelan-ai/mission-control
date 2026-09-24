"""A10 + A11 — heads_root from settings, derive_head_state (pure)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.services.heads.state import (
    HEARTBEAT_FRESH_S,
    NOT_PICKED_UP_S,
    VANISHED_S,
    derive_head_state,
)

NOW = 1_800_000_000.0
HEADS_PKG = Path(__file__).resolve().parents[1] / "app" / "services" / "heads"


def test_no_path_home_in_heads_services():
    """Inside the container $HOME is not the operator's (spec §6.1)."""
    for py in HEADS_PKG.glob("*.py"):
        assert not re.search(r"Path\.home\(\)|expanduser\(", py.read_text()), py.name


def test_heads_root_default_sits_next_to_the_vault():
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["heads_root"].default == fields["vault_path"].default.parent / "heads"
    assert fields["heads_enabled"].default is False


def _d(status=None, **kw):
    args = dict(
        status=status, created_ts=NOW - 10, heartbeat_mtime=None, last_output_ts=None,
        run_record_passed=False, question_exists=False, stop_requested=False, now=NOW,
    )
    args.update(kw)
    return derive_head_state(**args)


def test_spooled_not_yet_picked_up_is_starting():
    assert _d()["state"] == "starting"


def test_never_picked_up_is_failed_with_reason():
    out = _d(created_ts=NOW - NOT_PICKED_UP_S - 1)
    assert (out["state"], out["reason"]) == ("failed", "not_picked_up")


def test_phase_starting():
    assert _d({"phase": "starting"})["state"] == "starting"


def test_running_with_fresh_heartbeat_reports_silence():
    out = _d({"phase": "running"}, heartbeat_mtime=NOW - 20, last_output_ts=NOW - 1000)
    assert out["state"] == "running"
    assert out["silent_s"] == 1000


def test_stale_heartbeat_but_live_pid_is_still_running():
    out = _d({"phase": "running"}, heartbeat_mtime=NOW - VANISHED_S * 3, pid_alive=True)
    assert out["state"] == "running" and out["heartbeat_stale"] is True


def test_stale_heartbeat_and_pid_gone_is_process_vanished():
    out = _d({"phase": "running"}, heartbeat_mtime=NOW - HEARTBEAT_FRESH_S - 5, pid_alive=False)
    assert (out["state"], out["reason"]) == ("failed", "process_vanished")


def test_backend_without_pid_view_waits_until_vanished_threshold():
    mid = _d({"phase": "running"}, heartbeat_mtime=NOW - HEARTBEAT_FRESH_S - 5)
    assert mid["state"] == "running" and mid["heartbeat_stale"]
    late = _d({"phase": "running"}, heartbeat_mtime=NOW - VANISHED_S - 1)
    assert late["reason"] == "process_vanished"


def test_stopped():
    assert _d({"phase": "exited", "reason": "stopped"})["state"] == "stopped"
    # stop before the host picked the run up
    assert _d(None, stop_requested=True)["state"] == "stopped"


def test_late_stop_does_not_turn_a_passed_run_into_stopped():
    """Review finding: the UI polls every 10 s, so Stop can hit a run that
    has just passed. Once exited, only the wrapper's reason counts."""
    ok = {"phase": "exited", "exit_code": 0, "reason": None, "pr_url": "https://github.com/o/r/pull/1"}
    assert _d(ok, run_record_passed=True, stop_requested=True)["state"] == "passed"
    failed = {"phase": "exited", "exit_code": 1, "reason": "exit_1"}
    assert _d(failed, stop_requested=True)["reason"] == "exit_1"


@pytest.mark.parametrize("exit_code", [0, 1, 3])
def test_question_is_needs_you_with_any_exit_code(exit_code):
    out = _d({"phase": "exited", "exit_code": exit_code, "pr_url": None}, question_exists=True)
    assert out["state"] == "needs_you"


def test_passed_needs_wrapper_pr_and_valid_run_record():
    ok = {"phase": "exited", "exit_code": 0, "pr_url": "https://github.com/o/r/pull/1"}
    assert _d(ok, run_record_passed=True)["state"] == "passed"
    assert _d(ok, run_record_passed=False) == {**_d(ok, run_record_passed=False), "state": "failed"}
    assert _d(ok, run_record_passed=False)["reason"] == "run_record_missing"


def test_no_pr_is_failed_no_pr():
    out = _d({"phase": "exited", "exit_code": 0, "pr_url": None}, run_record_passed=True)
    assert (out["state"], out["reason"]) == ("failed", "no_pr")


@pytest.mark.parametrize("reason", ["time_limit", "spec_invalid", "box_busy", "exit_2", "sandbox_required"])
def test_wrapper_reasons_pass_through(reason):
    out = _d({"phase": "exited", "reason": reason})
    assert (out["state"], out["reason"]) == ("failed", reason)


def test_head_written_pr_url_is_ignored(tmp_path, monkeypatch):
    """The PR URL counts only from .wrapper/status.json (wrapper-written)."""
    import json

    from app.config import settings
    from app.services.heads import files
    from app.services.heads.state import derive_for_run

    monkeypatch.setattr(settings, "heads_root", tmp_path)
    run_id = "11111111-1111-4111-8111-111111111111"
    run = tmp_path / run_id
    (run / ".wrapper").mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps({"run_id": run_id, "created_at": "2026-09-23T10:00:00Z"}))
    (run / ".wrapper" / "status.json").write_text(json.dumps({"phase": "exited", "exit_code": 0}))
    # a head can write into its run folder (outside .wrapper) — ignored
    (run / "status.json").write_text(json.dumps({"pr_url": "https://github.com/o/r/pull/9"}))
    (run / "pr_url").write_text("https://github.com/o/r/pull/9")
    loaded = files.load_run(run_id)
    assert loaded.pr_url is None
    assert derive_for_run(loaded, NOW)["state"] == "failed"


# ── scratch repo with a local origin: a pushed branch instead of a PR ──


def test_scratch_branch_pushed_with_passed_record_is_passed_without_pr():
    st = {"phase": "exited", "exit_code": 0, "pr_url": None, "scratch_branch_pushed": True}
    out = _d(st, run_record_passed=True, scratch_branch_pushed=True)
    assert (out["state"], out["reason"]) == ("passed", "scratch_branch_pushed")


def test_scratch_branch_pushed_still_needs_a_passed_run_record():
    st = {"phase": "exited", "exit_code": 0, "pr_url": None}
    out = _d(st, run_record_passed=False, scratch_branch_pushed=True)
    assert (out["state"], out["reason"]) == ("failed", "run_record_missing")


def test_real_repo_without_pr_stays_failed_no_pr():
    """Default (every real repo): no scratch flag → a PR is still required."""
    st = {"phase": "exited", "exit_code": 0, "pr_url": None, "scratch_branch_pushed": True}
    out = _d(st, run_record_passed=True)
    assert (out["state"], out["reason"]) == ("failed", "no_pr")


def test_real_repo_with_pr_passes_without_a_reason():
    st = {"phase": "exited", "exit_code": 0, "pr_url": "https://github.com/o/r/pull/1"}
    assert _d(st, run_record_passed=True)["reason"] is None


def _scratch_run(tmp_path, monkeypatch, *, listed: bool, flag):
    import json

    from app.config import settings
    from app.services.heads import files

    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "heads_root", tmp_path)
    if listed:
        (tmp_path / "scratch-repos").write_text("# scratch\nscratch/probe\n")
    run_id = "22222222-2222-4222-8222-222222222222"
    run = tmp_path / run_id
    (run / ".wrapper").mkdir(parents=True)
    (run / "spec.json").write_text(json.dumps({
        "run_id": run_id, "repo_full_name": "scratch/probe", "created_at": "2026-09-23T10:00:00Z"}))
    (run / ".wrapper" / "status.json").write_text(json.dumps(
        {"phase": "exited", "exit_code": 0, "scratch_branch_pushed": flag}))
    # a head can write into its run folder (outside .wrapper) — ignored
    (run / "status.json").write_text(json.dumps({"scratch_branch_pushed": True}))
    return files.load_run(run_id)


def test_scratch_flag_counts_only_for_a_listed_scratch_repo(tmp_path, monkeypatch):
    assert _scratch_run(tmp_path, monkeypatch, listed=True, flag=True).scratch_branch_pushed is True


def test_scratch_flag_is_ignored_for_an_unlisted_repo(tmp_path, monkeypatch):
    assert _scratch_run(tmp_path, monkeypatch, listed=False, flag=True).scratch_branch_pushed is False


def test_scratch_flag_must_be_true_not_truthy(tmp_path, monkeypatch):
    assert _scratch_run(tmp_path, monkeypatch, listed=True, flag="yes").scratch_branch_pushed is False
    assert _scratch_run(tmp_path / "b", monkeypatch, listed=True, flag=None).scratch_branch_pushed is False
