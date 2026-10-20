"""Sabotage probes for the three guard runners that have no fixtures of their
own: lint.sh, run-all.sh and sabotage-probe.sh. Each one must turn red on the
case its GATES.md row names, on a copy of tools/guards (the real folder is
never touched)."""
import shutil
import subprocess
from pathlib import Path

import pytest

GUARDS = Path(__file__).resolve().parents[1]

FAKE_GUARD = """#!/usr/bin/env bash
# decides by the file name - its logic block is decoration
# >>> guard-logic
: noop
# <<< guard-logic
case "$1" in *.bad) exit 1 ;; *) exit 0 ;; esac
"""


def run(*cmd):
    return subprocess.run(["bash", *map(str, cmd)], capture_output=True, text=True, timeout=600)


@pytest.fixture
def copy(tmp_path):
    dst = tmp_path / "guards"
    shutil.copytree(GUARDS, dst, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    return dst


def test_lint_sh_is_red_when_one_lint_guard_is_red():
    bad = run(GUARDS / "lint.sh", GUARDS / "fixtures/push-gate.bad")
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "[push-gate] RED" in bad.stdout
    good = run(GUARDS / "lint.sh", GUARDS / "fixtures/push-gate.good")
    assert good.returncode == 0, good.stdout + good.stderr


def test_run_all_sh_is_red_for_a_guard_without_good_fixture(copy):
    (copy / "fake-guard.sh").write_text(FAKE_GUARD)
    (copy / "fixtures/fake-guard.bad").write_text("x\n")
    r = run(copy / "run-all.sh", copy)
    assert r.returncode == 1
    assert "fake-guard" in r.stdout and "needs at least one .bad and one .good" in r.stdout


def test_run_all_sh_is_red_when_a_bad_fixture_stays_green(copy):
    (copy / "fixtures/push-gate.sabotage.bad").write_text("echo nothing to see here\n")
    r = run(copy / "run-all.sh", copy)
    assert r.returncode == 1
    assert "push-gate.sabotage.bad" in r.stdout and "expected 1" in r.stdout


def test_sabotage_probe_sh_is_red_for_a_guard_whose_logic_is_untested(copy):
    (copy / "fake-guard.sh").write_text(FAKE_GUARD)
    (copy / "fixtures/fake-guard.bad").write_text("x\n")
    (copy / "fixtures/fake-guard.good").write_text("x\n")
    assert run(copy / "run-all.sh", copy).returncode == 0  # the fixtures alone look fine
    r = run(copy / "sabotage-probe.sh", copy)
    assert r.returncode == 1
    assert "FAIL  fake-guard: commenting out its logic changed nothing" in r.stdout
