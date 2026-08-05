"""Inbound: a Slack message -> its MC thread (ADR-072, Socket Mode).

The transport (`slack_socket`) hands one Slack ``event`` here; this module does
what only SLACK can do and nothing else:

  * decide the message is not our own (loop protection, below),
  * the hard channel gate — the Telegram equivalent of "never answer strangers",
  * unpack Slack's payload (its `<@U…>` / `<#C…>` / `<url|label>` markup),
  * translate ``thread_ts`` into a room and reply back into it.

The routing decision itself (known room -> its thread, unknown room -> ask back
instead of guessing, no room -> the general chat with Boss, ``@name`` ->
that agent) is channel-neutral and lives in ``chat_inbound`` — used, not
rebuilt. Telegram and Slack therefore cannot drift apart on who is meant.

── Loop protection ───────────────────────────────────────────────────────
MC posts into the very channel it reads. Without a hard filter its own message
comes straight back in, gets stored as an operator message, wakes an agent,
which answers into the channel, which comes back in… MC talking to itself.

Slack marks its own bot traffic in several places, and which ones are present
depends on how the message was posted (a `chat.postMessage` with
``username``/``icon_emoji`` — exactly what MC does — arrives with `bot_id` AND
``subtype: "bot_message"`` AND a `bot_profile`, but no `user`). So the check
below is deliberately a set of independent signals rather than one field: any
of them is enough, and a human message trips none of them.

Belt and braces continue downstream: the ingest writes with
``chat_inbound.INBOUND_MESSAGE_KWARGS`` (``sender_type="user"`` +
mirror suppressed), so even a message that slipped through this filter could
not be mirrored back out.
"""
from __future__ import annotations

import logging
import re

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.chat_inbound import INBOUND_MESSAGE_KWARGS, route_inbound

logger = logging.getLogger("mc.slack_inbound")

# `<@U123>` (a real Slack user), `<#C123|name>` (a channel), `<https://x|text>`
# (a link) — Slack's own markup, which a human never typed and MC must not
# store verbatim.
_USER_MENTION = re.compile(r"<@([UWB][A-Z0-9]+)(\|[^>]*)?>")
_CHANNEL_LINK = re.compile(r"<#[CGD][A-Z0-9]*\|([^>]*)>")
_LABELLED_LINK = re.compile(r"<([^>|]+)\|([^>]*)>")
_BARE_LINK = re.compile(r"<((?:https?|mailto):[^>]+)>")


def normalise_slack_text(raw: str | None) -> str:
    """Slack markup -> what the human actually wrote.

    A mention of a REAL Slack user (`<@U123>`) becomes `@U123`: MC's agents are
    not Slack users, so a real mention can only ever be the operator pinging the
    app itself — keeping the id readable is honest and harmless. Everything else
    is unwrapped to its label.
    """
    text = raw or ""
    text = _USER_MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = _CHANNEL_LINK.sub(lambda m: f"#{m.group(1)}", text)
    text = _LABELLED_LINK.sub(lambda m: m.group(2) or m.group(1), text)
    text = _BARE_LINK.sub(lambda m: m.group(1), text)
    return text.strip()


def is_own_message(event: dict) -> bool:
    """Did MC (or any other bot/app) write this? See the module header.

    Five independent signals, any one of which is decisive. A message from a
    human in a channel carries none of them.
    """
    if event.get("bot_id"):
        return True
    if (event.get("subtype") or "") == "bot_message":
        return True
    if event.get("bot_profile"):
        return True
    if event.get("app_id"):
        return True
    # No human author at all — nothing we could attribute to the operator, and
    # every MC post looks exactly like this.
    if not event.get("user"):
        return True
    return False


async def channel_is_ours(channel: str | None) -> bool:
    """Hard gate: only channels MC actively serves are processed.

    The Slack twin of Telegram's chat_id gate. The app may be invited into
    other channels (or receive its own DMs); nothing from there may drive MC.

    Two channels count as ours: the team chat (``slack_default_channel``)
    and the reports channel (``slack_reports_channel``). The reports channel
    is write-mostly, but the operator WILL answer a report right where he
    read it — and a single-valued gate silently discarded exactly such
    messages once before (2026-07-29, the missing_scope incident's sibling).
    An answered report routes like a channel-root message: to Boss.
    """
    from app.config import settings
    from app.services.slack_client import resolve_channel_id

    if not channel:
        return False
    for configured in (
        (getattr(settings, "slack_default_channel", "") or "").strip(),
        (getattr(settings, "slack_reports_channel", "") or "").strip(),
    ):
        if not configured:
            continue
        if configured.lstrip("#") == channel:
            return True
        resolved = await resolve_channel_id(configured)
        if resolved and resolved == channel:
            return True
    return False


def room_for(event: dict) -> str | None:
    """Which MC room is this? A Slack thread reply -> its ``thread_ts``; a
    message in the channel itself -> None (= the general chat).

    Slack sets ``thread_ts == ts`` on the message that STARTS a thread; that
    one is still a channel-level message, so it must resolve to None too.
    """
    thread_ts = event.get("thread_ts")
    if not thread_ts or thread_ts == event.get("ts"):
        return None
    return str(thread_ts)


async def ingest_slack_event(event: dict, *, adapter=None, session=None) -> None:
    """Process ONE Slack ``message`` event.

    Opens its own session when the caller has none (the socket loop has no
    transaction). ``adapter``/``session`` are injectable for tests.

    Errors are deliberately NOT swallowed here: the socket service isolates
    them per message (and logs a traceback) after it has already acknowledged
    the envelope, so a failure costs one message, never the connection and
    never a redelivery loop.
    """
    if session is not None:
        await _ingest(session, event, adapter)
        return

    from app.database import engine

    async with AsyncSession(engine, expire_on_commit=False) as own:
        await _ingest(own, event, adapter)


async def _ingest(session: AsyncSession, event: dict, adapter) -> None:
    if event.get("type") != "message":
        # `app_mention` arrives in addition to `message.channels` for the same
        # message — processing both would double every mention.
        return
    if is_own_message(event):
        logger.debug("slack inbound: own/bot message ignored")
        return

    # The channel gate runs BEFORE any subtype handling on purpose: a voice
    # message triggers a file download, and MC must not fetch bytes on behalf
    # of channels it does not serve.
    channel = event.get("channel")
    if not await channel_is_ours(channel):
        logger.warning("slack inbound from unconfigured channel %s — ignored", channel)
        return

    if adapter is None:
        from app.services.chat_slack import SlackChatAdapter

        adapter = SlackChatAdapter()

    subtype = event.get("subtype")
    voice_note = None
    pending_files: list[dict] = []
    if subtype == "file_share":
        from app.services import slack_file_ingest, slack_voice

        # Audio goes to the voice path (STT), everything else to the file
        # ingest (References, ADR-053) — one shared audio definition, so no
        # file is ever claimed by both branches or by neither.
        pending_files = slack_file_ingest.non_audio_files(event)
        if slack_voice.pick_audio_file(event) is not None:
            voice_note = await _transcribe_voice(event, adapter)
            if voice_note is None and not pending_files:
                # Transcription failed and said so in the channel; with no
                # other files there is nothing left to route.
                return
    elif subtype:
        # Edits, deletions, joins, thread broadcasts of an edit …
        # A plain message from a human has no subtype at all.
        logger.debug("slack inbound: subtype %s ignored", subtype)
        return

    caption = normalise_slack_text(event.get("text"))
    if voice_note is not None:
        # A caption typed alongside the clip belongs to the same utterance.
        text = f"{caption}\n{voice_note}".strip() if caption else voice_note
    else:
        text = caption

    if not text and not pending_files:
        logger.info("slack inbound: message without usable content — ignored")
        return

    room = room_for(event)
    # Thread-anchor fix (2026-08-05): a channel-root message carries its own
    # ``ts`` as the anchor — it opens an MC conversation whose replies arrive
    # as Slack thread replies UNDER the operator's message, instead of landing
    # in the one Boss DM thread that can only speak to the channel root.
    anchor = event.get("ts") if room is None else None
    route = await route_inbound(session, adapter, room, text=text, anchor=anchor)
    if route.thread is None and room is not None:
        # A reply inside a Slack thread MC cannot map — the operator answering
        # under a message MC posted top-level (a report, a notice). 2026-07-31
        # this dropped a real order after an ask-back ("ladet bitte supergirl
        # herunter"). The operator talking under OUR channel is never
        # "unknown": the parent ts becomes the anchor of a fresh conversation
        # with Boss, so the answer arrives in exactly this Slack thread — no
        # ask-back, no detour over the channel root.
        route = await route_inbound(session, adapter, None, text=text, anchor=room)
    if route.thread is None:
        # The neutral path already worded the reason (unknown room / no Boss);
        # deliver it where the operator is looking. Shared files are NOT taken
        # without a destination — the notice explains the situation.
        await _reply(adapter, room, route.notice or "")
        return

    file_note = ""
    if pending_files:
        from app.services.reference_ingest import serialize_reference
        from app.services.slack_file_ingest import ingest_event_files

        outcome = await ingest_event_files(session, route.thread, pending_files)
        if outcome.stored:
            # The agent reads the file straight off the shared ~/.mc mount —
            # the absolute path in the thread message IS the delivery.
            file_note = "\n".join(
                f"📎 Referenz-Datei: {serialize_reference(r)['abs_path']} "
                f"({r.original_name}, {_fmt_size(r.size)})"
                for r in outcome.stored
            )
        reply_lines = []
        if outcome.stored:
            names = ", ".join(r.original_name for r in outcome.stored)
            reply_lines.append(f"📎 {names} — angehängt an {outcome.owner_label}.")
        reply_lines.extend(f"⚠️ {reason}" for reason in outcome.rejected)
        await _reply(adapter, room, "\n".join(reply_lines))

    body = f"{text}\n\n{file_note}".strip() if file_note else text
    if not body:
        # Every file was rejected and no caption was typed — the reply above
        # already said so; there is nothing to put on the thread.
        return

    from app.services.messaging import post_message

    await post_message(
        session,
        thread_id=route.thread.id,
        body=body,
        mentions=route.mentions,
        **INBOUND_MESSAGE_KWARGS,  # sender_type=user + loop protection
    )
    logger.info(
        "inbound Slack -> thread %s (room=%s, addressed=%s, files=%d)",
        route.thread.id,
        room or "channel",
        route.addressed_agent.name if route.addressed_agent else "-",
        len(pending_files),
    )


async def _transcribe_voice(event: dict, adapter) -> str | None:
    """Voice clip -> transcript, or None after everything reasonable was tried.

    Two distinct "no" cases, deliberately handled differently:
      * no audio file in the share (a PDF, an image) -> silent None, exactly
        the old ignore behaviour;
      * an audio file that could not be transcribed -> tell the operator IN
        THE CHANNEL. He watches Slack, not the backend log — a voice message
        that silently vanishes looks like being ignored, which is precisely
        the failure mode this channel exists to end.
    """
    from app.services import slack_voice

    if slack_voice.pick_audio_file(event) is None:
        logger.debug("slack inbound: file_share without audio ignored")
        return None

    transcript = await slack_voice.transcribe_event_audio(event)
    if transcript:
        return transcript

    await _reply(
        adapter,
        room_for(event),
        "🎤 Deine Sprachnachricht ist angekommen, aber ich konnte sie nicht "
        "transkribieren. Bitte einmal als Text senden.",
    )
    return None


def _fmt_size(size: int) -> str:
    """1234567 -> "1.2 MB" — for the operator, not for parsing."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size} B"


async def _reply(adapter, room, text: str) -> None:
    """Answer in the same place the operator wrote. Never raises."""
    from app.services.chat_adapter import OutboundChatMessage

    if not text:
        return
    try:
        await adapter.send(room, OutboundChatMessage(body=text))
    except Exception as e:  # noqa: BLE001 — a failed reply must never throw
        logger.warning("slack inbound reply failed: %s", e)
