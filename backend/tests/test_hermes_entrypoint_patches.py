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


def test_patch_script_path_resolves_via_mc_repo_path_with_fallback():
    """PATCH_SCRIPT must not hardcode Mark's machine — public AGPL repo, any
    install. Same convention as kimi-host/boss-host entrypoints: MC_REPO_PATH
    env var wins, falls back to the interactive-checkout default."""
    txt = ENTRYPOINT.read_text()
    assert 'REPO_ROOT="${MC_REPO_PATH:-$HOME/Workspace/Projects/mission-control}"' in txt
    assert 'PATCH_SCRIPT="$REPO_ROOT/scripts/hermes-config-patch.py"' in txt
    # The old hardcoded form must be gone, not just supplemented.
    assert '${HOME}/Workspace/Projects/mission-control/scripts/hermes-config-patch.py' not in txt


def test_patch_invocation_prefers_backend_venv_python_with_fallback():
    """The patcher needs PyYAML/ruamel.yaml. Plain `python3` resolved through
    launchd's PATH is not guaranteed to be an interpreter that has them (and
    a failure there used to be silently swallowed — see
    hermes-bridge.py:ENTRYPOINT_LOG). Prefer the backend venv's python3,
    the same interpreter the patcher's own mcp_servers.mc.command entry
    already relies on; fall back to plain python3 if that venv is missing."""
    txt = ENTRYPOINT.read_text()
    assert 'PATCH_PYTHON="$REPO_ROOT/backend/.venv/bin/python3"' in txt
    fallback_idx = txt.index('if [ ! -x "$PATCH_PYTHON" ]')
    assert 'PATCH_PYTHON="python3"' in txt[fallback_idx:fallback_idx + 200]
    invoke_idx = txt.index('"$PATCH_PYTHON" "$PATCH_SCRIPT"')
    assert invoke_idx > fallback_idx, "must resolve the interpreter before invoking it"


def test_missing_patch_script_logs_loudly_instead_of_silently_skipping():
    """A missing hermes-config-patch.py (e.g. MC_REPO_PATH pointing nowhere)
    must produce a visible WARN, not just fall through the `if -f` guard."""
    txt = ENTRYPOINT.read_text()
    if_idx = txt.index('if [ -f "$PATCH_SCRIPT" ]')
    else_block = txt[if_idx: if_idx + 400]
    assert "else" in else_block
    assert "not found" in else_block
    assert "config.yaml NOT synced" in else_block


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
