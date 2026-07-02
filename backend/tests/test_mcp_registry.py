import json
import tempfile
from pathlib import Path

import pytest

from app.services.mcp_registry import (
    MCPRegistry,
    MCPManifest,
    MCPRegistryError,
)


@pytest.fixture
def tmp_registry_dir(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", tmp)
        yield Path(tmp)


def test_list_empty_registry(tmp_registry_dir):
    registry = MCPRegistry()
    assert registry.list_installed() == []


def test_load_valid_manifest(tmp_registry_dir):
    srv_dir = tmp_registry_dir / "filesystem"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "name": "filesystem",
        "transport": "stdio",
        "command": "node",
        "args": ["/mc-servers/filesystem/dist/index.js"],
        "env": {"ALLOWED_PATHS": "/workspace"},
        "description": "Read/write files in workspace",
        "source": "npm:@modelcontextprotocol/server-filesystem",
    }))
    registry = MCPRegistry()
    installed = registry.list_installed()
    assert len(installed) == 1
    assert installed[0].name == "filesystem"
    assert installed[0].transport == "stdio"
    assert installed[0].command == "node"


def test_load_manifest_missing_required_field(tmp_registry_dir):
    srv_dir = tmp_registry_dir / "broken"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "transport": "stdio",
        # name missing!
    }))
    registry = MCPRegistry()
    with pytest.raises(MCPRegistryError):
        registry.get_manifest("broken")


def test_render_mcp_json_entry_stdio(tmp_registry_dir):
    srv_dir = tmp_registry_dir / "filesystem"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "name": "filesystem",
        "transport": "stdio",
        "command": "node",
        "args": ["/mc-servers/filesystem/dist/index.js"],
        "env": {"ALLOWED_PATHS": "/workspace"},
    }))
    registry = MCPRegistry()
    entry = registry.render_mcp_json_entry("filesystem")
    assert entry == {
        "command": "node",
        "args": ["/mc-servers/filesystem/dist/index.js"],
        "env": {"ALLOWED_PATHS": "/workspace"},
    }


def test_render_mcp_json_entry_sse(tmp_registry_dir):
    srv_dir = tmp_registry_dir / "supabase"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "name": "supabase",
        "transport": "sse",
        "url": "https://mcp.supabase.com/sse",
        "headers": {"X-Auth-Token": "$SUPABASE_TOKEN"},
    }))
    registry = MCPRegistry()
    entry = registry.render_mcp_json_entry("supabase")
    assert entry == {
        "type": "sse",
        "url": "https://mcp.supabase.com/sse",
        "headers": {"X-Auth-Token": "$SUPABASE_TOKEN"},
    }


def test_uninstall_removes_dir(tmp_registry_dir):
    srv_dir = tmp_registry_dir / "filesystem"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text('{"name":"filesystem","transport":"stdio","command":"node","args":[]}')
    registry = MCPRegistry()
    assert len(registry.list_installed()) == 1
    registry.uninstall("filesystem")
    assert len(registry.list_installed()) == 0
    assert not srv_dir.exists()


def test_install_github_falls_back_to_mcp_json(tmp_registry_dir, monkeypatch):
    """If a github-sourced MCP lacks manifest.json but has .mcp.json,
    derive the manifest from the standard MCP client config."""
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    mcp_json_content = json.dumps({
        "mcpServers": {
            "higgsfield": {
                "command": "python",
                "args": ["-m", "higgsfield_mcp.server"],
                "env": {"HF_API_KEY": "${HF_API_KEY}"},
            }
        }
    })

    def fake_clone(cmd, capture_output=False, text=False, timeout=None, **kw):
        # simulate git clone by just creating target dir + .mcp.json
        assert cmd[0] == "git" and cmd[1] == "clone"
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / ".mcp.json").write_text(mcp_json_content)
        class R: returncode = 0; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_clone)
    registry = MCPRegistry()
    manifest = registry.install(
        "github:geopopos/higgsfield_ai_mcp", "higgsfield-ai"
    )
    assert manifest.name == "higgsfield-ai"
    assert manifest.transport == "stdio"
    assert manifest.command == "python"
    assert manifest.args == ["-m", "higgsfield_mcp.server"]
    assert manifest.env == {"HF_API_KEY": "${HF_API_KEY}"}


def test_install_github_with_branch_ref_passes_branch_flag(
    tmp_registry_dir, monkeypatch,
):
    """github:owner/repo@ref must translate into `git clone --branch ref`."""
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    captured: dict = {}

    def fake_clone(cmd, capture_output=False, text=False, timeout=None, **kw):
        captured["cmd"] = list(cmd)
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(json.dumps({
            "name": "higgsfield",
            "transport": "stdio",
            "command": "bun",
            "args": ["run", "src/index.ts"],
        }))
        class R: returncode = 0; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_clone)
    registry = MCPRegistry()
    manifest = registry.install(
        "github:test-owner/higgsfield-mcp-mc@ultra-plan-unlim",
        "higgsfield",
    )
    assert manifest.name == "higgsfield"
    assert "--branch" in captured["cmd"]
    i = captured["cmd"].index("--branch")
    assert captured["cmd"][i + 1] == "ultra-plan-unlim"
    # Repo URL must be the owner/repo part, stripped of the ref.
    assert any("test-owner/higgsfield-mcp-mc" in a for a in captured["cmd"])
    assert not any("@ultra-plan-unlim" in a for a in captured["cmd"])


def test_install_github_without_ref_uses_default_branch(
    tmp_registry_dir, monkeypatch,
):
    """Plain github:owner/repo must NOT pass --branch (server default)."""
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    captured: dict = {}

    def fake_clone(cmd, **kw):
        captured["cmd"] = list(cmd)
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(json.dumps({
            "name": "plain", "transport": "stdio",
            "command": "x", "args": [],
        }))
        class R: returncode = 0; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_clone)
    MCPRegistry().install("github:foo/mcp-plain", "plain")
    assert "--branch" not in captured["cmd"]


def test_install_github_empty_ref_raises(tmp_registry_dir):
    """Trailing '@' without a ref is a user typo, must be rejected early."""
    from app.services.mcp_registry import MCPRegistry
    with pytest.raises(MCPRegistryError, match="empty ref"):
        MCPRegistry().install("github:foo/bar@", "bar")


def test_install_github_without_any_manifest_fails(tmp_registry_dir, monkeypatch):
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    def fake_clone(cmd, **kw):
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        # No manifest.json, no .mcp.json
        class R: returncode = 0; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_clone)
    registry = MCPRegistry()
    with pytest.raises(MCPRegistryError, match="proposed_config"):
        registry.install("github:foo/mcp-empty", "empty-mcp")


def test_install_github_node_runs_npm_install(tmp_registry_dir, monkeypatch):
    """MCPs with package.json get npm install. Command from .mcp.json preserved."""
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    calls: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, timeout=None, cwd=None, **kw):
        calls.append(list(cmd))
        if cmd[0] == "git" and cmd[1] == "clone":
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "package.json").write_text(json.dumps({
                "name": "higgsfield-mcp", "type": "module",
                "dependencies": {}
            }))
            (target / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "higgsfield": {
                        "command": "bun",
                        "args": ["run", "src/index.ts"],
                    }
                }
            }))
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_run)
    registry = MCPRegistry()
    manifest = registry.install("github:jfikrat/higgsfield-mcp", "higgsfield")

    # Verify npm install was run
    npm_calls = [c for c in calls if c[0] == "npm"]
    assert len(npm_calls) == 1
    assert npm_calls[0][:2] == ["npm", "install"]
    # Command stays `bun` (not rewritten, unlike python → venv-python)
    assert manifest.command == "bun"
    assert manifest.args == ["run", "src/index.ts"]


def test_smoke_test_skips_when_command_missing(tmp_registry_dir):
    """Missing command binary → skip smoke test gracefully, return True."""
    import asyncio
    from app.services.mcp_registry import MCPRegistry

    srv_dir = tmp_registry_dir / "fake-mcp"
    srv_dir.mkdir()
    (srv_dir / "manifest.json").write_text(json.dumps({
        "name": "fake-mcp",
        "transport": "stdio",
        "command": "command-that-does-not-exist-ever",
        "args": [],
    }))
    registry = MCPRegistry()
    ok = asyncio.get_event_loop().run_until_complete(
        registry.smoke_test("fake-mcp", timeout=2.0)
    ) if False else asyncio.run(registry.smoke_test("fake-mcp", timeout=2.0))
    assert ok is True, "Should skip gracefully, not fail"


def test_install_github_uses_proposed_config_when_no_repo_config(tmp_registry_dir, monkeypatch):
    """Third fallback: proposed_config from install_request wins over 'not found'."""
    import subprocess as _sp
    from app.services.mcp_registry import MCPRegistry

    def fake_run(cmd, capture_output=False, text=False, timeout=None, cwd=None, **kw):
        if cmd[0] == "git" and cmd[1] == "clone":
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "package.json").write_text('{"name":"x"}')
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()

    monkeypatch.setattr(_sp, "run", fake_run)
    registry = MCPRegistry()
    manifest = registry.install(
        "github:jfikrat/higgsfield-mcp", "higgsfield",
        proposed_config={
            "command": "bun",
            "args": ["run", "src/index.ts"],
            "env": {"CURL_IMPERSONATE_BIN": ""},
            "transport": "stdio",
        },
    )
    assert manifest.command == "bun"
    assert manifest.args == ["run", "src/index.ts"]
    assert manifest.env == {"CURL_IMPERSONATE_BIN": ""}
