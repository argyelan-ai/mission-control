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
# a sabotage test that really runs the gate (R10 reads the probe column)
PROBE_TEST = "def test_red():\n    assert run('real-gate.sh', bad=True) == 2\n"
PROBE_CELL = "`tools/hooks/tests/test_real_gate.py` (bad input -> exit 2)"
WORKFLOW = "name: ci\non: [push]\njobs:\n  tests:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pytest\n"


def row(gid, kind="Gate", what="does x", blocks="test red", probe="run 123", req="ja"):
    return f"| `{gid}` | {kind} | {what} | {blocks} | {probe} | {req} |\n"


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "tools/hooks").mkdir(parents=True)
    (tmp_path / "tools/guards").mkdir(parents=True)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "tools/hooks/real-gate.sh").write_text(REAL_GATE)
    (tmp_path / "tools/hooks/tests").mkdir()
    (tmp_path / "tools/hooks/tests/test_real_gate.py").write_text(PROBE_TEST)
    (tmp_path / ".github/workflows/ci.yml").write_text(WORKFLOW)
    rows = row("ci:ci.yml#tests") + row("script:tools/hooks/real-gate.sh", probe=PROBE_CELL)
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
    (tree / "tools/hooks/tests/test_check.py").write_text("import check\n")
    add_rows(tree, row("script:tools/guards/check.py", probe="`tools/hooks/tests/test_check.py`"))
    assert any(x.startswith("R5") for x in findings(tree))
    (tree / "tools/guards/check.py").write_text("import sys\nsys.exit(1 if bad() else 0)\n")
    assert findings(tree) == []


def test_guard_whose_red_path_is_in_a_sourced_sibling_lib(tree):
    guard = '#!/bin/bash\n. "$(cd "$(dirname "$0")" && pwd)/lib.sh"\ncheck_line() { :; }\nlint_main "$@"\n'
    (tree / "tools/guards/lint-x.sh").write_text(guard)
    add_rows(tree, row("script:tools/guards/lint-x.sh", probe=PROBE_CELL), row("script:tools/guards/lib.sh", kind="Helfer"))
    (tree / "tools/hooks/tests/test_real_gate.py").write_text(PROBE_TEST + "# lint-x.sh\n")
    # lib missing -> nothing can exit non-zero
    assert any(x.startswith("R4") and "lib.sh" in x for x in findings(tree))
    assert any(x.startswith("R5") and "lint-x.sh" in x for x in findings(tree))
    # lib that can only succeed -> still red
    (tree / "tools/guards/lib.sh").write_text("lint_main() { exit 0; }\n")
    assert any(x.startswith("R5") and "lint-x.sh" in x for x in findings(tree))
    # lib with a red path -> green
    (tree / "tools/guards/lib.sh").write_text("lint_main() { [ -n \"$1\" ] || exit 1; exit 0; }\n")
    assert findings(tree) == []


def test_escaped_pipe_stays_in_its_cell_and_extra_columns_are_red(tree):
    # `\|` is a pipe inside a cell: the row keeps 6 columns and stays green
    add_rows(tree, row("script:tools/hooks/piped-gate.sh", blocks=r"gate piped into `\| tail`", probe=PROBE_CELL))
    (tree / "tools/hooks/tests/test_real_gate.py").write_text(PROBE_TEST + "# piped-gate.sh\n")
    (tree / "tools/hooks/piped-gate.sh").write_text(REAL_GATE)
    assert findings(tree) == []
    # an unescaped pipe shifts every later column - that must not pass silently
    add_rows(tree, row("script:tools/hooks/other-gate.sh", blocks="gate piped into `| tail`", probe=PROBE_CELL))
    (tree / "tools/hooks/other-gate.sh").write_text(REAL_GATE)
    assert any("expected 6 columns, got 7" in x for x in findings(tree))


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


# --- Kritik 19.10.: form vs. content ----------------------------------------

WF4 = ("name: ci4\non: [push]\njobs:\n    lint:\n        name: Lint\n"
       "        runs-on: x\n        steps:\n            - run: ruff .\n")


def test_four_space_indented_workflow_jobs_are_read(tree):
    (tree / ".github/workflows/ci4.yml").write_text(WF4)
    assert any(x.startswith("R1") and "ci:ci4.yml#lint" in x for x in findings(tree))
    add_rows(tree, row("ci:ci4.yml#lint"))
    assert findings(tree) == []


def test_four_space_continue_on_error_job_as_gate_is_red(tree):
    (tree / ".github/workflows/ci4.yml").write_text(WF4.replace(
        "        runs-on:", "        # gate-audit: soft - hint\n        continue-on-error: true\n        runs-on:"))
    add_rows(tree, row("ci:ci4.yml#lint"))
    assert any(x.startswith("R7") for x in findings(tree))


@pytest.mark.parametrize("cond", ["false", "${{ false }}", "'false'"])
def test_disabled_job_registered_as_gate_is_red(tree, cond):
    wf = tree / ".github/workflows/ci.yml"
    wf.write_text(WORKFLOW.replace("    runs-on:", f"    if: {cond}\n    runs-on:"))
    assert any(x.startswith("R5") and "if:" in x for x in findings(tree))


def test_ci_gate_that_is_not_required_must_say_so(tree):
    p = tree / "tools/guards/GATES.md"
    base = p.read_text()
    p.write_text(base.replace("| `ci:ci.yml#tests` | Gate | does x | test red | run 123 | ja |",
                              "| `ci:ci.yml#tests` | Gate | does x | test red | run 123 | nein |"))
    assert any(x.startswith("R8") for x in findings(tree))
    p.write_text(base.replace("| `ci:ci.yml#tests` | Gate | does x | test red | run 123 | ja |",
                              "| `ci:ci.yml#tests` | Gate (nicht Pflicht) | does x | test red | run 123 | nein |"))
    assert findings(tree) == []
    p.write_text(base.replace("| `ci:ci.yml#tests` | Gate | does x | test red | run 123 | ja |",
                              "| `ci:ci.yml#tests` | Gate (nicht Pflicht) | does x | test red | run 123 | ja |"))
    assert any(x.startswith("R8") for x in findings(tree))


def _rows(tree, probe):
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace("| test red | run 123 | ja |\n", f"| test red | {probe} | ja |\n", 1))
    (tree / ".github/workflows/ci.yml").write_text(WORKFLOW.replace("    runs-on:", "    name: Tests\n    runs-on:"))


def fake_fetch(runs, jobs):
    def fetch(path):
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": jobs.get(int(path.split("/")[-2].split("?")[0]), [])}
        rid = int(path.rsplit("/", 1)[1])
        if rid not in runs:
            raise RuntimeError("HTTP 404")
        return runs[rid]
    return fetch


def test_red_run_evidence_must_be_a_real_failed_run_of_that_job(tree):
    _rows(tree, "Rot-Lauf 5550001")
    ok = fake_fetch({5550001: {"conclusion": "failure", "path": ".github/workflows/ci.yml"}},
                    {5550001: [{"name": "Tests", "conclusion": "failure"}]})
    assert gate_audit.verify_runs(tree, ok) == []
    green = fake_fetch({5550001: {"conclusion": "success", "path": ".github/workflows/ci.yml"}}, {})
    assert any(x.startswith("R9") and "success" in x for x in gate_audit.verify_runs(tree, green))
    other_wf = fake_fetch({5550001: {"conclusion": "failure", "path": ".github/workflows/x.yml"}}, {})
    assert any(x.startswith("R9") and "x.yml" in x for x in gate_audit.verify_runs(tree, other_wf))
    job_green = fake_fetch({5550001: {"conclusion": "failure", "path": ".github/workflows/ci.yml"}},
                           {5550001: [{"name": "Tests", "conclusion": "success"}]})
    assert any(x.startswith("R9") and "Tests" in x for x in gate_audit.verify_runs(tree, job_green))
    missing = fake_fetch({}, {})
    assert any(x.startswith("R9") and "404" in x for x in gate_audit.verify_runs(tree, missing))


# --- R10: the probe must be a test file that really exercises the script -----


def test_probe_that_is_only_words_is_red(tree):
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace(PROBE_CELL, "tested by hand, trust me"))
    assert any(x.startswith("R10") and "names no test file" in x for x in findings(tree))


def test_probe_file_that_does_not_exist_is_red(tree):
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace("test_real_gate.py", "test_gone.py"))
    assert any(x.startswith("R10") and "do not exist" in x for x in findings(tree))


def test_probe_file_that_never_runs_the_script_is_red(tree):
    (tree / "tools/hooks/tests/test_real_gate.py").write_text("def test_nothing():\n    assert True\n")
    assert any(x.startswith("R10") and "never mention" in x for x in findings(tree))


def test_python_hook_scripts_must_be_registered_too(tree):
    (tree / "tools/hooks/new-check.py").write_text("import sys\nsys.exit(2)\n")
    assert any(x.startswith("R2") and "new-check.py" in x for x in findings(tree))


# --- R10 for guards in tools/guards: their fixtures are the sabotage test ----

RUN_ALL = '#!/bin/bash\nNOT_GUARDS=" run-all.sh lib.sh "\nfor g in *.sh; do bash "$g" fixtures/x.bad || exit 1; done\n'
FIXTURE_CELL = "`tools/guards/fixtures/lint-y*.bad` über run-all.sh"


def _guard_tree(tree, fixtures=("lint-y.bad", "lint-y.good")):
    (tree / "tools/guards/fixtures").mkdir(exist_ok=True)
    (tree / "tools/guards/run-all.sh").write_text(RUN_ALL)
    (tree / "tools/guards/lint-y.sh").write_text(REAL_GATE)
    for f in fixtures:
        (tree / "tools/guards/fixtures" / f).write_text("x\n")
    add_rows(tree, row("script:tools/guards/run-all.sh", probe=PROBE_CELL.replace("real_gate", "real_gate")),
             row("script:tools/guards/lint-y.sh", probe=FIXTURE_CELL))
    (tree / "tools/hooks/tests/test_real_gate.py").write_text(PROBE_TEST + "# run-all.sh\n")


def test_guard_with_its_own_bad_fixtures_satisfies_r10(tree):
    _guard_tree(tree)
    assert findings(tree) == []


def test_guard_fixture_probe_without_a_bad_fixture_is_red(tree):
    _guard_tree(tree, fixtures=("lint-y.good",))
    assert any(x.startswith("R10") and "no fixtures/lint-y.bad" in x for x in findings(tree))


def test_guard_fixture_probe_for_a_script_run_all_skips_is_red(tree):
    _guard_tree(tree)
    (tree / "tools/guards/run-all.sh").write_text(RUN_ALL.replace(" lib.sh ", " lib.sh lint-y.sh "))
    assert any(x.startswith("R10") and "skips lint-y.sh" in x for x in findings(tree))


def test_fixture_of_another_guard_does_not_count(tree):
    _guard_tree(tree)
    p = tree / "tools/guards/GATES.md"
    p.write_text(p.read_text().replace(FIXTURE_CELL, "`tools/guards/fixtures/other*.bad`"))
    (tree / "tools/guards/fixtures/other.bad").write_text("x\n")
    assert any(x.startswith("R10") and "lint-y.sh" in x for x in findings(tree))
