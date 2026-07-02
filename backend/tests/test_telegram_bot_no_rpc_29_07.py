"""Phase 29-07 Task 3: telegram_bot.py removes RPC unblock path (D-10)
but keeps direct HTTPS path intact (D-14).
"""
from __future__ import annotations

import pathlib


def test_telegram_bot_has_no_rpc_imports() -> None:
    """telegram_bot.py must not import openclaw_rpc anywhere."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "telegram_bot.py"
    ).read_text(encoding="utf-8")

    assert "openclaw_rpc" not in src, "telegram_bot still imports openclaw_rpc"
    # Ban rpc.<method> tokens (excluding any genuine doc-string "rpc" refs)
    bad_calls = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "rpc." in line and "openclaw_rpc" not in line:
            bad_calls.append(f"{line_no}: {line.strip()}")
    assert not bad_calls, "rpc.* calls remain:\n" + "\n".join(bad_calls)


def test_telegram_bot_direct_api_path_intact() -> None:
    """Per D-14: send_message + send_approval_telegram MUST still exist."""
    from app.services.telegram_bot import telegram_bot

    assert callable(telegram_bot.send_message)
    assert callable(telegram_bot.send_approval_telegram)
    assert callable(telegram_bot.edit_message_text)
    assert callable(telegram_bot.answer_callback_query)


def test_telegram_bot_uses_taskcomment_for_unblock() -> None:
    """_resolve_approval must import TaskComment, not rpc.chat_send."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "telegram_bot.py"
    ).read_text(encoding="utf-8")

    assert "TaskComment" in src, "telegram_bot must use TaskComment for unblock notification"
    # Original unblock notify content should be preserved (resolution comment)
    assert "UNBLOCKED" in src
