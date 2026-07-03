"""Tests for the plugin_manager service."""
import json
from unittest.mock import patch

from app.services.plugin_manager import list_available_plugins, render_agent_installed_plugins, sync_agent_plugins_to_disk


def test_list_available_plugins_empty(tmp_path):
    """Empty cache returns an empty list."""
    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = list_available_plugins()
        assert result == []


def test_list_available_plugins_parses_correctly(tmp_path):
    """Plugins are correctly parsed from installed_plugins.json."""
    ipj = {
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
            "claude-mem@thedotmack": [{"version": "10.6.3"}],
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(ipj))

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = list_available_plugins()
        assert len(result) == 2
        names = {p.name for p in result}
        assert "superpowers" in names
        assert "claude-mem" in names
        # Check sorted
        assert result[0].name <= result[1].name


def test_list_available_plugins_version_parsing(tmp_path):
    """Version is correctly parsed from the first entry."""
    ipj = {
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(ipj))

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = list_available_plugins()
        assert result[0].version == "5.0.7"
        assert result[0].source == "claude-plugins-official"
        assert result[0].key == "superpowers@claude-plugins-official"


def test_list_available_plugins_invalid_json(tmp_path):
    """Malformed JSON returns an empty list."""
    (tmp_path / "installed_plugins.json").write_text("not json")

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = list_available_plugins()
        assert result == []


def test_render_agent_installed_plugins_filters(tmp_path):
    """Only assigned plugins are written into the agent-specific JSON."""
    ipj = {
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
            "github@claude-plugins-official": [{"version": "1.0.0"}],
            "firecrawl@claude-plugins-official": [{"version": "1.0.3"}],
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(ipj))

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = json.loads(render_agent_installed_plugins(["superpowers@claude-plugins-official"]))
        assert len(result["plugins"]) == 1
        assert "superpowers@claude-plugins-official" in result["plugins"]


def test_render_agent_installed_plugins_none_returns_all(tmp_path):
    """None returns all plugins."""
    ipj = {
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
            "github@claude-plugins-official": [{"version": "1.0.0"}],
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(ipj))

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = json.loads(render_agent_installed_plugins(None))
        assert len(result["plugins"]) == 2


def test_render_agent_installed_plugins_empty_list(tmp_path):
    """Empty list returns no plugins."""
    ipj = {
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
        }
    }
    (tmp_path / "installed_plugins.json").write_text(json.dumps(ipj))

    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = json.loads(render_agent_installed_plugins([]))
        assert len(result["plugins"]) == 0


def test_render_agent_installed_plugins_no_file(tmp_path):
    """Missing file returns an empty result."""
    with patch("app.services.plugin_manager._plugins_dir", return_value=tmp_path):
        result = json.loads(render_agent_installed_plugins(None))
        assert result == {"version": 2, "plugins": {}}


# ---------------------------------------------------------------------------
# Helper: minimal template dir for sync tests
# ---------------------------------------------------------------------------

def _setup_sync_env(tmp_path, plugins_data=None, km_data=None):
    """Creates a minimal directory structure for sync_agent_plugins_to_disk tests."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "installed_plugins.json").write_text(json.dumps(
        plugins_data or {"version": 2, "plugins": {}}
    ))
    (plugins_dir / "known_marketplaces.json").write_text(json.dumps(km_data or {}))

    agents_dir = tmp_path / "agents"
    agent_dir = agents_dir / "testbot"
    agent_dir.mkdir(parents=True)
    plugin_out = agent_dir / "claude-config" / "plugins"
    plugin_out.mkdir(parents=True)

    tmpl_dir = tmp_path / "templates"
    tmpl_dir.mkdir()
    (tmpl_dir / "cli_agent_settings.json.j2").write_text(
        '{"systemPrompt":"{{ system_prompt }}","model":"{{ model }}",'
        '"enabledPlugins":{{ enabled_plugins | tojson }},'
        '"extraKnownMarketplaces":{{ extra_marketplaces | tojson }}}'
    )

    return plugins_dir, agents_dir, tmpl_dir, plugin_out


def test_sync_writes_settings_to_canonical_and_docker_mirror(tmp_path):
    """settings.json must be written to both parent dir and claude-config/ mirror.

    The parent is the host source of truth; the mirror is what Docker mounts
    into the container. A plugin-update that only touches the parent leaves
    containers reading stale enabledPlugins (which is exactly how the old
    claude-mem hook kept firing after it was removed from DB).
    """
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(
        tmp_path,
        plugins_data={
            "version": 2,
            "plugins": {
                "keeper@claude-plugins-official": [{"version": "1.0.0"}],
                "dropper@thedotmack": [{"version": "1.0.0"}],
            },
        },
    )

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        sync_agent_plugins_to_disk(
            "testbot", "prompt", "model",
            ["keeper@claude-plugins-official"],
        )

    parent = json.loads((agents_dir / "testbot" / "settings.json").read_text())
    mirror = json.loads((agents_dir / "testbot" / "claude-config" / "settings.json").read_text())

    assert parent == mirror, "parent and mirror settings.json must match"
    assert parent["enabledPlugins"]["keeper@claude-plugins-official"] is True
    assert parent["enabledPlugins"]["dropper@thedotmack"] is False


def test_sync_replaces_stale_settings_symlink(tmp_path):
    """Historical installs symlinked claude-config/settings.json to the parent.
    That breaks in Docker (mount boundary), so sync must replace the symlink.
    """
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(tmp_path)
    canonical = agents_dir / "testbot" / "settings.json"
    canonical.write_text("{}")
    mirror = agents_dir / "testbot" / "claude-config" / "settings.json"
    mirror.symlink_to(canonical)
    assert mirror.is_symlink()

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        sync_agent_plugins_to_disk("testbot", "prompt", "model", [])

    assert not mirror.is_symlink()
    assert mirror.exists()


def test_sync_writes_known_marketplaces(tmp_path):
    """sync_agent_plugins_to_disk writes known_marketplaces.json with container paths."""
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(
        tmp_path,
        plugins_data={
            "version": 2,
            "plugins": {"superpowers@claude-plugins-official": [{"version": "5.0.7"}]},
        },
        km_data={
            "claude-plugins-official": {
                "source": {"source": "github", "repo": "anthropics/claude-plugins-official"},
                "installLocation": "/Users/testuser/.openclaw/plugins/marketplaces/claude-plugins-official",
                "lastUpdated": "2026-04-12T00:00:00.000Z",
            }
        },
    )

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        result = sync_agent_plugins_to_disk("testbot", "prompt", "model", ["superpowers@claude-plugins-official"])

    assert result.get("known_marketplaces.json") is True

    km_file = plugin_out / "known_marketplaces.json"
    assert km_file.exists()
    data = json.loads(km_file.read_text())
    loc = data["claude-plugins-official"]["installLocation"]
    assert loc == "/home/agent/.claude/plugins/marketplaces/claude-plugins-official"
    assert "/Users/" not in loc


def test_sync_replaces_known_marketplaces_symlink(tmp_path):
    """Existing symlinks are replaced by real files."""
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(tmp_path)

    # Create symlink (simulates old state)
    symlink_target = plugin_out / "known_marketplaces.json"
    symlink_target.symlink_to("../../../plugins/known_marketplaces.json")
    assert symlink_target.is_symlink()

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        sync_agent_plugins_to_disk("testbot", "", "model", [])

    assert not symlink_target.is_symlink()
    assert symlink_target.is_file()


def test_sync_copies_cache_directories(tmp_path):
    """sync only copies needed marketplace dirs from the shared cache."""
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(
        tmp_path,
        plugins_data={
            "version": 2,
            "plugins": {"superpowers@claude-plugins-official": [{"version": "5.0.7"}]},
        },
    )

    # Shared cache with 2 marketplaces
    cache_dir = plugins_dir / "cache"
    (cache_dir / "claude-plugins-official" / "superpowers" / "5.0.7").mkdir(parents=True)
    (cache_dir / "claude-plugins-official" / "superpowers" / "5.0.7" / "SKILL.md").write_text("# Superpowers")
    (cache_dir / "thedotmack" / "claude-mem" / "10.6.3").mkdir(parents=True)
    (cache_dir / "thedotmack" / "claude-mem" / "10.6.3" / "SKILL.md").write_text("# Mem")

    # Shared marketplaces
    mp_dir = plugins_dir / "marketplaces"
    (mp_dir / "claude-plugins-official").mkdir(parents=True)
    (mp_dir / "claude-plugins-official" / "plugin-registry.json").write_text("{}")
    (mp_dir / "thedotmack").mkdir(parents=True)
    (mp_dir / "thedotmack" / "plugin-registry.json").write_text("{}")

    # Only superpowers assigned — thedotmack should NOT be copied
    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        result = sync_agent_plugins_to_disk("testbot", "prompt", "model", ["superpowers@claude-plugins-official"])

    assert result.get("cache") is True
    assert result.get("marketplaces") is True

    # claude-plugins-official copied
    assert (plugin_out / "cache" / "claude-plugins-official" / "superpowers" / "5.0.7" / "SKILL.md").exists()
    # thedotmack NOT copied
    assert not (plugin_out / "cache" / "thedotmack").exists()

    # marketplaces: claude-plugins-official copied, thedotmack not
    assert (plugin_out / "marketplaces" / "claude-plugins-official" / "plugin-registry.json").exists()
    assert not (plugin_out / "marketplaces" / "thedotmack").exists()


def test_sync_copies_all_cache_when_cli_plugins_none(tmp_path):
    """cli_plugins=None copies all marketplace dirs."""
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(
        tmp_path,
        plugins_data={
            "version": 2,
            "plugins": {
                "superpowers@claude-plugins-official": [{"version": "5.0.7"}],
                "claude-mem@thedotmack": [{"version": "10.6.3"}],
            },
        },
    )

    cache_dir = plugins_dir / "cache"
    (cache_dir / "claude-plugins-official").mkdir(parents=True)
    (cache_dir / "claude-plugins-official" / "data.json").write_text("{}")
    (cache_dir / "thedotmack").mkdir(parents=True)
    (cache_dir / "thedotmack" / "data.json").write_text("{}")

    mp_dir = plugins_dir / "marketplaces"
    (mp_dir / "claude-plugins-official").mkdir(parents=True)
    (mp_dir / "thedotmack").mkdir(parents=True)

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        result = sync_agent_plugins_to_disk("testbot", "prompt", "model", None)

    assert (plugin_out / "cache" / "claude-plugins-official").exists()
    assert (plugin_out / "cache" / "thedotmack").exists()


def test_sync_replaces_cache_symlink(tmp_path):
    """Existing cache symlink is replaced by a real directory."""
    plugins_dir, agents_dir, tmpl_dir, plugin_out = _setup_sync_env(tmp_path)
    (plugins_dir / "cache").mkdir()

    # Create symlink
    cache_symlink = plugin_out / "cache"
    cache_symlink.symlink_to("../../../plugins/cache")
    assert cache_symlink.is_symlink()

    with patch("app.services.plugin_manager._plugins_dir", return_value=plugins_dir), \
         patch("app.services.plugin_manager._agents_dir", return_value=agents_dir), \
         patch("app.services.plugin_manager._templates_dir", return_value=tmpl_dir):
        sync_agent_plugins_to_disk("testbot", "", "model", [])

    assert not cache_symlink.is_symlink()
    assert cache_symlink.is_dir()
