"""Phase 29-07 Task 2: meeting_service has zero rpc.* calls.

After Gateway sunset, the meeting runner cannot synchronously ask agents via
rpc.chat_send anymore. The legacy ask-and-wait paths are replaced with a
placeholder that records the limitation and logs. Telegram summary delivery
uses telegram_bot.send_message direct HTTPS instead of services/telegram.py
(which is deleted by Plan 29-08).
"""
from __future__ import annotations

import pathlib


def test_meeting_service_has_no_rpc_imports() -> None:
    """meeting_service.py must not import openclaw_rpc or services/telegram."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "meeting_service.py"
    ).read_text(encoding="utf-8")

    assert "openclaw_rpc" not in src, "meeting_service still imports openclaw_rpc"
    assert "from app.services.telegram import" not in src, (
        "meeting_service still imports services/telegram (deleted by Plan 29-08)"
    )
    # Allow telegram_bot (direct HTTPS); ban rpc.<method> tokens
    bad_calls = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "rpc." in line and "openclaw_rpc" not in line:
            bad_calls.append(f"{line_no}: {line.strip()}")
    assert not bad_calls, "rpc.* calls remain:\n" + "\n".join(bad_calls)


def test_meeting_service_imports_cleanly() -> None:
    """Module must import after refactor."""
    import importlib

    import app.services.meeting_service as ms
    importlib.reload(ms)

    # Public API still present
    assert hasattr(ms, "start_meeting")
    assert hasattr(ms, "cancel_meeting")
