#!/usr/bin/env python3
"""One selector rule for every way an omp agent starts a model.

Before this file there were four copies of "which model does this agent
run": ``acp_model.selector_from`` (chat daemon), ``bridge._acp_model_selector``
+ ``_default_model_selector`` (task bridge), ``acp_chat.model_selector`` and
the shell line in ``launch-omp.sh`` (native TUI). They had drifted:

* an ``OPENAI_MODEL`` that is already provider-qualified (``mc-openai/X``)
  became ``mc-openai/mc-openai/X`` in the chat daemon, but stayed ``mc-openai/X``
  in the task bridge;
* ``launch-omp.sh`` ignored the ``OMP_ACP_MODEL`` override the other two
  honour.

``acp_model`` is now the only rule. The same table is run through every
entry point — Python (chat + task) and the shell launcher — so they cannot
drift again. Model names are neutral on purpose.

  pytest docker/omp-bridge/tests/test_model_selector_single_source.py -v
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import acp_chat  # noqa: E402
import acp_model  # noqa: E402
import bridge  # noqa: E402

# (env, expected selector) — None = no model configured.
TABLE = [
    ({"OMP_ACP_MODEL": "x/pinned", "OMP_MODEL_SELECTOR": "mc-openai/A", "OPENAI_MODEL": "B"}, "x/pinned"),
    ({"OMP_MODEL_SELECTOR": "mc-openai/A", "OPENAI_MODEL": "B"}, "mc-openai/A"),
    ({"OPENAI_MODEL": "B"}, "mc-openai/B"),
    # A bare model id that merely contains a slash (vendor/model) is NOT a
    # provider: it still goes under mc-openai.
    ({"OPENAI_MODEL": "vendor/Model-B"}, "mc-openai/vendor/Model-B"),
    # Already provider-qualified values pass through unchanged.
    ({"OPENAI_MODEL": "mc-openai/B"}, "mc-openai/B"),
    ({"OPENAI_MODEL": "lm-studio/B"}, "lm-studio/B"),
    ({"OPENAI_MODEL": "openai/B"}, "openai/B"),
    # Blank values do not count.
    ({"OMP_ACP_MODEL": "  ", "OMP_MODEL_SELECTOR": "", "OPENAI_MODEL": "B"}, "mc-openai/B"),
    ({}, None),
]


@pytest.mark.parametrize("env,expected", TABLE)
def test_acp_model_is_the_rule(env, expected):
    assert acp_model.selector_from(env) == expected


@pytest.mark.parametrize("env,expected", TABLE)
def test_chat_daemon_uses_the_same_rule(env, expected):
    assert acp_chat.model_selector(env) == expected


@pytest.mark.parametrize("env,expected", [row for row in TABLE if row[1] is not None])
def test_task_bridge_uses_the_same_rule(env, expected):
    assert bridge._acp_model_selector(env) == expected


def test_required_selector_fails_loud(tmp_path):
    """No model = boot error, never omp's built-in default (ADR-054)."""
    env = {"OMP_HOME": str(tmp_path)}
    assert acp_model.current_selector(env) is None
    with pytest.raises(RuntimeError, match="OPENAI_MODEL"):
        acp_model.current_selector(env, required=True)
    with pytest.raises(RuntimeError, match="OPENAI_MODEL"):
        bridge._acp_model_selector({})


def test_current_env_overlays_the_rendered_file(tmp_path):
    env_file = tmp_path / "omp.env"
    env_file.write_text("OMP_MODEL_SELECTOR=mc-openai/New\nOPENAI_MODEL=New\n", encoding="utf-8")
    base = {"OMP_ENV_FILE": str(env_file), "OMP_MODEL_SELECTOR": "mc-openai/Old",
            "OMP_ACP_MODEL": "x/override"}
    merged = acp_model.current_env(base)
    assert merged["OMP_MODEL_SELECTOR"] == "mc-openai/New"
    assert merged["OMP_ACP_MODEL"] == "x/override"
    assert acp_model.current_selector(base, required=True) == "x/override"


def test_bridge_has_no_second_copy_of_the_rule():
    """Tripwire: the serve loop resolves through acp_model, and the bridge's
    old helpers are thin aliases (no own precedence chain)."""
    src = (ROOT / "bridge.py").read_text(encoding="utf-8")
    assert "current_selector(required=True)" in src
    assert 'env.get("OMP_ACP_MODEL")' not in src
    assert 'f"mc-openai/{m}"' not in src


# ── the shell launcher (native TUI) ─────────────────────────────────────────


def _run_launcher(tmp_path: Path, env: dict) -> str:
    """Run launch-omp.sh with a fake ``omp`` on PATH that prints its args."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "omp"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
    fake.chmod(0o755)
    run_env = {
        "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "OMP_ENV_FILE": str(tmp_path / "missing-omp.env"),
        "OMP_DEFAULT_CWD": str(tmp_path),
        **env,
    }
    proc = subprocess.run(
        ["bash", str(ROOT / "launch-omp.sh")],
        env=run_env, capture_output=True, text=True, timeout=20,
    )
    if proc.returncode != 0:
        return f"<exit {proc.returncode}>"
    args = proc.stdout.splitlines()
    return args[args.index("--model") + 1]


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("env,expected", TABLE)
def test_shell_launcher_uses_the_same_rule(tmp_path, env, expected):
    got = _run_launcher(tmp_path, env)
    if expected is None:
        assert got.startswith("<exit"), got  # fails loud, never a default model
    else:
        assert got == expected


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_shell_launcher_rendered_file_wins_over_boot_env(tmp_path):
    env_file = tmp_path / "omp.env"
    env_file.write_text("OMP_MODEL_SELECTOR=mc-openai/New\n", encoding="utf-8")
    got = _run_launcher(tmp_path, {"OMP_ENV_FILE": str(env_file),
                                   "OMP_MODEL_SELECTOR": "mc-openai/Old"})
    assert got == "mc-openai/New"
