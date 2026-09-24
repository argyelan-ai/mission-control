"""A1 — mc-head never trusts spec.json (spec §6.1).

The backend container mounts ~/.mc read-write, so a compromised backend
could plant values that become shell arguments on the host. Every field is
checked against a fixed pattern before anything runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.heads_host_helpers import read_status, run_head, write_spec


@pytest.fixture
def mc_home(tmp_path: Path) -> Path:
    home = tmp_path / "mc"
    home.mkdir()
    return home


def test_valid_spec_passes(mc_home):
    run_id = write_spec(mc_home)
    res = run_head(mc_home, "validate", run_id)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "ok"


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo_full_name", "owner/$(touch /tmp/pwned-mc-head)"),
        ("repo_full_name", "owner"),
        ("branch", "main"),
        ("branch", "mc-head/../../main"),
        ("branch", "mc-head/UPPER"),
        ("branch", "head/2026-09-23-x"),  # collides with origin/HEAD on macOS
        ("harness", "kimi"),
        ("harness", "omp; rm -rf ~"),
        ("base_url", "http://host:8000/v1;touch /tmp/x"),
        ("base_url", "file:///etc/passwd"),
        ("model", "-rf"),
        ("model", "m`id`"),
        ("mode", "yolo"),
        ("time_limit_s", 10),
        ("time_limit_s", "600"),
        ("box_keys", ["not-a-uuid"]),
        ("base_branch", "--upload-pack=x"),
        ("restarted_from", "../other"),
        ("runtime_slug", "Bad Slug"),
        ("job_folder", "../../agents/x"),
    ],
)
def test_malicious_or_bad_field_is_refused(mc_home, tmp_path, field, value):
    run_id = write_spec(mc_home, **{field: value})
    res = run_head(mc_home, "validate", run_id)
    assert res.returncode != 0
    assert "spec_invalid" in res.stdout
    assert field in res.stdout


def test_run_id_must_match_folder(mc_home):
    run_id = write_spec(mc_home)
    spec_path = mc_home / "heads" / run_id / "spec.json"
    spec = json.loads(spec_path.read_text())
    spec["run_id"] = "00000000-0000-0000-0000-000000000000"
    spec_path.write_text(json.dumps(spec))
    res = run_head(mc_home, "validate", run_id)
    assert res.returncode != 0 and "run_id" in res.stdout


def test_start_with_bad_spec_writes_exited_and_creates_nothing(mc_home, tmp_path):
    pwned = tmp_path / "pwned"
    run_id = write_spec(mc_home, repo_full_name=f"owner/$(touch {pwned})")
    res = run_head(mc_home, "start", run_id)
    assert res.returncode != 0
    status = read_status(mc_home, run_id)
    assert status and status["phase"] == "exited" and status["reason"] == "spec_invalid"
    assert not pwned.exists()
    assert not (mc_home / "heads" / "clones").exists()
    assert not (mc_home / "heads" / run_id / "wt").exists()


def test_non_uuid_run_id_argument_is_refused(mc_home):
    res = run_head(mc_home, "validate", "../../etc")
    assert res.returncode != 0
