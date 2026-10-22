"""The ACP driver syncs ~/.hermes/config.yaml before Hermes starts.

Live 22.10.2026: the runtime had served DeepSeek for a day, but Hermes'
config.yaml still said GLM — so Hermes guessed GLM's 202,752-token window and
its chat ring read 9 % instead of 2 %. The config patcher existed, but only
docker/hermes/entrypoint.sh ran it, and under HERMES_DRIVER=acp the bridge
returns ``chat_daemon().start()`` BEFORE ever reaching the entrypoint.

These tests run the REAL patcher (scripts/hermes-config-patch.py) against a
config in tmp_path (HERMES_HOME + HOME_DIR redirected) — never the real one.
No `hermes` child is spawned: the ACP session is a fake.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest
import yaml as pyyaml

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPO_ROOT / "scripts" / "hermes-bridge.py"

WINDOW = 1_048_576
MODEL = "DeepSeek-V4.1-Flash-1017"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("hermes_bridge_cfg", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class _FakeSession:
    def __init__(self, order: list[str]):
        self.order = order

    def start(self):
        self.order.append("session.start")

    def close(self):
        self.order.append("session.close")

    def state(self):
        return {"busy": False}


@pytest.fixture
def hermes_home(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    cfg = {
        "model": {
            "provider": "custom",
            "base_url": "http://192.0.2.10:8000/v1",
            "default": "GLM-5.3-Flash-EXL3",
        },
        "display": {"skin": "default"},
    }
    (home / "config.yaml").write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    return home


@pytest.fixture
def bridge(monkeypatch, tmp_path, hermes_home):
    mod = _load_bridge()
    # Every host path into tmp_path — the patcher must never see the real
    # home: HOME_DIR is what load_env_from_file forces as HOME, and
    # HERMES_HOME points the patcher at the tmp config.
    monkeypatch.setattr(mod, "HOME_DIR", tmp_path)
    monkeypatch.setattr(mod, "WORKSPACE", tmp_path / "agents" / "hermes")
    monkeypatch.setattr(mod, "LAST_TASK_FILE", tmp_path / "last-task-id")
    monkeypatch.setattr(mod, "_chat_daemon_instance", None)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_DRIVER", "acp")
    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "MC_CONTEXT_WINDOW"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        "MC_AGENT_TOKEN='tok'\n"
        "OPENAI_BASE_URL='http://192.0.2.10:8000/v1'\n"
        f"OPENAI_MODEL='{MODEL}'\n"
        f"MC_CONTEXT_WINDOW='{WINDOW}'\n"
    )
    monkeypatch.setattr(mod, "ENV_FILE", env_file)
    return mod


def _fake_sessions(bridge, monkeypatch, order):
    def _build_session(**kwargs):
        order.append("build_session")
        return _FakeSession(order)

    monkeypatch.setattr(bridge.hermes_acp_chat, "build_session", _build_session)


def _config(hermes_home: Path) -> dict:
    return pyyaml.safe_load((hermes_home / "config.yaml").read_text())


def test_acp_start_patches_config_before_hermes_starts(bridge, monkeypatch, hermes_home):
    """THE fix: under the ACP driver the real patcher runs, and it runs
    BEFORE the `hermes acp` child is built (Hermes reads config.yaml once at
    agent init). Sabotage: drop the sync call from _build_chat_session and
    this goes red — config.yaml keeps GLM and no context_length."""
    order: list[str] = []
    _fake_sessions(bridge, monkeypatch, order)
    real_sync = bridge.sync_hermes_config

    def _spy(env):
        order.append("sync")
        return real_sync(env)

    monkeypatch.setattr(bridge, "sync_hermes_config", _spy)

    result = bridge.start_hermes_session()

    assert result["status"] == "started"
    assert order == ["sync", "build_session", "session.start"]
    model = _config(hermes_home)["model"]
    assert model["default"] == MODEL
    assert model["context_length"] == WINDOW
    assert _config(hermes_home)["display"] == {"skin": "default"}


def test_acp_start_without_spy_really_writes_the_config(bridge, monkeypatch, hermes_home):
    """Same as above with nothing patched but the session — the bridge's own
    wiring alone must produce the right config.yaml."""
    _fake_sessions(bridge, monkeypatch, [])
    bridge.start_hermes_session()
    model = _config(hermes_home)["model"]
    assert (model["default"], model["context_length"]) == (MODEL, WINDOW)


def test_restart_picks_up_a_runtime_switch(bridge, monkeypatch, hermes_home):
    """MC rewrites agent.env on a runtime switch and reloads the bridge; a
    plain daemon /restart must sync the NEW values too."""
    order: list[str] = []
    _fake_sessions(bridge, monkeypatch, order)
    bridge.start_hermes_session()

    bridge.ENV_FILE.write_text(
        "MC_AGENT_TOKEN='tok'\n"
        "OPENAI_BASE_URL='http://192.0.2.10:8000/v1'\n"
        "OPENAI_MODEL='next-model'\n"
        "MC_CONTEXT_WINDOW='524288'\n"
    )
    bridge.chat_daemon().restart()

    model = _config(hermes_home)["model"]
    assert (model["default"], model["context_length"]) == ("next-model", 524_288)


def test_patch_failure_is_loud_but_the_chat_still_starts(
    bridge, monkeypatch, hermes_home, caplog
):
    (hermes_home / "config.yaml").unlink()  # patcher exits 2: "run hermes setup first"
    order: list[str] = []
    _fake_sessions(bridge, monkeypatch, order)

    with caplog.at_level(logging.ERROR, logger="hermes-bridge"):
        result = bridge.start_hermes_session()

    assert result["status"] == "started"
    assert "session.start" in order
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("config sync FAILED rc=2" in m for m in errors), errors


def test_patch_crash_never_raises(bridge, monkeypatch, caplog):
    def _boom(*a, **kw):
        raise OSError("no interpreter")

    monkeypatch.setattr(bridge._sp, "run", _boom)
    with caplog.at_level(logging.ERROR, logger="hermes-bridge"):
        assert bridge.sync_hermes_config({}) is False
    assert any("config sync FAILED" in r.getMessage() for r in caplog.records)


def test_native_driver_leaves_the_patch_to_the_entrypoint(bridge, monkeypatch):
    """The native tmux path already patches in entrypoint.sh (before tmux);
    the bridge must not add a second, unsandboxed run there."""
    monkeypatch.setenv("HERMES_DRIVER", "native")
    calls = []
    monkeypatch.setattr(bridge, "sync_hermes_config", lambda env: calls.append(env))
    monkeypatch.setattr(bridge, "ENTRYPOINT", Path("/nonexistent/entrypoint.sh"))
    monkeypatch.setattr(bridge, "is_session_running", lambda: False)
    monkeypatch.setattr(bridge._sp, "run", lambda *a, **kw: None)
    bridge.start_hermes_session()
    assert calls == []
    entrypoint = (REPO_ROOT / "docker" / "hermes" / "entrypoint.sh").read_text()
    assert entrypoint.index("hermes-config-patch.py") < entrypoint.index("new-session")
