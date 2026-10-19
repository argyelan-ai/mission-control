"""Sabotage probes for tools/guards/gate_audit.py: each rule must turn the
audit red on a minimal repo, and the real repository must be green."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tools/guards"))
import gate_audit  # noqa: E402

HEADER = (
    "| ID | Art | Was | blockt bei | Sabotage-Probe / Rot-Beleg | Merge-Pflicht |\n"
    "|---|---|---|---|---|---|\n"
)
REAL_GATE = "#!/bin/bash\n# a real gate\nif grep -q bad x; then exit 2; fi\nexit 0\n"
WORKFLOW = "name: ci\non: [push]\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"


def row(gid, kind="Gate", what="does x", blocks="test red", probe="run 123", req="ja"):
    return f"| `{gid}` | {kind} | {what} | {blocks} | {probe} | {req} |\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tools/hooks").mkdir(parents=True)
    (tmp_path / "tools/guards").mkdir(parents=True)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "tools/hooks/real-gate.sh").write_text(REAL_GATE)
    (tmp_path / ".github/workflows/ci.yml").write_text(WORKFLOW)
    rows = row("ci:ci.yml#tests") + row("script:tools/hooks/real-gate.sh")
    (tmp_path / "tools/guards/GATES.md").write_text("# Gates\n\n" + HEADER + rows)
    return tmp_path


def add_rows(tree, *rows):
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text() + "".join(rows))


def findings(tree):
    f, _ = gate_audit.audit(tree)
    return f


def test_clean_tree_is_green(tree):
    assert findings(tree) == []


def test_real_repository_is_green():
    f, stats = gate_audit.audit(REPO)
    assert f == [], "\n".join(f)
    assert stats["no_red_case"] == 0 and stats["no_probe"] == 0


def test_missing_register_is_red(tree):
    (tree / "tools/guards/GATES.md").unlink()
    assert findings(tree)[0].startswith("R0")


def test_dummy_gate_with_exit_0_unregistered_is_red(tree):
    (tree / "tools/hooks/dummy-gate.sh").write_text("#!/bin/bash\n# Placeholder\nexit 0\n")
    assert any(x.startswith("R2") and "dummy-gate.sh" in x for x in findings(tree))


def test_dummy_gate_with_exit_0_registered_as_gate_is_red(tree):
    (tree / "tools/hooks/dummy-gate.sh").write_text("#!/bin/bash\n# Placeholder\nexit 0\n")
    add_rows(tree, row("script:tools/hooks/dummy-gate.sh"))
    assert any(x.startswith("R5") and "dummy-gate.sh" in x for x in findings(tree))


def test_python_guard_without_nonzero_exit_is_red(tree):
    (tree / "tools/guards/check.py").write_text("print('all good')\n")
    add_rows(tree, row("script:tools/guards/check.py"))
    assert any(x.startswith("R5") for x in findings(tree))
    (tree / "tools/guards/check.py").write_text("import sys\nsys.exit(1 if bad() else 0)\n")
    assert findings(tree) == []


def test_unregistered_workflow_job_is_red(tree):
    (tree / ".github/workflows/new.yml").write_text(
        "name: new\non: [push]\njobs:\n  lint:\n    runs-on: x\n    steps:\n      - run: ruff .\n")
    assert any(x.startswith("R1") and "ci:new.yml#lint" in x for x in findings(tree))


def test_dummy_ci_job_with_exit_0_is_red_even_when_registered(tree):
    (tree / ".github/workflows/new.yml").write_text(
        "name: new\non: [push]\njobs:\n  lint:\n    runs-on: x\n    steps:\n      - run: exit 0\n")
    add_rows(tree, row("ci:new.yml#lint"))
    assert any(x.startswith("R6") and "exit 0" in x for x in findings(tree))


@pytest.mark.parametrize("blocks,probe,frag", [
    ("", "run 1", "no red case"),
    ("—", "run 1", "no red case"),
    ("test red", "offen", "no sabotage probe"),
    ("test red", "keine", "no sabotage probe"),
    ("test red", "-", "no sabotage probe"),
])
def test_row_without_red_case_or_probe_is_red(tree, blocks, probe, frag):
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace("| test red | run 123 | ja |\n", f"| {blocks} | {probe} | ja |\n", 1))
    assert any(frag in x for x in findings(tree))


def test_continue_on_error_without_reason_is_red_and_with_reason_green(tree):
    wf = tree / ".github/workflows/ci.yml"
    wf.write_text(WORKFLOW.replace("    runs-on:", "    continue-on-error: true\n    runs-on:"))
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace("`ci:ci.yml#tests` | Gate", "`ci:ci.yml#tests` | Hinweis"))
    assert any(x.startswith("R6") for x in findings(tree))
    wf.write_text(WORKFLOW.replace(
        "    runs-on:", "    # gate-audit: soft - only a hint\n    # more words\n    continue-on-error: true\n    runs-on:"))
    assert findings(tree) == []


def test_continue_on_error_job_registered_as_gate_is_red(tree):
    wf = tree / ".github/workflows/ci.yml"
    wf.write_text(WORKFLOW.replace("    runs-on:", "    # gate-audit: soft - hint\n    continue-on-error: true\n    runs-on:"))
    assert any(x.startswith("R7") for x in findings(tree))


def test_or_true_without_reason_is_red(tree):
    wf = tree / ".github/workflows/ci.yml"
    wf.write_text(WORKFLOW.replace("- run: pytest", "- run: pytest || true"))
    assert any(x.startswith("R6") and "|| true" in x for x in findings(tree))


def test_stale_rows_are_red(tree):
    add_rows(tree, row("ci:ci.yml#gone"), row("script:tools/hooks/gone.sh"), row("test:backend/tests/test_gone.py"))
    f = findings(tree)
    assert sum(1 for x in f if x.startswith("R4")) == 3


def test_helper_rows_need_no_red_case_but_must_say_what(tree):
    (tree / "tools/hooks/notify.sh").write_text("#!/bin/bash\nexit 0\n")
    add_rows(tree, row("script:tools/hooks/notify.sh", kind="Helfer", blocks="—", probe="—", req="—"))
    assert findings(tree) == []
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace("| Helfer | does x |", "| Helfer | — |"))
    assert any("Helfer row must say" in x for x in findings(tree))


def test_cli_exit_codes(tree):
    script = REPO / "tools/guards/gate_audit.py"
    ok = subprocess.run([sys.executable, str(script), "--root", str(tree)], capture_output=True, text=True)
    assert ok.returncode == 0 and "gate-audit: ok" in ok.stdout
    (tree / "tools/hooks/dummy-gate.sh").write_text("#!/bin/bash\nexit 0\n")
    bad = subprocess.run([sys.executable, str(script), "--root", str(tree)], capture_output=True, text=True)
    assert bad.returncode == 1 and "gate-audit: FAIL" in bad.stdout
