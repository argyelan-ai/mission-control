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
