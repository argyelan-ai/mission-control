"""Adapter TCK — golden-fixture conformance suite for runtime adapters.

Every CLI runtime MC drives (claude, openclaude, …) is scraped by the same
shell libs (docker/*/lib/turn-state.sh + ui-detect.sh). claude-cli 2.1 broke
every one of those heuristics at once — NBSP prompt, new spinners, collapse
chips — and cost 6 production bugs fixed live. This suite pins the scraping
layer against REAL pane snapshots so the next CLI update turns a heuristic
break into a red test instead of a broken fleet.

Onboarding a new CLI = record fixtures with tools/record-pane-fixtures.sh into
backend/tests/fixtures/panes/<cli>/ and this suite picks the directory up
automatically (parametrized over the fixture dirs). See docs/adapters.md.

What is tested per CLI directory:
  • detect_turn_state (SCRAPE MODE) classifies each state fixture correctly:
      idle.txt ⇒ idle, working.txt ⇒ working, crashed.txt ⇒ crashed.
    Scrape mode is forced (TURN_SIGNAL_MODE=scrape) because the fixtures test
    the fragile scraping layer — the Phase-A hook signal is a separate path.
  • detect_pane_ui (NO override) matches the blessed heuristic output in
    meta.json (golden regression guard), and the override path resolves the
    true runtime.
  • Both hand-maintained lib copies (mc-agent-base + mc-claude-agent) are
    byte-identical — guards against drift of the duplicated files.
"""

import json
import os
import pathlib
import shutil
import subprocess

import pytest

_HERE = pathlib.Path(__file__).parent
_ROOT = _HERE.parent.parent
_FIXTURE_ROOT = _HERE / "fixtures" / "panes"

# Behaviour is tested against the mc-agent-base copy; the byte-identity guard
# (test_lib_copies_byte_identical) proves the mc-claude-agent copy matches, so
# one behavioural run covers both.
_LIB_DIR = _ROOT / "docker" / "mc-agent-base" / "lib"
_TURN_LIB = _LIB_DIR / "turn-state.sh"
_UI_LIB = _LIB_DIR / "ui-detect.sh"

# Files that exist byte-identical in both lib copies (hand-maintained — no
# build-time sync). A drift here is exactly how a fix lands in one image and
# silently not the other.
_DUPLICATED_LIB_FILES = ["turn-state.sh", "ui-detect.sh", "paste-verify.sh", "mc-pre-push.sh"]

# Known scraping misclassifications: (cli, state) -> reason. These are real
# heuristic gaps the TCK surfaces but does NOT paper over — the test asserts the
# CORRECT expectation and is marked xfail(strict), so the day turn-state.sh is
# fixed the xfail flips to XPASS and forces removal of this entry.
#
# claude/working: claude-cli 2.1.x renders its input box with a bare `❯` at all
# times, INCLUDING mid-turn. detect_turn_state's idle check (`tail -5 | ^❯ *$`)
# runs before the working check, so an active turn scrapes as idle. In
# production this is masked by the Phase-A hook signal (submit ⇒ working); it
# only bites in signal-less scrape mode. Fix belongs in turn-state.sh (out of
# scope for the TCK phase — do NOT edit the lib here).
_KNOWN_SCRAPE_BUGS = {
    ("claude", "working"): (
        "claude-cli 2.1.x renders a bare `❯` input line mid-turn; the idle "
        "check beats the working check in scrape mode. Masked in prod by the "
        "Phase-A hook signal. Fix in turn-state.sh."
    ),
}


def _cli_dirs():
    if not _FIXTURE_ROOT.is_dir():
        return []
    return sorted(p for p in _FIXTURE_ROOT.iterdir() if p.is_dir())


def _load_meta(cli_dir):
    meta_path = cli_dir / "meta.json"
    if not meta_path.is_file():
        return {}
    return json.loads(meta_path.read_text())


def _turn_state_cases():
    """(cli_dir, state) for every <state>.txt present under each CLI dir."""
    cases = []
    for cli_dir in _cli_dirs():
        for state in ("idle", "working", "crashed"):
            if (cli_dir / f"{state}.txt").is_file():
                cases.append((cli_dir, state))
    return cases


def _run_lib_fn(lib_path, fn, pane_file, extra_env=None):
    """Source `lib_path`, stub tmux to emit `pane_file`, run `fn`, return stdout.

    Mirrors the tmux-stub pattern the existing shell smoke-tests use, so the
    REAL lib functions run against the fixture exactly as they do in poll.sh.
    """
    stub_dir = pane_file.parent / f".tmuxstub_{os.getpid()}"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "tmux"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "capture-pane" ]; then\n'
        '  cat "$TMUX_STUB_PANE_FILE"\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["TMUX_STUB_PANE_FILE"] = str(pane_file)
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["bash", "-c", f'source "{lib_path}"; {fn}'],
            capture_output=True, text=True, timeout=30, env=env,
        )
    finally:
        shutil.rmtree(stub_dir, ignore_errors=True)
    assert result.returncode == 0 or fn.startswith("detect_pane_ui"), (
        f"lib fn '{fn}' errored: {result.stderr}"
    )
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "cli_dir,state",
    _turn_state_cases(),
    ids=[f"{d.name}-{s}" for d, s in _turn_state_cases()],
)
def test_turn_state_scrape(cli_dir, state, request):
    """Scrape-mode detect_turn_state classifies each golden pane as its state."""
    known = _KNOWN_SCRAPE_BUGS.get((cli_dir.name, state))
    if known:
        request.node.add_marker(pytest.mark.xfail(reason=known, strict=True))
    out = _run_lib_fn(
        _TURN_LIB, "detect_turn_state s",
        cli_dir / f"{state}.txt",
        extra_env={"TURN_SIGNAL_MODE": "scrape"},
    )
    assert out == state, (
        f"{cli_dir.name}/{state}.txt scraped as '{out}', expected '{state}'"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "cli_dir", _cli_dirs(), ids=[d.name for d in _cli_dirs()],
)
def test_ui_detect_heuristic(cli_dir):
    """detect_pane_ui (no override) matches the blessed heuristic output.

    Golden regression guard: a future ui-detect.sh change that flips the
    classification of a real pane turns this red so a human blesses the new
    meta.json value. For claude-cli 2.1.x the blessed value is intentionally
    'openclaude' — the box-glyph-less pane is genuinely ambiguous, which is
    exactly why the image bakes PANE_UI_OVERRIDE (see test below).
    """
    meta = _load_meta(cli_dir)
    expected_ui = meta.get("expected_ui")
    if not expected_ui:
        pytest.skip(f"{cli_dir.name}: no blessed expected_ui in meta.json")
    idle_fixture = cli_dir / "idle.txt"
    if not idle_fixture.is_file():
        pytest.skip(f"{cli_dir.name}: no idle.txt to classify")
    out = _run_lib_fn(
        _UI_LIB, "detect_pane_ui s:0", idle_fixture,
        extra_env={"PANE_UI_OVERRIDE": ""},
    )
    assert out == expected_ui, (
        f"{cli_dir.name}: heuristic returned '{out}', blessed '{expected_ui}'"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
@pytest.mark.parametrize(
    "cli_dir", _cli_dirs(), ids=[d.name for d in _cli_dirs()],
)
def test_ui_detect_override_resolves_true_runtime(cli_dir):
    """PANE_UI_OVERRIDE (the production path) resolves the true runtime.

    The heuristic can be ambiguous (claude 2.1.x looks like openclaude), so the
    image bakes PANE_UI_OVERRIDE. This proves that mechanism identifies the CLI
    correctly regardless of pane content.
    """
    meta = _load_meta(cli_dir)
    true_runtime = meta.get("true_runtime")
    if not true_runtime:
        pytest.skip(f"{cli_dir.name}: no true_runtime in meta.json")
    idle_fixture = cli_dir / "idle.txt"
    if not idle_fixture.is_file():
        pytest.skip(f"{cli_dir.name}: no idle.txt to classify")
    out = _run_lib_fn(
        _UI_LIB, "detect_pane_ui s:0", idle_fixture,
        extra_env={"PANE_UI_OVERRIDE": true_runtime},
    )
    assert out == true_runtime, (
        f"{cli_dir.name}: override gave '{out}', expected '{true_runtime}'"
    )


@pytest.mark.parametrize("filename", _DUPLICATED_LIB_FILES)
def test_lib_copies_byte_identical(filename):
    """The hand-maintained lib copies must never drift.

    build-agent-images.sh syncs only shared/poll.sh, NOT lib/ — these files are
    hand-copied. A fix applied to one image and not the other is a silent fleet
    split-brain. Byte-compare both copies.
    """
    base = _ROOT / "docker" / "mc-agent-base" / "lib" / filename
    claude = _ROOT / "docker" / "mc-claude-agent" / "lib" / filename
    assert base.is_file(), f"missing {base}"
    assert claude.is_file(), f"missing {claude}"
    assert base.read_bytes() == claude.read_bytes(), (
        f"{filename} differs between mc-agent-base and mc-claude-agent lib copies"
    )
