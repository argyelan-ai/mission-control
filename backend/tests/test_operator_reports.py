"""OperatorReports adapter — the seam that makes the report channel swappable.

Pins the three properties the Telegram decommission will depend on:

1. **Fan-out with containment** — every configured backend gets the report;
   one backend down neither eats the report nor leaks an exception into the
   task flow.
2. **Config at call time** — a reports channel set after process start is
   seen (telegram_reports.py froze its token at import; that bug class must
   not return here).
3. **The gate stays channel-aware** — NULL/"telegram"/"report" gate, Discord
   stays exempt (the counter-case test_report_back_gate.py:672 relies on).
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.services.operator_reports import (
    ReportResult,
    SlackReportsBackend,
    TelegramReportsBackend,
    _chunks,
    _html_report_to_mrkdwn,
    is_operator_report_channel,
    report_backends,
    send_report,
)


# ── Gate semantics ────────────────────────────────────────────────────────

@pytest.mark.parametrize("channel", [None, "", "telegram", "report", "operator"])
def test_gate_applies_for_operator_channels(channel):
    assert is_operator_report_channel(channel) is True


def test_discord_stays_exempt_from_the_gate():
    """The one behaviour the rename must not change: a Discord-routed task
    never needed `mc report` before `done`, and still does not."""
    assert is_operator_report_channel("discord") is False


# ── Config at call time ───────────────────────────────────────────────────

def test_slack_backend_reads_the_channel_at_call_time(monkeypatch):
    backend = SlackReportsBackend()
    monkeypatch.setattr(settings, "slack_reports_channel", "", raising=False)
    assert backend.configured is False
    # Set AFTER the backend object exists — must be seen (no import-time freeze).
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)
    assert backend.configured is True


def test_no_backend_configured_means_no_delivery(monkeypatch):
    monkeypatch.setattr(settings, "slack_reports_channel", "", raising=False)
    with patch(
        "app.services.telegram_reports.telegram_reports"
    ) as tg:
        tg.configured = False
        assert report_backends() == []


# ── Fan-out with containment ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_backend_down_does_not_eat_the_report(monkeypatch):
    """Telegram throws, Slack delivers -> delivered=True. The operator got
    his report; a partial failure is a log line, not a 503."""
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)
    with patch(
        "app.services.telegram_reports.telegram_reports"
    ) as tg, patch.object(
        SlackReportsBackend, "send_text", new_callable=AsyncMock,
        return_value=ReportResult("slack", True),
    ):
        tg.configured = True
        tg.send = AsyncMock(side_effect=RuntimeError("boom"))
        delivered, results = await send_report("hallo")
    assert delivered is True
    verdicts = {r.backend: r.ok for r in results}
    assert verdicts == {"telegram": False, "slack": True}


@pytest.mark.asyncio
async def test_all_backends_down_reports_failure(monkeypatch):
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)
    with patch(
        "app.services.telegram_reports.telegram_reports"
    ) as tg, patch.object(
        SlackReportsBackend, "send_text", new_callable=AsyncMock,
        return_value=ReportResult("slack", False, "nope"),
    ):
        tg.configured = True
        tg.send = AsyncMock(return_value={"ok": False, "description": "down"})
        delivered, results = await send_report("hallo")
    assert delivered is False
    assert all(not r.ok for r in results)


# ── Slack backend mechanics ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slack_backend_posts_into_the_resolved_reports_channel(monkeypatch):
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)
    posted = []

    class _Result:
        ok = True
        error = None

    async def _post(self, *, channel, text, **kw):
        posted.append((channel, text))
        return _Result()

    with patch(
        "app.services.slack_client.resolve_channel_id", new_callable=AsyncMock,
        return_value="C0REPORTS",
    ), patch("app.services.slack_client.SlackTransport.post_message", _post):
        result = await SlackReportsBackend().send_text("<b>fertig</b> — Details im PR")

    assert result.ok is True
    assert posted == [("C0REPORTS", "*fertig* — Details im PR")]


@pytest.mark.asyncio
async def test_unresolvable_reports_channel_fails_softly(monkeypatch):
    monkeypatch.setattr(settings, "slack_reports_channel", "#does-not-exist", raising=False)
    with patch(
        "app.services.slack_client.resolve_channel_id", new_callable=AsyncMock,
        return_value=None,
    ):
        result = await SlackReportsBackend().send_text("hallo")
    assert result.ok is False
    assert "does-not-exist" in (result.detail or "")


# ── Formatting helpers ────────────────────────────────────────────────────

def test_telegram_html_becomes_slack_mrkdwn():
    assert _html_report_to_mrkdwn(
        '<b>Status</b> — <i>done</i>, <code>mc report</code>, '
        '<a href="https://x.test/pr/1">PR 1</a>'
    ) == "*Status* — _done_, `mc report`, <https://x.test/pr/1|PR 1>"


def test_long_reports_chunk_on_line_boundaries():
    text = "\n".join(f"Zeile {i} " + "x" * 80 for i in range(80))
    chunks = _chunks(text, 3900)
    assert len(chunks) > 1
    assert "".join(c + "\n" for c in chunks).strip() == text.strip() or (
        # rejoining with newlines must lose nothing but the split newlines
        sum(len(c) for c in chunks) >= len(text) - len(chunks)
    )
    assert all(len(c) <= 3900 for c in chunks)


# ── File fan-out ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_file_report_fans_out_to_both_backends(monkeypatch, tmp_path):
    """`mc report --photo/--file` reaches Telegram AND #mc-reports — the
    whole point of phase 2. The Slack leg rides the two-stage upload."""
    f = tmp_path / "shot.png"; f.write_bytes(b"png")
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)
    uploaded = []

    async def _upload(**kw):
        uploaded.append(kw)
        from app.services.slack_client import SlackUploadResult
        return SlackUploadResult(ok=True, file_id="F1")

    with patch("app.services.telegram_reports.telegram_reports") as tg, patch(
        "app.services.slack_client.resolve_channel_id", new_callable=AsyncMock,
        return_value="C0REPORTS",
    ), patch("app.services.slack_client.upload_file", _upload):
        tg.configured = True
        tg.send_photo = AsyncMock(return_value={"ok": True, "result": {"message_id": 7}})
        delivered, results = await send_report(
            "<b>Screenshot</b>", file_path=str(f), as_photo=True
        )

    assert delivered is True
    assert {r.backend: r.ok for r in results} == {"telegram": True, "slack": True}
    tg.send_photo.assert_awaited_once()
    assert uploaded[0]["channel"] == "C0REPORTS"
    assert uploaded[0]["initial_comment"] == "*Screenshot*"


@pytest.mark.asyncio
async def test_uploads_disabled_is_semantic_not_retryable(monkeypatch, tmp_path):
    f = tmp_path / "doc.pdf"; f.write_bytes(b"pdf")
    monkeypatch.setattr(settings, "slack_reports_channel", "#mc-reports", raising=False)

    async def _upload(**kw):
        from app.services.slack_client import SlackUploadResult
        return SlackUploadResult(ok=False, code="file_uploads_disabled", error="disabled")

    with patch("app.services.telegram_reports.telegram_reports") as tg, patch(
        "app.services.slack_client.resolve_channel_id", new_callable=AsyncMock,
        return_value="C0REPORTS",
    ), patch("app.services.slack_client.upload_file", _upload):
        tg.configured = False
        delivered, results = await send_report("x", file_path=str(f), as_photo=False)

    assert delivered is False
    assert results[0].retryable is False
