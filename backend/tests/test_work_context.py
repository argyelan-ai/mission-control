"""Tests for work_context service (T-1 Phase D)."""
import os
import pytest


@pytest.mark.asyncio
async def test_detect_nextjs_project(tmp_path):
    """Detects a Next.js project from package.json."""
    from app.services.work_context import detect_project_config

    pkg = tmp_path / "package.json"
    pkg.write_text('{"dependencies": {"next": "15.0.0", "react": "18.0.0"}}')

    config = await detect_project_config(str(tmp_path))
    assert config["stack"] == "node"
    assert config["framework"] == "nextjs"
    assert "test_command" in config


@pytest.mark.asyncio
async def test_detect_python_project(tmp_path):
    """Detects a Python project from pyproject.toml."""
    from app.services.work_context import detect_project_config

    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\nname = 'test'")

    config = await detect_project_config(str(tmp_path))
    assert config["stack"] == "python"
    assert config["test_command"] == "pytest"


@pytest.mark.asyncio
async def test_detect_source_dirs(tmp_path):
    """Detects source directories."""
    from app.services.work_context import detect_project_config

    (tmp_path / "frontend-v2").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "package.json").write_text('{"dependencies": {"next": "15.0.0"}}')

    config = await detect_project_config(str(tmp_path))
    assert "frontend-v2" in config.get("source_dirs", [])
    assert "backend" in config.get("source_dirs", [])


@pytest.mark.asyncio
async def test_detect_docker_compose(tmp_path):
    """Detects docker-compose.yml."""
    from app.services.work_context import detect_project_config

    (tmp_path / "docker-compose.yml").write_text("version: '3'")

    config = await detect_project_config(str(tmp_path))
    assert config.get("has_docker") is True


def test_resolve_config_manual_overrides_auto():
    """Manual config overrides auto-detection."""
    from app.services.work_context import resolve_project_config

    auto = {"stack": "node", "framework": "nextjs", "test_command": "npm test"}
    manual = {"source_dir": "frontend-v2/", "notes": "NICHT in frontend/ arbeiten"}

    resolved = resolve_project_config(auto_config=auto, manual_config=manual)
    assert resolved["source_dir"] == "frontend-v2/"
    assert resolved["notes"] == "NICHT in frontend/ arbeiten"
    assert resolved["stack"] == "node"  # Auto value stays when there's no conflict


def test_build_config_section_for_dispatch():
    """Builds the project context section for the dispatch message."""
    from app.services.work_context import build_config_dispatch_section

    config = {
        "stack": "node",
        "framework": "nextjs",
        "source_dir": "frontend-v2/",
        "dev_command": "npm run dev -- -p {port}",
        "test_command": "npm run test:run",
        "notes": "NICHT in frontend/ arbeiten",
    }

    section = build_config_dispatch_section("MC Development", config, port=4200)
    assert "frontend-v2/" in section
    assert "npm run dev -- -p 4200" in section
    assert "NICHT in frontend/ arbeiten" in section


def test_build_config_section_without_port():
    """Without a port, {port} is not replaced."""
    from app.services.work_context import build_config_dispatch_section

    config = {"dev_command": "npm run dev -- -p {port}"}
    section = build_config_dispatch_section("Test", config, port=None)
    assert "{port}" in section  # Placeholder is preserved
