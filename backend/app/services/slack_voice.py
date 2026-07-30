"""Slack voice clips -> text, so the operator can talk instead of type.

A native Slack voice message arrives as ``subtype: "file_share"`` with one
audio file. This module does the three Slack-specific steps and nothing else:

  * recognise the audio file in the event (``pick_audio_file``),
  * fetch its bytes — Slack files require the bot token and the ``files:read``
    scope (``download_slack_audio``),
  * turn them into text via the SHARED jarvis_core STT chain
    (``transcribe_event_audio``), falling back to Slack's own transcription
    preview when the chain is unavailable.

The fallback exists because Slack transcribes voice clips itself — but only as
a *preview* that may be truncated (``has_more``). Good enough to not lose the
operator's words entirely; not good enough to be the default, which is why the
shared chain is asked first and the fallback says what it is.

Routing, storage and reply live in ``slack_inbound`` — this module never
touches a session.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("mc.slack_voice")

_DOWNLOAD_TIMEOUT = 30.0
#: Voice clips are seconds long (a few hundred KB). Anything beyond this is
#: not a voice message, whatever its mimetype claims — refuse to buffer it.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


def pick_audio_file(event: dict) -> dict | None:
    """The first audio file in a ``file_share`` event, or None.

    Slack marks native voice clips with ``subtype: "slack_audio"`` on the FILE
    (not the message); an uploaded m4a has only its mimetype. Either counts —
    the operator does not care which button produced the audio.
    """
    for file in event.get("files") or []:
        if not isinstance(file, dict):
            continue
        if file.get("subtype") == "slack_audio":
            return file
        if str(file.get("mimetype") or "").startswith("audio/"):
            return file
    return None


def slack_transcript_fallback(file: dict) -> str | None:
    """Slack's own transcription preview, honestly labelled.

    Only complete transcriptions are used, and a truncated preview says so —
    silently passing a half sentence off as the whole message would put words
    in the operator's mouth by omission.
    """
    transcription = file.get("transcription") or {}
    if transcription.get("status") != "complete":
        return None
    preview = transcription.get("preview") or {}
    content = (preview.get("content") or "").strip()
    if not content:
        return None
    if preview.get("has_more"):
        return f"{content} … [Transkript unvollständig — Slack-Vorschau]"
    return content


async def download_slack_audio(file: dict) -> bytes | None:
    """The clip's bytes, or None with a logged reason. Never raises.

    ``url_private_download`` is served only with the bot token — and only when
    the app holds the ``files:read`` scope. Slack answers a scope problem with
    an HTML login page and status 200, so the content type is checked too:
    HTML here means "no access", not "audio".
    """
    from app.services.slack_client import get_bot_token

    url = file.get("url_private_download") or file.get("url_private")
    if not url:
        logger.warning("slack voice: file %s has no download url", file.get("id"))
        return None

    token = await get_bot_token()
    if not token:
        logger.warning("slack voice: no bot token — cannot download audio")
        return None

    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
            )
    except httpx.HTTPError as exc:
        logger.warning("slack voice: download failed: %s", type(exc).__name__)
        return None

    if resp.status_code != 200:
        logger.warning("slack voice: download HTTP %s", resp.status_code)
        return None
    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type:
        logger.warning(
            "slack voice: got an HTML page instead of audio — the app is "
            "probably missing the files:read scope (re-install after adding it)"
        )
        return None
    if len(resp.content) > _MAX_AUDIO_BYTES:
        logger.warning(
            "slack voice: file %s exceeds %d bytes — not a voice clip, refusing",
            file.get("id"), _MAX_AUDIO_BYTES,
        )
        return None
    return resp.content


async def transcribe_event_audio(event: dict) -> str | None:
    """Event -> transcript, or None when it truly cannot be had.

    Order matters: the shared STT chain first (full transcript), Slack's
    preview second (possibly truncated, but better than losing the message).
    """
    from app.services.voice_transcription import get_voice_transcriber

    file = pick_audio_file(event)
    if file is None:
        return None

    transcriber = get_voice_transcriber()
    if transcriber is not None:
        audio = await download_slack_audio(file)
        if audio:
            # Slack voice clips are mp4/m4a; the filename's extension is what
            # tells the STT endpoint the container format.
            name = file.get("name") or "voice.m4a"
            transcript = (await transcriber(audio, filename=name) or "").strip()
            if transcript:
                return transcript

    fallback = slack_transcript_fallback(file)
    if fallback:
        logger.info("slack voice: using Slack's own transcription preview")
    return fallback
