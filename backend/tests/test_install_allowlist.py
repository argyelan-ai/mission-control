import pytest
from app.services.install_allowlist import (
    validate_source,
    AllowlistError,
)


def test_skill_github_anthropic_allowed():
    assert validate_source("skill", "github:anthropic/skill-web-perf") is True


def test_skill_github_google_labs_code_allowed():
    assert validate_source("skill", "github:google-labs-code/stitch-skills") is True


def test_skill_github_google_labs_code_generic_allowed():
    assert validate_source("skill", "github:google-labs-code/some-skill") is True


def test_skill_github_getcursor_allowed():
    assert validate_source("skill", "github:getcursor/skill-test") is True


def test_skill_github_obra_allowed():
    assert validate_source("skill", "github:obra/skill-test") is True


def test_skill_random_github_rejected():
    with pytest.raises(AllowlistError):
        validate_source("skill", "github:random-user/evil-skill")


def test_plugin_official_marketplace_allowed():
    assert validate_source("plugin", "claude-plugins-official") is True


def test_plugin_random_source_rejected():
    with pytest.raises(AllowlistError):
        validate_source("plugin", "npm:random-package")


def test_mcp_modelcontextprotocol_npm_allowed():
    assert validate_source("mcp", "npm:@modelcontextprotocol/server-filesystem") is True


def test_mcp_supabase_npm_allowed():
    assert validate_source("mcp", "npm:@supabase/mcp-server-postgres") is True


def test_mcp_github_mcp_prefix_allowed():
    assert validate_source("mcp", "github:foo/mcp-bar") is True


def test_mcp_github_mcp_suffix_allowed():
    """Repos like geopopos/higgsfield_ai_mcp should be allowed — mcp anywhere in the name."""
    assert validate_source("mcp", "github:geopopos/higgsfield_ai_mcp") is True


def test_mcp_github_no_mcp_in_name_rejected():
    """Random repos without 'mcp' in the name must still be rejected."""
    with pytest.raises(AllowlistError):
        validate_source("mcp", "github:malicious/evil-tool")


def test_unknown_type_rejected():
    with pytest.raises(AllowlistError):
        validate_source("unknown_type", "github:foo/bar")


def test_empty_source_rejected():
    with pytest.raises(AllowlistError):
        validate_source("skill", "")
