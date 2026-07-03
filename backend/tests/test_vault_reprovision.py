"""M.3 T2: Reprovision endpoint renders vault sections per agent scope.

Tests that TOOLS.md and SOUL.md contain (or omit) the vault sections
depending on whether the agent has vault:write in its effective scopes.

Test entry points:
  - generate_tools_md()     (tools_md_builder.py)
  - build_agent_context() + render_agent_file("SOUL.md.j2", ctx)  (template_renderer.py)

Both are pure functions — no DB / HTTP needed.
"""
import uuid

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file
from app.services.tools_md_builder import generate_tools_md

# ── Stable marker strings ────────────────────────────────────────────────────
# Verified against tools_md_builder.py line 1027 and SOUL.md.j2 line 3154.
TOOLS_VAULT_HEADER = "## Vault — long-term memory (Karpathy Wiki)"
SOUL_VAULT_HEADER  = "## Vault Writing Discipline"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_agent(scopes: list[str]) -> Agent:
    """Return a minimal Agent with the given scopes list."""
    return Agent(
        id=uuid.uuid4(),
        name="TestAgent",
        role="Developer",
        emoji="🤖",
        board_id=uuid.uuid4(),
        is_board_lead=False,
        scopes=scopes,
    )


def _tools_md(scopes: list[str]) -> str:
    return generate_tools_md(
        name="TestAgent",
        emoji="🤖",
        raw_token="dummy-token",
        board_id="board-test-uuid",
        is_board_lead=False,
        scopes=scopes,
    )


def _soul_md(scopes: list[str]) -> str:
    agent = _make_agent(scopes)
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("SOUL.md.j2", ctx)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_sync_config_renders_vault_section_for_vault_write_agent():
    """Agent with vault:write → TOOLS.md and SOUL.md both contain vault sections."""
    scopes = ["vault:read", "vault:write", "tasks:read", "tasks:write", "heartbeat"]

    tools = _tools_md(scopes)
    soul  = _soul_md(scopes)

    assert TOOLS_VAULT_HEADER in tools, (
        f"Expected TOOLS.md to contain '{TOOLS_VAULT_HEADER}' for vault:write agent"
    )
    assert SOUL_VAULT_HEADER in soul, (
        f"Expected SOUL.md to contain '{SOUL_VAULT_HEADER}' for vault:write agent"
    )


def test_sync_config_omits_vault_section_for_reader_only_agent():
    """Agent with vault:read but NOT vault:write → vault write sections absent."""
    scopes = ["vault:read", "tasks:read", "tasks:write", "heartbeat"]

    tools = _tools_md(scopes)
    soul  = _soul_md(scopes)

    assert TOOLS_VAULT_HEADER not in tools, (
        f"TOOLS.md must NOT contain '{TOOLS_VAULT_HEADER}' for read-only vault agent"
    )
    assert SOUL_VAULT_HEADER not in soul, (
        f"SOUL.md must NOT contain '{SOUL_VAULT_HEADER}' for read-only vault agent"
    )


def test_sync_config_omits_vault_for_no_vault_scope_agent():
    """Agent with NO vault scopes → neither file contains vault sections."""
    scopes = ["tasks:read", "tasks:write", "heartbeat", "knowledge:read"]

    tools = _tools_md(scopes)
    soul  = _soul_md(scopes)

    assert TOOLS_VAULT_HEADER not in tools, (
        f"TOOLS.md must NOT contain '{TOOLS_VAULT_HEADER}' for agent with no vault scope"
    )
    assert SOUL_VAULT_HEADER not in soul, (
        f"SOUL.md must NOT contain '{SOUL_VAULT_HEADER}' for agent with no vault scope"
    )
