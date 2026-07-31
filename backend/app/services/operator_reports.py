"""Operator reports — channel-neutral delivery of "the task is done" messages.

The report-back contract (SOUL: a final report before ``mc done``) was wired
straight into the Telegram reports bot. This module is the seam that makes the
*channel* an implementation detail, exactly like chat_adapter (ADR-072) did
for the team chat: callsites talk to :func:`send_report`, and whichever
backends are configured deliver — Telegram today, Slack's ``#mc-reports``
alongside it, Telegram alone gone in the decommission phase without any
callsite noticing.

Design rules, learned the hard way:

* **Config is read at call time.** ``telegram_reports`` froze its token at
  import (telegram_reports.py:34) — a token set via the UI after boot was
  invisible until restart. The Slack backend resolves token and channel per
  send.
* **Fan-out, not failover.** During the parallel-run phase every configured
  backend gets the report; delivery counts as success if at least one
  backend accepted. That is what makes the Telegram decommission a pure
  config change instead of a code change.
* **Never throw into the task flow.** A report failure must surface to the
  agent (it rolls back the gate claim), but a *partial* failure — one of two
  backends down — must not: the operator got his report.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("mc.operator_reports")

# Slack chunk ceiling: Slack rejects ~4000+ chars with msg_too_long. Reports
# are capped at 4000 by the endpoint (Telegram limit), so a single split
# point is enough headroom; chunking stays here in the backend, not in
# callsites.
_SLACK_CHUNK = 3900


@dataclass
class ReportResult:
    """One backend's verdict.

    ``retryable`` distinguishes the two failure classes the old endpoint
    already told apart: a transport problem (timeout, network — try again,
    HTTP 503) versus a semantic rejection (Telegram "can't parse entities",
    Slack ``msg_too_long`` — the AGENT must fix the content, HTTP 422, and
    the description must reach it verbatim for self-correction).
    ``message_id`` preserves the Telegram message id the old response
    carried.
    """

    backend: str
    ok: bool
    detail: str | None = None
    retryable: bool = True
    message_id: int | None = None


class TelegramReportsBackend:
    """Wraps the existing singleton — behaviour byte-identical to before."""

    name = "telegram"

    @property
    def configured(self) -> bool:
        from app.services.telegram_reports import telegram_reports

        return telegram_reports.configured

    async def send_text(self, text: str) -> ReportResult:
        from app.services.telegram_reports import telegram_reports

        result = await telegram_reports.send(text)
        if result is None:
            return ReportResult(self.name, False, "Telegram-Send fehlgeschlagen", retryable=True)
        if not result.get("ok"):
            # A Telegram API rejection ("can't parse entities", …) is the
            # agent's content, not the wire — non-retryable, description
            # passes through verbatim so the agent can fix its HTML.
            detail = result.get("description", "Telegram API Fehler")
            return ReportResult(self.name, False, str(detail), retryable=False)
        return ReportResult(
            self.name, True,
            message_id=result.get("result", {}).get("message_id"),
        )


class SlackReportsBackend:
    """Posts into the reports channel via the existing transport.

    Deliberately NOT routed through the ADR-072 chat adapter: its room
    contract is one thread_ts inside the *default* channel and its ``send()``
    is hard-wired there (chat_slack.py) — a second channel does not fit that
    contract, and bending it would break the team chat's invariants. Direct
    transport + ``resolve_channel_id`` (cached, 600s) is the honest shape.

    Text only for now — file attachments arrive with ``files.uploadV2``
    in the next phase; ``send_file`` then lands here and nowhere else.
    """

    name = "slack"

    @property
    def configured(self) -> bool:
        from app.config import settings

        return bool((getattr(settings, "slack_reports_channel", "") or "").strip())

    async def send_text(self, text: str) -> ReportResult:
        from app.config import settings
        from app.services.slack_client import SlackTransport, resolve_channel_id

        channel = await resolve_channel_id(settings.slack_reports_channel)
        if not channel:
            return ReportResult(
                self.name,
                False,
                f"Reports-Kanal {settings.slack_reports_channel!r} nicht auflösbar",
            )
        transport = SlackTransport()
        # Telegram reports are HTML (<b>/<i>/<code>); Slack renders mrkdwn.
        # The shared converter from the team chat keeps the two channels
        # showing the same report.
        body = _html_report_to_mrkdwn(text)
        for chunk in _chunks(body, _SLACK_CHUNK):
            result = await transport.post_message(channel=channel, text=chunk)
            if not result.ok:
                # Slack error codes (msg_too_long, invalid_blocks, …) are
                # semantic; pure transport problems surface as exceptions
                # above and stay retryable via send_report's containment.
                return ReportResult(
                    self.name, False,
                    result.error or "Slack-Send fehlgeschlagen",
                    retryable=False,
                )
        return ReportResult(self.name, True)


def _chunks(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    rest = text
    while rest:
        cut = rest.rfind("\n", 0, size) if len(rest) > size else len(rest)
        if cut <= 0:
            cut = min(size, len(rest))
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return out


def _html_report_to_mrkdwn(text: str) -> str:
    """Telegram-HTML -> Slack mrkdwn, minimal and honest.

    Only the four tags the endpoint documents (<b>, <i>, <code>, <a>). No
    HTML parser: reports are agent-written snippets, and a regex over four
    known tags cannot be confused the way a parser can be abused.
    """
    import html
    import re

    s = re.sub(r"</?b>", "*", text)
    s = re.sub(r"</?i>", "_", s)
    s = re.sub(r"</?code>", "`", s)
    s = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"<\1|\2>", s)
    return html.unescape(s)


def is_operator_report_channel(channel: str | None) -> bool:
    """Does the report-back gate apply for this ``report_back_channel`` value?

    NULL means "the active reports adapter" (the fleet default); "telegram"
    is the historical spelling of the same thing; "report"/"operator" are the
    neutral spellings. Anything else — today that is "discord" — is an
    explicit different delivery route and stays exempt from the `mc done`
    gate, exactly as before the adapter existed (the Discord counter-case in
    test_report_back_gate.py pins this).
    """
    return (channel or "operator") in ("operator", "report", "telegram")


def report_backends() -> list[TelegramReportsBackend | SlackReportsBackend]:
    """All *configured* backends, resolved at call time."""
    return [b for b in (TelegramReportsBackend(), SlackReportsBackend()) if b.configured]


async def send_report(text: str) -> tuple[bool, list[ReportResult]]:
    """Deliver ``text`` to every configured backend.

    Returns ``(delivered, results)`` — ``delivered`` is True when at least
    one backend accepted. Backend exceptions are contained per backend: one
    channel being down must not eat the report on the other.
    """
    backends = report_backends()
    if not backends:
        return False, []
    results: list[ReportResult] = []
    for backend in backends:
        try:
            results.append(await backend.send_text(text))
        except Exception as exc:  # noqa: BLE001 — contained by design
            logger.warning("Report-Backend %s warf %s: %s", backend.name, type(exc).__name__, exc)
            results.append(ReportResult(backend.name, False, f"{type(exc).__name__}: {exc}"))
    delivered = any(r.ok for r in results)
    for r in results:
        if not r.ok:
            logger.warning("Report via %s fehlgeschlagen: %s", r.backend, r.detail)
    return delivered, results
