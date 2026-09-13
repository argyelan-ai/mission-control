"""No ghost TUI under OMP_DRIVER=acp (fix omp-acp-no-tui-window, 13.09.2026).

Live incident (13.09.2026): an ACP-driven agent still had
`entrypoint.sh:start_native()` launch the native omp TUI in tmux Window 0,
even though ACP agents run both tasks (`bridge.py run_acp_once`) and chat
(`acp_chat.py`, Window 3) over ACP. The operator saw the idle TUI as a second "ghost"
session in the Terminal view.

Fix: Window 0 only launches the native TUI when `OMP_DRIVER` is unset/native.
Under `acp` it prints a static `OMP_ACP_READY` banner and drops to a plain
shell instead — the window itself stays reachable (Boss: keep the Terminal
view until the live proof, docs/specs/chat-over-acp.md), it just shows no
launcher. `omp-recycler.sh`'s idle-TUI-relaunch responsibility is disabled
under the same flag (native `tui_alive` is always false for the banner shell,
so it would otherwise stomp the banner every `RECYCLER_IDLE_INTERVAL`). The
health gate additionally accepts the `OMP_ACP_READY` sentinel so switching an
agent onto ACP doesn't fail readiness (native TUI never prints that string,
so this is purely additive).

Static source-text guards, same idiom as test_hermes_entrypoint_patches.py /
test_boss_host_entrypoint_comm_v2.py — the shell scripts here have no
container to run tests against in CI.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "docker" / "omp-bridge" / "entrypoint.sh"
RECYCLER = REPO / "docker" / "omp-bridge" / "omp-recycler.sh"

NATIVE_LAUNCH_CMD = 'tmux send-keys -t "$SESSION":0 "exec ${LAUNCHER} ${OMP_DEFAULT_CWD}" C-m'


def test_native_launch_line_is_byte_identical():
    """Sabotage guard: the native launch command itself must be untouched —
    only reachability (behind an `if`) may change. A native (OMP_DRIVER
    unset) agent's Window 0 must still be byte-identical to before this fix.
    """
    txt = ENTRYPOINT.read_text()
    assert NATIVE_LAUNCH_CMD in txt, (
        "native TUI launch command changed — native agents must be unaffected"
    )


def test_window0_branches_on_omp_driver_acp():
    txt = ENTRYPOINT.read_text()
    start_idx = txt.index("start_native() {")
    body = txt[start_idx:txt.index("start_native", start_idx + 1)]
    assert 'if [ "${OMP_DRIVER:-}" = "acp" ]; then' in body
    launch_idx = body.index(NATIVE_LAUNCH_CMD)
    branch_idx = body.index('if [ "${OMP_DRIVER:-}" = "acp" ]; then')
    assert branch_idx < launch_idx, (
        "native launch must sit inside the else branch of the acp check"
    )


def test_acp_branch_prints_sentinel_and_drops_to_shell():
    """Window 0 under acp must print OMP_ACP_READY (the health-gate anchor,
    see OMP_READY_SIGNALS in agent_runtime_switch.py) and end in a plain
    shell — no native launcher, no exit (Ops still wants a live pane)."""
    txt = ENTRYPOINT.read_text()
    acp_idx = txt.index('if [ "${OMP_DRIVER:-}" = "acp" ]; then')
    acp_block = txt[acp_idx: acp_idx + 400]
    assert "OMP_ACP_READY" in acp_block
    assert "exec bash" in acp_block
    assert "LAUNCHER" not in acp_block.split("else")[0], (
        "the acp branch must not invoke the native launcher"
    )


def test_recycler_skips_tui_relaunch_under_acp_driver():
    """omp-recycler.sh must not respawn Window 0 with the native launcher
    when OMP_DRIVER=acp — there is no TUI to relaunch there, only a static
    banner shell, and tui_alive() is always false for it."""
    txt = RECYCLER.read_text()
    assert 'ACP_DRIVER="${OMP_DRIVER:-}"' in txt
    idle_idx = txt.index("elif ! task_active; then")
    guard_idx = txt.index('if [ "$ACP_DRIVER" != "acp" ] && ! tui_alive; then', idle_idx)
    relaunch_call_idx = txt.index("relaunch_tui", guard_idx)
    # relaunch_tui must be reachable only through the ACP_DRIVER guard.
    assert guard_idx < relaunch_call_idx


def test_recycler_still_watches_bridge_rss_under_acp():
    """The RSS-pressure respawn of Window 1 (bridge.py) is independent of
    the TUI and must keep working under acp — only the TUI branch is
    disabled, not the whole idle block."""
    txt = RECYCLER.read_text()
    assert "bridge_rss_mb" in txt
    assert "RSS_LIMIT_MB" in txt


def test_recycler_env_comment_documents_tmux_inheritance():
    """Regression guard for the "does the recycler even see OMP_DRIVER"
    question raised in review — live-verified (tmux 3.6a, 13.09.2026) that
    `new-window`/`respawn-window` inherit `tmux set-environment -g`, so no
    extra plumbing is needed. Pin the reasoning in the script so a future
    edit doesn't reintroduce an unnecessary explicit env passthrough."""
    txt = RECYCLER.read_text()
    assert "tmux set-environment -g" in txt
    assert "verified live" in txt
