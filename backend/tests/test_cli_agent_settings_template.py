"""Renders the real cli_agent_settings.json.j2 and checks the W2.1 hooks block.

The plugin_manager sync tests use a minimal INLINE template, so they never
exercise the hooks block added for the hook-based turn signal (Phase A). This
file renders the actual backend/templates/cli_agent_settings.json.j2 and
asserts the rendered settings.json is valid JSON, with the hooks block present
for the claude harness (turn_signal_hooks=True) and ABSENT for openclaude
(turn_signal_hooks=False) — without touching the plugin cache on disk.
"""

import json
import pathlib

import pytest
from jinja2 import Environment, FileSystemLoader

_TEMPLATES = pathlib.Path(__file__).parent.parent / "templates"


def _render(extra_marketplaces, turn_signal_hooks):
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)))
    template = env.get_template("cli_agent_settings.json.j2")
    return template.render(
        system_prompt='You are "Rex".\nMulti-line\tprompt.',
        model="claude-sonnet-4-6",
        enabled_plugins={"superpowers@claude-plugins-official": True, "x@y": False},
        extra_marketplaces=extra_marketplaces,
        turn_signal_hooks=turn_signal_hooks,
    )


def _assert_common(data, extra_marketplaces):
    """Keys that must survive unchanged regardless of the hooks branch."""
    assert data["model"] == "claude-sonnet-4-6"
    assert data["systemPrompt"] == 'You are "Rex".\nMulti-line\tprompt.'
    assert data["skipDangerousModePermissionPrompt"] is True
    assert data["enabledPlugins"]["superpowers@claude-plugins-official"] is True
    assert data["enabledPlugins"]["x@y"] is False
    if extra_marketplaces:
        assert data["extraKnownMarketplaces"] == extra_marketplaces
    else:
        assert "extraKnownMarketplaces" not in data


@pytest.mark.parametrize(
    "extra_marketplaces",
    [{}, {"claude-plugins-official": {"installLocation": "/home/agent/x"}}],
    ids=["no-marketplaces", "with-marketplaces"],
)
def test_claude_harness_renders_hooks(extra_marketplaces):
    data = json.loads(_render(extra_marketplaces, turn_signal_hooks=True))
    _assert_common(data, extra_marketplaces)

    hooks = data["hooks"]
    submit_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]
    stop_cmd = hooks["Stop"][0]["hooks"][0]
    for entry, kind in ((submit_cmd, "submit"), (stop_cmd, "stop")):
        assert entry["type"] == "command"
        assert entry["timeout"] == 5
        # Shell command appends "<epoch> <kind>" to /home/agent/.turn-signal.
        # The JSON string carries a literal backslash-n so the shell printf
        # writes a real newline.
        assert entry["command"] == (
            "printf '%s " + kind + "\\n' $(date +%s) >> /home/agent/.turn-signal"
        ), entry["command"]


@pytest.mark.parametrize(
    "extra_marketplaces",
    [{}, {"claude-plugins-official": {"installLocation": "/home/agent/x"}}],
    ids=["no-marketplaces", "with-marketplaces"],
)
def test_openclaude_harness_omits_hooks(extra_marketplaces):
    # openclaude's tolerance to an unknown top-level `hooks` key is unproven,
    # so the block must be ELIMINATED, not merely hoped harmless.
    data = json.loads(_render(extra_marketplaces, turn_signal_hooks=False))
    _assert_common(data, extra_marketplaces)
    assert "hooks" not in data
