"""entrypoint.sh must run the config patcher AFTER sourcing agent.env (ADR-060)."""
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
