import json
import tempfile
from pathlib import Path

from app.models.agent import Agent
from app.services.mcp_sync import (
    render_agent_mcp_json,
    render_agent_mcp_servers,
    sync_agent_mcp_to_disk,
)


def _setup_registry(tmp_root: Path) -> None:
    fs = tmp_root / "filesystem"
    fs.mkdir()
    (fs / "manifest.json").write_text(json.dumps({
        "name": "filesystem", "transport": "stdio",
        "command": "node",
        "args": ["/mc-servers/filesystem/dist/index.js"],
    }))
    sb = tmp_root / "supabase"
    sb.mkdir()
    (sb / "manifest.json").write_text(json.dumps({
        "name": "supabase", "transport": "sse",
        "url": "https://mcp.supabase.com/sse",
    }))


def test_render_agent_mcp_json_null_means_all(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        _setup_registry(tmp_root)
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(tmp_root))

        agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=None)
        config = render_agent_mcp_json(agent)
        assert set(config["mcpServers"].keys()) == {"filesystem", "supabase"}


def test_render_agent_mcp_json_allowlist(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        _setup_registry(tmp_root)
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(tmp_root))

        agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=["filesystem"])
        config = render_agent_mcp_json(agent)
        assert set(config["mcpServers"].keys()) == {"filesystem"}


def test_render_agent_mcp_json_empty_list_means_none(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        _setup_registry(tmp_root)
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(tmp_root))

        agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=[])
        config = render_agent_mcp_json(agent)
        assert config["mcpServers"] == {}


def test_render_user_scope_entries_have_explicit_type(monkeypatch):
    """User-scope openclaude entries need an explicit `type` tag."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        _setup_registry(tmp_root)
        monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(tmp_root))

        agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=None)
        servers = render_agent_mcp_servers(agent)

        assert servers["filesystem"]["type"] == "stdio"
        assert servers["filesystem"]["command"] == "node"
        assert servers["filesystem"]["args"] == ["/mc-servers/filesystem/dist/index.js"]
        assert servers["supabase"]["type"] == "sse"
        assert servers["supabase"]["url"] == "https://mcp.supabase.com/sse"


def test_sync_writes_mcp_servers_to_claude_json(monkeypatch, tmp_path):
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    _setup_registry(registry_root)
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(registry_root))
    monkeypatch.setenv("MC_AGENTS_ROOT", str(agents_root))

    agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=["filesystem"])
    slug = agent.name.lower()
    (agents_root / slug / "claude-config").mkdir(parents=True)

    path = sync_agent_mcp_to_disk(agent)

    assert path.name == ".claude.json"
    data = json.loads(path.read_text())
    assert "filesystem" in data["mcpServers"]
    assert data["mcpServers"]["filesystem"]["type"] == "stdio"


def test_sync_preserves_existing_claude_json_fields(monkeypatch, tmp_path):
    """openclaude writes startup counters etc. into .claude.json. Don't clobber."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    _setup_registry(registry_root)
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(registry_root))
    monkeypatch.setenv("MC_AGENTS_ROOT", str(agents_root))

    agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=["filesystem"])
    slug = agent.name.lower()
    cfg_dir = agents_root / slug / "claude-config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / ".claude.json").write_text(json.dumps({
        "numStartups": 42,
        "tipsHistory": {"something": 7},
        "mcpServers": {"oldone": {"type": "stdio", "command": "stale"}},
    }))

    sync_agent_mcp_to_disk(agent)

    data = json.loads((cfg_dir / ".claude.json").read_text())
    assert data["numStartups"] == 42
    assert data["tipsHistory"] == {"something": 7}
    assert set(data["mcpServers"].keys()) == {"filesystem"}


def test_sync_removes_stale_project_mcp_json(monkeypatch, tmp_path):
    """Old installs left a .mcp.json — delete it so it doesn't confuse anyone."""
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    _setup_registry(registry_root)
    agents_root = tmp_path / "agents"
    agents_root.mkdir()
    monkeypatch.setenv("MC_MCP_REGISTRY_DIR", str(registry_root))
    monkeypatch.setenv("MC_AGENTS_ROOT", str(agents_root))

    agent = Agent(name="Spark", role="developer", scopes=[], mcp_servers=["filesystem"])
    slug = agent.name.lower()
    cfg_dir = agents_root / slug / "claude-config"
    cfg_dir.mkdir(parents=True)
    stale = cfg_dir / ".mcp.json"
    stale.write_text(json.dumps({"mcpServers": {"ghost": {"command": "x"}}}))

    sync_agent_mcp_to_disk(agent)

    assert not stale.exists()
