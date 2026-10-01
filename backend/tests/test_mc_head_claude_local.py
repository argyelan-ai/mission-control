"""Claude Code as a head harness on a local runtime (Anthropic-protocol
endpoint of a local engine): the model mapping in the rendered settings and
the clear failure reason when macOS blocks the local network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.heads_host_helpers import (
    fake_harness,
    make_origin,
    mark_scratch,
    run_head,
    seed_clone,
    wait_phase,
    write_spec,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="host script is POSIX-only")


@pytest.fixture
def env(tmp_path: Path):
    mc_home = tmp_path / "mc"
    mc_home.mkdir()
    origin, full_name = make_origin(tmp_path)
    seed_clone(mc_home, origin, full_name)
    mark_scratch(mc_home, full_name)
    return {"mc_home": mc_home, "tmp": tmp_path}


def _run_claude(env, body: str = "true", head_env: str = "", extra: dict | None = None, **spec):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], body)
    run_id = write_spec(mc_home, harness="claude", **spec)
    run = mc_home / "heads" / run_id
    (run / "head.env").write_text(head_env)
    env_extra = {"MC_HEAD_BIN_CLAUDE": str(harness), **(extra or {})}
    assert run_head(mc_home, "start", run_id, env_extra=env_extra).returncode == 0
    return wait_phase(mc_home, run_id, "exited"), run


def _picker(run: Path) -> dict:
    data = json.loads((run / "head-settings.json").read_text())
    return {o["model"]: o.get("behavesAs") for o in data.get("modelPicker", {}).get("options", [])}


LOCAL_ENV = (
    "ANTHROPIC_BASE_URL='http://127.0.0.1:9'\nANTHROPIC_API_KEY='placeholder'\n"
    "ANTHROPIC_MODEL='Local-Coder-9B'\nANTHROPIC_SMALL_FAST_MODEL='Local-Tiny'\n"
)


def test_local_models_are_mapped_with_behaves_as(env):
    """Claude Code warns `unrecognized_model` for a model its catalog does not
    know; a modelPicker row with behavesAs maps it (from --settings)."""
    _, run = _run_claude(env, head_env=LOCAL_ENV, model="Local-Coder-9B")
    assert _picker(run) == {"Local-Coder-9B": "claude-sonnet-4-6", "Local-Tiny": "claude-haiku-4-5"}


def test_behaves_as_targets_are_configurable(env):
    _, run = _run_claude(env, head_env=LOCAL_ENV, model="Local-Coder-9B",
                         extra={"MC_HEAD_CLAUDE_BEHAVES_AS": "claude-opus-4-8",
                                "MC_HEAD_CLAUDE_SMALL_BEHAVES_AS": "claude-sonnet-4-6"})
    assert _picker(run) == {"Local-Coder-9B": "claude-opus-4-8", "Local-Tiny": "claude-sonnet-4-6"}


def test_claude_models_get_no_mapping(env):
    head_env = "ANTHROPIC_MODEL='claude-sonnet-5'\nANTHROPIC_SMALL_FAST_MODEL='haiku'\n"
    _, run = _run_claude(env, head_env=head_env, model="claude-sonnet-5")
    assert "modelPicker" not in json.loads((run / "head-settings.json").read_text())


EHOSTUNREACH = "echo 'API Error: No internet route - check your connection or VPN (EHOSTUNREACH)'; exit 1"


def test_ehostunreach_to_a_lan_engine_names_the_local_network_permission(env):
    status, _ = _run_claude(env, EHOSTUNREACH, base_url="http://10.0.0.8:8000/v1")
    assert status["reason"] == "local_network_blocked"
    assert "claude" in status["detail"] and "Local Network" in status["detail"]


def test_omp_typo_message_to_a_lan_engine_is_the_same_cause(env):
    mc_home = env["mc_home"]
    harness = fake_harness(env["tmp"], "echo 'Was there a typo in the url or port?'; exit 1")
    run_id = write_spec(mc_home, base_url="http://10.0.0.8:8000/v1")
    assert run_head(mc_home, "start", run_id, env_extra={"MC_HEAD_BIN_OMP": str(harness)}).returncode == 0
    status = wait_phase(mc_home, run_id, "exited")
    assert status["reason"] == "local_network_blocked" and "omp" in status["detail"]


def test_ehostunreach_to_loopback_is_not_the_local_network_permission(env):
    status, _ = _run_claude(env, EHOSTUNREACH, base_url="http://127.0.0.1:9/v1")
    assert status["reason"] == "exit_1"
