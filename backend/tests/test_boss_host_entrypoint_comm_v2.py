"""Boss-Host entrypoint runs the SHARED poll.sh in comm_v2 nudge mode (2026-07-27).

Boss used to carry its own docker/boss-host/poll.sh copy that predated comm_v2
(no new_messages / msg_gate / MSG_DELIVERY_MODE) — the backend delivered thread
messages that Boss's script silently dropped. Boss now runs the shared
docker/shared/poll.sh (same Nudge+Pull path as the Docker fleet and kimi-host),
with the host specifics injected purely via env overrides in entrypoint.sh.

These are static guards on that entrypoint so a future edit can't silently
regress Boss back off comm_v2 or break the tmux-env-inheritance contract.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "docker" / "boss-host" / "entrypoint.sh"
SHARED_POLL = REPO / "docker" / "shared" / "poll.sh"


def test_entrypoint_runs_the_shared_poll_sh():
    txt = ENTRYPOINT.read_text()
    assert "docker/shared/poll.sh" in txt, "Boss must run the shared poll.sh"
    # No revived boss-local poll.sh copy.
    assert not (REPO / "docker" / "boss-host" / "poll.sh").exists(), (
        "docker/boss-host/poll.sh must stay gone — Boss uses the shared copy"
    )


def test_entrypoint_sets_comm_v2_host_overrides():
    txt = ENTRYPOINT.read_text()
    # SESSION_NAME in poll.sh derives from AGENT_NAME; the tmux session is
    # 'boss-host' while agent.env sets AGENT_NAME=boss → must be overridden.
    assert 'export AGENT_NAME="boss-host"' in txt
    # Native Anthropic claude needs the bracketed-paste end-marker (Bug 14).
    assert 'export PANE_UI_OVERRIDE="claude"' in txt
    # Fleet-standard delivery.
    assert "MSG_DELIVERY_MODE" in txt and "nudge" in txt
    # Host paths (no /home/agent on macOS) + repo libs.
    for var in (
        "TASK_LOCK_FILE",
        "TURN_SIGNAL_FILE",
        "MSG_QUEUE_DIR",
        "MSG_ACK_DIR",
        "NUDGE_STATE_FILE",
        "TASK_PROMPT_FILE",
        "COMMENTS_PROMPT_FILE",
        "POLL_LIB_DIR",
    ):
        assert f"export {var}=" in txt, f"entrypoint must override {var} for the host"


def test_overrides_land_after_env_source_and_before_tmux():
    """agent.env is sourced first (AGENT_NAME=boss); the overrides must come
    AFTER it (so boss-host wins) and BEFORE the tmux server starts (tmux windows
    inherit the server's env, not the client's — hermes lesson, ADR-068)."""
    txt = ENTRYPOINT.read_text()
    src_idx = txt.index('. "$ENV_FILE"')
    override_idx = txt.index('export AGENT_NAME="boss-host"')
    start_idx = txt.index("new-session")
    assert src_idx < override_idx < start_idx


def test_entrypoint_kills_server_not_just_session():
    """A tmux server surviving from a previous boot caches the OLD global env and
    would hand it to new windows, defeating the overrides. kill-server forces a
    fresh server that inherits the exported comm_v2 env."""
    txt = ENTRYPOINT.read_text()
    assert "kill-server" in txt
    kill_idx = txt.index("kill-server")
    start_idx = txt.index("new-session")
    assert kill_idx < start_idx


def test_shared_poll_sh_is_comm_v2():
    """Sanity: the shared poll.sh Boss now points at actually is comm_v2."""
    txt = SHARED_POLL.read_text()
    for marker in ("new_messages", "MSG_DELIVERY_MODE"):
        assert marker in txt
