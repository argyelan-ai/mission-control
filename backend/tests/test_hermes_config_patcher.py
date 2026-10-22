"""Unit tests for hermes-config-patch.py — idempotency + correct keys.

Tests run against a temp config copy, never touch real ~/.hermes/config.yaml.
Phase 25, Plan 25-07 T3.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml as pyyaml

REPO = Path(__file__).resolve().parents[2]
PATCHER = REPO / "scripts" / "hermes-config-patch.py"


@pytest.fixture
def fake_config(tmp_path):
    """Create a minimal valid hermes config in tmp."""
    cfg = {
        "terminal": {"env_passthrough": [], "backend": "local"},
        "approvals": {"mode": "manual", "timeout": 60},
        "security": {"allow_private_urls": False},
        "mcp_servers": {"mc": {"command": "python3", "args": ["wrong", "path"]}},
    }
    p = tmp_path / "config.yaml"
    p.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    return p


def _load_module():
    spec = importlib.util.spec_from_file_location("hermes_config_patch", PATCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_run_applies_all_patches(fake_config, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    rc = mod.main()
    assert rc == 0
    result = pyyaml.safe_load(fake_config.read_text())
    assert result["security"]["allow_private_urls"] is True
    assert "MC_BASE_URL" in result["terminal"]["env_passthrough"]
    assert "MC_AGENT_TOKEN" in result["terminal"]["env_passthrough"]
    assert result["approvals"]["timeout"] == 0
    assert result["mcp_servers"]["mc"]["command"].endswith("python3")
    assert len(result["mcp_servers"]["mc"]["args"]) == 1


def test_second_run_is_idempotent(fake_config, monkeypatch, capsys):
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    mod.main()  # first run
    capsys.readouterr()  # drain
    before = fake_config.read_text()
    rc = mod.main()  # second run
    after = fake_config.read_text()
    assert rc == 0
    assert "idempotent" in capsys.readouterr().out.lower()
    assert before == after


def test_does_not_clobber_unrelated_keys(fake_config, monkeypatch):
    """Sibling keys outside our patch list must not be lost."""
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["terminal"]["custom_user_key"] = "preserve_me"
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    mod.main()
    result = pyyaml.safe_load(fake_config.read_text())
    assert result["terminal"]["custom_user_key"] == "preserve_me"


def test_missing_config_returns_2(tmp_path, monkeypatch):
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", tmp_path / "nonexistent.yaml")
    monkeypatch.setattr(mod, "BACKUP_PATH", tmp_path / "nonexistent.yaml.bak")
    rc = mod.main()
    assert rc == 2


def test_model_block_set_from_openai_env(fake_config, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://192.0.2.10:8000/v1")
    monkeypatch.setenv("OPENAI_MODEL", "nvidia/Qwen3.6-35B")
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    assert mod.main() == 0
    result = pyyaml.safe_load(fake_config.read_text())
    assert result["model"]["provider"] == "custom"
    assert result["model"]["base_url"] == "http://192.0.2.10:8000/v1"
    assert result["model"]["default"] == "nvidia/Qwen3.6-35B"


def test_model_block_untouched_when_env_missing(fake_config, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["model"] = {"provider": "ollama-cloud", "base_url": "https://ollama.com/v1", "default": "kimi-k2.6"}
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    mod.main()
    result = pyyaml.safe_load(fake_config.read_text())
    assert result["model"]["provider"] == "ollama-cloud"  # guard held


def test_mc_mcp_server_gets_agent_env_file(fake_config, monkeypatch):
    """The mc MCP server must receive MC_AGENT_ENV_FILE via its per-server env
    block: hermes-agent spawns MCP servers with a SANITIZED env (_build_safe_env),
    so neither the TUI's nor the gateway's process env reaches mc-mcp.py. Live
    incident 2026-07-12: agent-scoped calls (comments/checklist/finish) failed
    for days — mc-mcp.py had no MC_AGENT_TOKEN and no file fallback pointer."""
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".bak"))
    assert mod.main() == 0
    cfg = pyyaml.safe_load(fake_config.read_text())
    env = cfg["mcp_servers"]["mc"].get("env") or {}
    assert env.get("MC_AGENT_ENV_FILE", "").endswith("/.mc/agents/hermes/agent.env")


# ── Runtime window -> model.context_length (22.10.2026) ────────────────────
# Hermes guesses its context window from its own model-name table unless
# model.context_length says otherwise. With a stale model name it guessed
# GLM's 202,752 while the runtime served 1,048,576 — wrong chat ring AND
# compaction thresholds. MC now sends MC_CONTEXT_WINDOW in agent.env.


def _patch_with(monkeypatch, fake_config, **env):
    for key in ("OPENAI_BASE_URL", "OPENAI_MODEL", "MC_CONTEXT_WINDOW"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    return mod


def test_context_length_written_from_runtime_window(fake_config, monkeypatch):
    mod = _patch_with(
        monkeypatch,
        fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1",
        OPENAI_MODEL="DeepSeek-V4.1-Flash-1017",
        MC_CONTEXT_WINDOW="1048576",
    )
    assert mod.main() == 0
    model = pyyaml.safe_load(fake_config.read_text())["model"]
    assert model["default"] == "DeepSeek-V4.1-Flash-1017"
    assert model["base_url"] == "http://192.0.2.10:8000/v1"
    # An INT: Hermes rejects anything else ("must be a plain integer").
    assert model["context_length"] == 1048576


def test_context_length_is_idempotent(fake_config, monkeypatch, capsys):
    mod = _patch_with(
        monkeypatch,
        fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1",
        OPENAI_MODEL="m",
        MC_CONTEXT_WINDOW="1048576",
    )
    mod.main()
    before = fake_config.read_text()
    capsys.readouterr()
    assert mod.main() == 0
    assert fake_config.read_text() == before
    assert "idempotent" in capsys.readouterr().out.lower()


def test_stale_glm_block_is_corrected_and_unrelated_keys_survive(fake_config, monkeypatch):
    """The live state of 22.10.2026: config still on GLM, plus Hermes' own keys."""
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["model"] = {
        "provider": "custom",
        "base_url": "http://192.0.2.10:8000/v1",
        "default": "GLM-5.3-Flash-EXL3",
        "max_tokens": 8192,
    }
    cfg["display"] = {"skin": "default"}
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    mod = _patch_with(
        monkeypatch,
        fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1",
        OPENAI_MODEL="DeepSeek-V4.1-Flash-1017",
        MC_CONTEXT_WINDOW="1048576",
    )
    assert mod.main() == 0
    result = pyyaml.safe_load(fake_config.read_text())
    assert result["model"]["default"] == "DeepSeek-V4.1-Flash-1017"
    assert result["model"]["context_length"] == 1048576
    assert result["model"]["max_tokens"] == 8192
    assert result["display"] == {"skin": "default"}


def test_window_change_on_same_model_is_applied(fake_config, monkeypatch):
    """Recipe switch that keeps the model name but changes the window
    (GLM vLLM 250k -> GLM TensorFold 1M, 01.10.2026)."""
    common = dict(OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="GLM")
    _patch_with(monkeypatch, fake_config, MC_CONTEXT_WINDOW="250000", **common).main()
    _patch_with(monkeypatch, fake_config, MC_CONTEXT_WINDOW="1048576", **common).main()
    assert pyyaml.safe_load(fake_config.read_text())["model"]["context_length"] == 1048576


def test_old_window_is_dropped_when_the_model_changes_without_one(fake_config, monkeypatch):
    """A window that belonged to the PREVIOUS model is worse than none —
    Hermes would trust it over its own detection."""
    _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="old-model",
        MC_CONTEXT_WINDOW="1048576",
    ).main()
    _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="new-model",
    ).main()
    model = pyyaml.safe_load(fake_config.read_text())["model"]
    assert model["default"] == "new-model"
    assert "context_length" not in model


def test_hand_set_window_survives_when_mc_sends_none_and_model_is_unchanged(
    fake_config, monkeypatch
):
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["model"] = {"provider": "custom", "base_url": "http://192.0.2.10:8000/v1",
                    "default": "same", "context_length": 65536}
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="same",
    ).main()
    assert pyyaml.safe_load(fake_config.read_text())["model"]["context_length"] == 65536


def test_garbage_window_is_ignored_loudly(fake_config, monkeypatch, capsys):
    mod = _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="m",
        MC_CONTEXT_WINDOW="1M",
    )
    assert mod.main() == 0
    assert "context_length" not in pyyaml.safe_load(fake_config.read_text())["model"]
    assert "MC_CONTEXT_WINDOW" in capsys.readouterr().err


def test_window_without_runtime_binding_touches_nothing(fake_config, monkeypatch):
    """No OPENAI_* = no MC binding: a hand-written (cloud) model block stays."""
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["model"] = {"provider": "ollama-cloud", "default": "kimi-k2.6"}
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    _patch_with(monkeypatch, fake_config, MC_CONTEXT_WINDOW="1048576").main()
    assert "context_length" not in pyyaml.safe_load(fake_config.read_text())["model"]


def test_write_keeps_the_config_private(fake_config, monkeypatch):
    """config.yaml holds provider keys: the atomic replace must not widen it
    to the umask's 0644."""
    import os
    import stat

    os.chmod(fake_config, 0o600)
    mod = _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="m",
        MC_CONTEXT_WINDOW="1048576",
    )
    old_umask = os.umask(0o022)
    try:
        assert mod.main() == 0
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(fake_config.stat().st_mode) == 0o600
    assert not list(fake_config.parent.glob("*.tmp-25-07"))


def test_hermes_home_is_respected(tmp_path, monkeypatch):
    """Hermes reads $HERMES_HOME/config.yaml when set — so must the patcher."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    mod = _load_module()
    assert mod.CONFIG_PATH == tmp_path / "hh" / "config.yaml"


# ── MCP path follows the patcher's own install (review #939, MUST 1) ───────
# Since the ACP bridge runs this patch on every chat start, a hardcoded
# ~/Workspace dev path rewrote mcp_servers.mc.args away from the deliberate
# ~/.mc/checkouts install (hand-set 11.10.2026: agent mode, no admin fallback
# from the dev tree's .env) — on every bridge start, the gateway included.


def _load_copy_from(checkout: Path):
    """Load a COPY of the patcher living in ``checkout/scripts`` — its
    ``__file__`` is what a launchd bridge under ~/.mc/checkouts sees."""
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    copy = scripts / "hermes-config-patch.py"
    copy.write_text(PATCHER.read_text())
    spec = importlib.util.spec_from_file_location("hermes_config_patch_copy", copy)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mcp_args_follow_the_patchers_own_checkout(fake_config, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MC_REPO_PATH", raising=False)
    checkout = tmp_path / "home" / ".mc" / "checkouts" / "mission-control"
    # The live state since 11.10.: args already on the checkout.
    cfg = pyyaml.safe_load(fake_config.read_text())
    cfg["mcp_servers"]["mc"]["args"] = [str(checkout / "scripts" / "mc-mcp.py")]
    fake_config.write_text(pyyaml.safe_dump(cfg, sort_keys=False))
    mod = _load_copy_from(checkout)
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    assert mod.main() == 0
    args = pyyaml.safe_load(fake_config.read_text())["mcp_servers"]["mc"]["args"]
    assert args == [str(checkout / "scripts" / "mc-mcp.py")]
    assert "Workspace/Projects" not in args[0]
    # A second start changes nothing — args is never bounced back and forth.
    capsys.readouterr()
    assert mod.main() == 0
    assert "idempotent" in capsys.readouterr().out.lower()


def test_mcp_args_honour_mc_repo_path(fake_config, tmp_path, monkeypatch):
    deliberate = tmp_path / "srv" / "deliberate-checkout"
    monkeypatch.setenv("MC_REPO_PATH", str(deliberate))
    mod = _load_module()
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    assert mod.main() == 0
    args = pyyaml.safe_load(fake_config.read_text())["mcp_servers"]["mc"]["args"]
    assert args == [str(deliberate / "scripts" / "mc-mcp.py")]


def test_mcp_command_stays_on_the_dev_venv(fake_config, tmp_path, monkeypatch):
    """The checkout has no backend venv; the interpreter stays where the
    hand-set config of 11.10. points (HOME_HOST's dev tree)."""
    monkeypatch.delenv("MC_REPO_PATH", raising=False)
    host_home = tmp_path / "host-home"
    monkeypatch.setenv("HOME_HOST", str(host_home))
    mod = _load_copy_from(tmp_path / "home" / ".mc" / "checkouts" / "mission-control")
    monkeypatch.setattr(mod, "CONFIG_PATH", fake_config)
    monkeypatch.setattr(mod, "BACKUP_PATH", fake_config.with_suffix(".yaml.bak"))
    assert mod.main() == 0
    command = pyyaml.safe_load(fake_config.read_text())["mcp_servers"]["mc"]["command"]
    assert command == str(
        host_home / "Workspace" / "Projects" / "mission-control" / "backend" / ".venv" / "bin" / "python3"
    )


# ── Rollback backup on every real change (review #939, SHOULD 1) ───────────
# The one-time .bak-pre-25-07 dates from the first patch (May 2026); with it
# as the only backup, "restore from .bak-*" would roll back six months.


def _sync_backups(config: Path) -> list[Path]:
    return sorted(config.parent.glob(config.name + ".bak-mcsync-*"))


def test_real_change_writes_a_fresh_0600_rollback_backup(fake_config, monkeypatch):
    before = fake_config.read_bytes()
    mod = _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="m", MC_CONTEXT_WINDOW="1048576",
    )
    # The old one-time backup already exists (the live situation).
    mod.BACKUP_PATH.write_text("may config")
    assert mod.main() == 0
    backups = _sync_backups(fake_config)
    assert len(backups) == 1
    assert backups[0].read_bytes() == before  # the state BEFORE this change
    assert (backups[0].stat().st_mode & 0o777) == 0o600
    # A no-op run writes no further backup.
    assert mod.main() == 0
    assert len(_sync_backups(fake_config)) == 1


def test_rollback_backups_are_pruned_to_ours_only(fake_config, monkeypatch):
    foreign = fake_config.with_suffix(".yaml.bak-20261011-mcmcp")
    foreign.write_text("hand backup")
    for i in range(7):
        fake_config.with_suffix(f".yaml.bak-mcsync-2026010{i}-000000").write_text(str(i))
    mod = _patch_with(
        monkeypatch, fake_config,
        OPENAI_BASE_URL="http://192.0.2.10:8000/v1", OPENAI_MODEL="m",
    )
    assert mod.main() == 0
    backups = _sync_backups(fake_config)
    assert len(backups) == mod.SYNC_BACKUPS_KEPT
    assert backups[-1].read_bytes() != b"6"  # the newest is this run's copy
    assert foreign.read_text() == "hand backup"  # never touched
