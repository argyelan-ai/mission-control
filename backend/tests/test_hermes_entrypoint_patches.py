"""entrypoint.sh must run the config patcher AFTER sourcing agent.env (ADR-064)."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "docker" / "hermes" / "entrypoint.sh"


def test_entrypoint_invokes_config_patcher_after_env():
    txt = ENTRYPOINT.read_text()
    assert "hermes-config-patch.py" in txt
    src_idx = txt.index('. "$ENV_FILE"')
    patch_idx = txt.index("hermes-config-patch.py")
    start_idx = txt.index("new-session")
    assert src_idx < patch_idx < start_idx, "patcher must run after env source, before tmux start"


def test_watchdog_loop_sources_agent_env_in_window_shell():
    """tmux windows inherit env from the tmux SERVER, not from the client that
    runs `new-session` (grok lesson, ADR-068 / grok-bridge _grok_launch_shell_cmd).
    Sourcing agent.env in the entrypoint's own shell therefore never reaches the
    window process when the tmux server already exists.

    Live incident 2026-07-12: hermes ran since Jul 7 with a 4.4KB quote-mangled
    MC_AGENT_TOKEN in its process env — `mc comment`/`mc finish` failed, tasks
    hung in review. The watchdog loop must (re-)source agent.env INSIDE the
    window shell before every hermes start (also refreshes a rotated token on
    each watchdog restart)."""
    txt = ENTRYPOINT.read_text()
    start = txt.index("while true; do")
    end = txt.index("done", start)
    loop = txt[start:end]
    assert "set -a" in loop, "watchdog loop must enable auto-export before sourcing"
    assert "$ENV_FILE" in loop, "watchdog loop must source agent.env itself"
    assert "set +a" in loop
    # env source must happen BEFORE the hermes invocation
    assert loop.index("$ENV_FILE") < loop.index("$HERMES_BIN")
