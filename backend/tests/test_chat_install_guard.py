"""Operator rule (12.10.2026): installing plugins/skills/MCP servers from the
phone/chat only via an approval card — that card flow does not exist yet.
Until it does, ``send_text`` (the one chat-input path every harness and
every runtime — docker TUI, Boss, headless ACP — funnels through, see
``agent_chat_input.send_text``) refuses any message whose first slash
command is an install/modify verb for plugins, marketplaces, MCP servers or
skills. Read-only variants (list, search, help, discover, installed, status,
a bare ``/mcp``/``/marketplace``) stay allowed — they cannot change what is
installed, so the gap they'd need the approval card for does not apply.

Table test derived live from two sources (not guessed):
- omp (oh-my-pi) ACP builtins — ``~/.mc/agenten-richtung-2026-10/src/oh-my-pi/
  packages/coding-agent/src/slash-commands/builtin-marketplace.ts`` (the
  ``/marketplace`` and ``/plugins``/``/plugin`` subcommand tables) and
  ``builtin-session.ts`` (``/mcp`` subcommand table).
- Claude Code's own CLI binary strings (``strings <claude binary>``, 2.1.295):
  ``/plugin install|enable|disable``, ``/plugin marketplace add|remove``,
  and ``Usage: /mcp [reconnect|enable|disable [<server>|all]]`` (Claude Code
  has no slash-form ``/mcp add`` — that is ``claude mcp add`` on the host
  shell, out of scope for chat text).

This does NOT stop an agent from running ``omp plugin install`` (or similar)
in its own shell once a message reaches it — that is a separate, unsolved
permission-mode gap (see the PR description)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


class _StubAgent:
    def __init__(self, slug="rex", agent_runtime="cli-bridge", harness="claude"):
        self.slug = slug
        self.agent_runtime = agent_runtime
        self.harness = harness
        self.id = slug


# ══════════════════════════════════════════════════════════════════════════
# Table test — blocked vs. allowed, harness-neutral (the guard runs before
# any harness-specific dispatch, so one table covers every harness/runtime).
# ══════════════════════════════════════════════════════════════════════════

BLOCKED = [
    # omp: /marketplace <verb> — install/modify subcommands
    "/marketplace add foo/bar",
    "/marketplace remove foo",
    "/marketplace rm foo",
    "/marketplace install plugin@market",
    "/marketplace uninstall plugin@market",
    "/marketplace upgrade plugin@market",
    # omp: /plugins (and its alias /plugin) enable/disable
    "/plugins enable plugin@market",
    "/plugins disable plugin@market",
    "/plugin enable plugin@market",
    "/plugin disable plugin@market",
    # Claude Code: /plugin install, enable, disable
    "/plugin install frontend-design@anthropics",
    # Claude Code: /plugin marketplace add|remove (nested one level deeper
    # than omp's top-level /marketplace)
    "/plugin marketplace add anthropics/claude-plugins-official",
    "/plugin marketplace remove anthropics",
    # omp + Claude Code: /mcp add|remove (omp) / enable|disable (both)
    "/mcp add my-server -- my-command",
    "/mcp remove my-server",
    "/mcp enable my-server",
    "/mcp disable all",
    # omp skillshare: /skills install|update
    "/skills install @scope/name",
    "/skills update @scope/name",
    # bare /install — not a real builtin anywhere onboarded today, but an
    # obvious phrasing to refuse outright rather than forward as a prompt
    "/install",
    "/install some-plugin",
    # case/whitespace must not be a bypass
    "/MARKETPLACE INSTALL plugin@market",
    "  /marketplace install plugin@market  ",
]

ALLOWED = [
    # plain chat — the overwhelming common case
    "hi, can you check the deploy logs?",
    "please run the tests again",
    # read-only /marketplace
    "/marketplace",
    "/marketplace list",
    "/marketplace discover",
    "/marketplace installed",
    "/marketplace help",
    "/marketplace update",  # refreshes the catalog, installs nothing
    # read-only /plugins / /plugin
    "/plugins",
    "/plugins list",
    "/plugin list",
    "/plugin directory query",
    # read-only /mcp
    "/mcp",
    "/mcp list",
    "/mcp test my-server",
    "/mcp reconnect my-server",
    "/mcp resources",
    # read-only /skills
    "/skills",
    "/skills search some-query",
    "/skills installed",
    # unrelated builtins that merely start with a similar-looking word
    "/model sonnet",
    "/status",
    "/help",
    # a command mentioning "install" later in the text, not as the first
    # token, is prose to the agent — not something MC parses as a command
    "please don't /plugin install anything without asking me first",
]


@pytest.mark.parametrize("text", BLOCKED)
async def test_send_text_blocks_install_commands(text):
    from app.services.agent_chat_input import InstallCommandBlockedError, send_text

    agent = _StubAgent()
    with pytest.raises(InstallCommandBlockedError):
        await send_text(agent, text)


@pytest.mark.parametrize("text", ALLOWED)
async def test_send_text_allows_everything_else(monkeypatch, text):
    """These must reach normal dispatch — stubbed out here (any one of them
    would otherwise try a real ``docker exec``), proving the guard did NOT
    raise for them rather than merely not raising ``InstallCommandBlockedError``
    specifically (a guard that swallowed everything into a different
    exception would pass the BLOCKED table and silently fail this one)."""
    from app.services import agent_chat_input

    called = {}

    async def _fake_wait_for_send_readiness(agent):
        return None

    async def _fake_touch_recycler_marker(slug):
        return None

    async def _fake_run_docker_exec(*args, **kwargs):
        called["ran"] = True
        return None

    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _fake_wait_for_send_readiness)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _fake_touch_recycler_marker)
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _fake_run_docker_exec)

    agent = _StubAgent()
    await agent_chat_input.send_text(agent, text)
    assert called.get("ran") is True, f"{text!r} never reached dispatch"


# ══════════════════════════════════════════════════════════════════════════
# Sabotage probe — the guard must fire on the FIRST token only, and must not
# be satisfied by a prefix/substring match (both would be a silent bypass
# or a silent over-block).
# ══════════════════════════════════════════════════════════════════════════


async def test_sabotage_probe_leading_whitespace_still_blocks(monkeypatch):
    """A message starting with spaces before the slash is still a command to
    every harness's own parser — the guard must strip, not just check index 0."""
    from app.services.agent_chat_input import InstallCommandBlockedError, send_text

    agent = _StubAgent()
    with pytest.raises(InstallCommandBlockedError):
        await send_text(agent, "   /marketplace install evil@market")


async def test_sabotage_probe_command_in_second_line_is_not_blocked(monkeypatch):
    """Only the FIRST token of the message counts — a slash command typed on
    a later line of a multi-line message is not something any harness
    dispatches as a command (it is literal text to the model), so blocking
    it would be a false positive the operator never asked for."""
    from app.services import agent_chat_input

    called = {}

    async def _noop(*args, **kwargs):
        called["ran"] = True
        return None

    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _noop)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _noop)
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _noop)

    agent = _StubAgent()
    await agent_chat_input.send_text(agent, "please do this:\n/marketplace install evil@market")
    assert called.get("ran") is True


async def test_sabotage_probe_near_miss_subcommand_not_blocked(monkeypatch):
    """A subcommand that merely SOUNDS like an install verb but isn't one in
    any onboarded harness (``/mcp installme``) must not trip the guard —
    proves the match is exact-verb, not a substring/prefix check that would
    quietly over-block legitimate traffic."""
    from app.services import agent_chat_input

    called = {}

    async def _noop(*args, **kwargs):
        called["ran"] = True
        return None

    monkeypatch.setattr(agent_chat_input, "_wait_for_send_readiness", _noop)
    monkeypatch.setattr(agent_chat_input, "_touch_recycler_marker", _noop)
    monkeypatch.setattr(agent_chat_input, "_run_docker_exec", _noop)

    agent = _StubAgent()
    await agent_chat_input.send_text(agent, "/mcp installme")
    assert called.get("ran") is True


async def test_sabotage_probe_matched_command_is_echoed_for_the_ui(monkeypatch):
    """The exception must carry the exact matched command text, not just a
    bool — the UI needs to be able to say what it refused, not just that it
    refused something."""
    from app.services.agent_chat_input import InstallCommandBlockedError, send_text

    agent = _StubAgent()
    with pytest.raises(InstallCommandBlockedError) as exc:
        await send_text(agent, "/marketplace install evil@market")
    assert exc.value.command == "/marketplace install"


# ══════════════════════════════════════════════════════════════════════════
# Router — POST /agents/{id}/chat/input maps the guard to an honest 409, for
# EVERY harness (the guard runs inside send_text before any harness branch,
# so one router test with the fleet default harness proves it for all of
# them — the per-harness differences live only in the table test above).
# ══════════════════════════════════════════════════════════════════════════


async def test_router_maps_install_blocked_to_409(auth_client, make_agent):
    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input",
        json={"text": "/marketplace install evil@market"},
    )

    assert res.status_code == 409, res.text
    assert res.json() == {
        "reason": "install_blocked",
        "command": "/marketplace install",
    }


async def test_router_allows_read_only_marketplace_list(auth_client, make_agent, monkeypatch):
    """A read-only subcommand must reach real dispatch — stubbed out here so
    the test proves the router's own routing (not a second guard trip)."""
    import app.routers.agent_chat as agent_chat_mod

    agent = await make_agent(name="Rex", agent_runtime="cli-bridge", harness="claude")

    async def _fake_send_text(a, text):
        return None

    monkeypatch.setattr(agent_chat_mod, "send_text", _fake_send_text)

    res = await auth_client.post(
        f"/api/v1/agents/{agent.id}/chat/input",
        json={"text": "/marketplace list"},
    )

    assert res.status_code == 204, res.text


# ══════════════════════════════════════════════════════════════════════════
# Slash picker — install-capable commands hidden from autocomplete entirely
# (coarser than the text guard: the picker only knows the command NAME, not
# which subcommand the operator is about to type, so a whole name is hidden
# rather than toggled per-subcommand). Covers both paths
# ``slash_command_capabilities`` has: the static builtin table (claude/
# openclaude/omp over its TUI) and the headless ACP-advertised list (omp/
# hermes chat daemons).
# ══════════════════════════════════════════════════════════════════════════


async def test_picker_hides_install_capable_builtins_for_claude_harness():
    from app.services import agent_chat_input

    agent = _StubAgent(slug="rex", agent_runtime="cli-bridge", harness="claude")
    names = {
        c["name"]
        for c in (await agent_chat_input.slash_command_capabilities(agent))["slashCommands"]
    }
    for hidden in ("marketplace", "plugin", "plugins", "mcp", "skills", "install"):
        assert hidden not in names
    # The claude builtin table's own harmless entries must still be there —
    # proves the filter removed specific names, not everything.
    assert "model" in names
    assert "help" in names


async def test_picker_hides_install_capable_acp_advertised_commands(monkeypatch):
    """The headless path: commands come from the agent's own ACP state file
    (``available_commands_update``), not a static table — a real omp/hermes
    daemon DOES advertise ``marketplace``/``plugins``/``mcp`` (see
    ``ACP_BUILTIN_SLASH_COMMANDS`` in oh-my-pi's acp-builtins.ts), so this
    proves the filter also strips THOSE, not just the static builtin
    tables."""
    from app.services import agent_chat_input

    state = {
        "commands": [
            {"name": "usage", "description": "Show usage", "input": None},
            {"name": "marketplace", "description": "Manage plugins from marketplaces", "input": None},
            {"name": "plugins", "description": "Manage plugins", "input": None},
            {"name": "mcp", "description": "Manage MCP servers", "input": None},
        ],
    }
    monkeypatch.setattr(agent_chat_input, "read_acp_chat_state", lambda agent: state)
    monkeypatch.setattr(agent_chat_input, "headless_chat_kind", lambda agent: "acp-docker")

    agent = _StubAgent(slug="acp-one", agent_runtime="cli-bridge", harness="omp")
    names = {
        c["name"]
        for c in (await agent_chat_input.slash_command_capabilities(agent))["slashCommands"]
    }
    assert "usage" in names
    for hidden in ("marketplace", "plugins", "mcp"):
        assert hidden not in names
