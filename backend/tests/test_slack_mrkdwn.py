"""Agents write Markdown; Slack speaks mrkdwn. The adapter translates.

Live finding by the operator (2026-07-30): Boss's replies arrived in Slack
with literal `**asterisks**` around every heading — Slack's dialect uses
single `*bold*`, `_italic_` and `<url|label>` links, and renders standard
Markdown as plain text. The agents are not wrong: MC's whole comment culture
is Markdown (`**Update** — …`), and Telegram renders its own dialect too.
So the translation is the SLACK ADAPTER's job (ADR-072: channel quirks live
in the channel module), never the agents'.

Code is the sacred exception: anything inside backticks or fences must come
through untouched — a transformed code sample is worse than an ugly one.
"""
import pytest

from app.services.chat_slack import markdown_to_mrkdwn


# ── The exact things the operator saw break ───────────────────────────────


def test_bold_double_asterisks_become_single():
    assert markdown_to_mrkdwn("**Update** — alles grün") == "*Update* — alles grün"


def test_bold_double_underscores_become_single_asterisk():
    assert markdown_to_mrkdwn("__wichtig__") == "*wichtig*"


def test_headings_become_bold_lines():
    assert markdown_to_mrkdwn("## Was wurde gemacht\nText") == "*Was wurde gemacht*\nText"


def test_links_become_slack_form():
    assert (
        markdown_to_mrkdwn("siehe [PR #204](https://github.com/x/y/pull/204)")
        == "siehe <https://github.com/x/y/pull/204|PR #204>"
    )


def test_strikethrough_tildes_halve():
    assert markdown_to_mrkdwn("~~verworfen~~") == "~verworfen~"


def test_italic_stays_italic():
    """Single-asterisk italic must survive the bold conversion running first."""
    assert markdown_to_mrkdwn("*leise* und **laut**") == "_leise_ und *laut*"


# ── Code is untouchable ───────────────────────────────────────────────────


def test_inline_code_is_never_transformed():
    text = "nutze `mc msg \"**text**\"` dafür"
    assert markdown_to_mrkdwn(text) == text


def test_fenced_code_is_never_transformed():
    text = "```python\n# **kein** mrkdwn hier\nx = [a](b)\n```"
    assert markdown_to_mrkdwn(text) == text


def test_prose_around_a_fence_is_still_transformed():
    got = markdown_to_mrkdwn("**vorher**\n```\n**code**\n```\n**nachher**")
    assert got == "*vorher*\n```\n**code**\n```\n*nachher*"


# ── Plain text passes through, exactly ────────────────────────────────────


def test_plain_text_is_untouched():
    text = "Alles klar, ja. Kein laufender Task bei mir — sag an."
    assert markdown_to_mrkdwn(text) == text


def test_empty_and_none_are_safe():
    assert markdown_to_mrkdwn("") == ""
    assert markdown_to_mrkdwn(None) == ""


# ── The adapter applies it on send ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_translates_the_body():
    from app.services.chat_adapter import OutboundChatMessage
    from app.services.chat_slack import SlackChatAdapter

    sent = {}

    class _Transport:
        async def post_message(self, **kw):
            sent.update(kw)

            class R:
                ok = True
                ts = "1.2"
                error = None

            return R()

    class _Faces:
        async def face_for(self, sender):
            return ":dart:"

    adapter = SlackChatAdapter(transport=_Transport(), faces=_Faces())
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        SlackChatAdapter, "_channel", return_value="#team"
    ):
        await adapter.send(None, OutboundChatMessage(body="**Update** — fertig"))

    assert sent["text"] == "*Update* — fertig", (
        "the adapter must translate Markdown to mrkdwn on its way out — "
        "this is the literal-asterisks bug the operator saw in Slack"
    )
