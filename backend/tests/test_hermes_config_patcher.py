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
