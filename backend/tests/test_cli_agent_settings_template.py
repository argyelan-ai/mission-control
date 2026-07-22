"""Renders the real cli_agent_settings.json.j2 and checks the W2.1 hooks block.

The plugin_manager sync tests use a minimal INLINE template, so they never
exercise the hooks block added for the hook-based turn signal (Phase A). This
file renders the actual backend/templates/cli_agent_settings.json.j2 and
asserts the rendered settings.json is valid JSON with a correct hooks block —
without touching the plugin cache on disk (hermetic, jinja2 only).
"""

import json
import pathlib

import pytest
from jinja2 import Environment, FileSystemLoader

_TEMPLATES = pathlib.Path(__file__).parent.parent / "templates"


def _render(extra_marketplaces):
    env = Environment(loader=FileSystemLoader(str(_TEMPLATES)))
    template = env.get_template("cli_agent_settings.json.j2")
    return template.render(
        system_prompt='You are "Rex".\nMulti-line\tprompt.',
        model="claude-sonnet-4-6",
        enabled_plugins={"superpowers@claude-plugins-official": True, "x@y": False},
        extra_marketplaces=extra_marketplaces,
    )


@pytest.mark.parametrize(
    "extra_marketplaces",
    [{}, {"claude-plugins-official": {"installLocation": "/home/agent/x"}}],
    ids=["no-marketplaces", "with-marketplaces"],
)
def test_rendered_settings_is_valid_json_with_hooks(extra_marketplaces):
    data = json.loads(_render(extra_marketplaces))

    # Existing keys must survive unchanged.
    assert data["model"] == "claude-sonnet-4-6"
    assert data["systemPrompt"] == 'You are "Rex".\nMulti-line\tprompt.'
    assert data["skipDangerousModePermissionPrompt"] is True
    assert data["enabledPlugins"]["superpowers@claude-plugins-official"] is True
    assert data["enabledPlugins"]["x@y"] is False
    if extra_marketplaces:
        assert data["extraKnownMarketplaces"] == extra_marketplaces
    else:
        assert "extraKnownMarketplaces" not in data

    # Hooks block: UserPromptSubmit → submit, Stop → stop, both append to the
    # turn-signal file the poll.sh turn-state reader watches.
    hooks = data["hooks"]
    submit_cmd = hooks["UserPromptSubmit"][0]["hooks"][0]
    stop_cmd = hooks["Stop"][0]["hooks"][0]

    for entry, kind in ((submit_cmd, "submit"), (stop_cmd, "stop")):
        assert entry["type"] == "command"
        assert entry["timeout"] == 5
        # Shell command must append "<epoch> <kind>" to /home/agent/.turn-signal.
        # The JSON string carries a literal backslash-n so the shell printf
        # writes a real newline.
        assert entry["command"] == (
            "printf '%s " + kind + "\\n' $(date +%s) >> /home/agent/.turn-signal"
        ), entry["command"]
