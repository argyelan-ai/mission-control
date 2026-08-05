"""Slack voice messages reach Boss as text.

The operator records a native Slack voice clip instead of typing — on a phone
that is the natural way to talk to the team. Slack delivers it as a ``message``
event with ``subtype: "file_share"`` and an audio file. Before this feature the
inbound path dropped EVERY subtype in one line, so a voice message vanished
without a trace: no thread entry, no reply, nothing for the operator to see.

The transcription itself is not new: Telegram voice notes already run through
the shared jarvis_core STT chain. These tests pin three things — the event is
recognised and transcribed, the shared chain is the one used (no second STT
wiring), and failure modes speak up in the channel instead of staying silent.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.thread import Message
from app.services.slack_inbound import ingest_slack_event
from app.services.slack_voice import pick_audio_file, slack_transcript_fallback
from tests.conftest import test_engine


def _voice_event(**over):
    """A native Slack voice clip, as Socket Mode delivers it."""
    event = {
        "type": "message",
        "subtype": "file_share",
        "user": "U0MARK",
        "channel": "C0TEAM",
        "ts": "1753900000.000100",
        "text": "",
        "files": [
            {
                "id": "F0AUDIO",
                "mimetype": "audio/mp4",
                "subtype": "slack_audio",
                "url_private_download": "https://files.slack.com/files-pri/T0-F0AUDIO/download/audio.mp4",
                "transcription": {
                    "status": "complete",
                    "preview": {"content": "Slack-eigene Vorschau", "has_more": False},
                },
            }
        ],
    }
    event.update(over)
    return event


class _Adapter:
    """Records replies; never talks to Slack."""

    def __init__(self):
        self.sent = []

    async def send(self, room, message):
        self.sent.append((room, message.body))
        return True

    async def resolve_thread_for_room(self, session, room):
        return None

    async def bind_room(self, session, thread, room):
        # No anchoring in this stub: the ingest then falls back to the DM
        # route, which is exactly the pre-anchor behaviour these tests pin.
        return False


async def _boss(session: AsyncSession) -> Agent:
    from app.auth import generate_agent_token

    _raw, token_hash = generate_agent_token()
    agent = Agent(
        name="Boss",
        slug="boss",  # chat_inbound resolves the general chat via this slug
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


def _channel_ours():
    return patch(
        "app.services.slack_inbound.channel_is_ours", new_callable=AsyncMock,
        return_value=True,
    )


# ── The happy path: voice in, text in the thread ──────────────────────────


@pytest.mark.asyncio
async def test_a_voice_clip_lands_as_its_transcript(async_session):
    await _boss(async_session)
    adapter = _Adapter()

    with _channel_ours(), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
        return_value="Hallo Boss, bitte prüfe die Pipeline.",
    ) as stt:
        await ingest_slack_event(_voice_event(), adapter=adapter, session=async_session)

    stt.assert_awaited_once()
    msgs = list(
        (await async_session.exec(select(Message))).all()
    )
    assert len(msgs) == 1, "the voice message must be stored, exactly once"
    assert msgs[0].body == "Hallo Boss, bitte prüfe die Pipeline."
    assert msgs[0].sender_type == "user", "a transcript is the operator speaking"


@pytest.mark.asyncio
async def test_a_caption_travels_with_the_transcript(async_session):
    """Slack allows text alongside a file — both halves belong to Boss."""
    await _boss(async_session)
    adapter = _Adapter()

    with _channel_ours(), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
        return_value="der gesprochene Teil",
    ):
        await ingest_slack_event(
            _voice_event(text="@boss wichtig:"), adapter=adapter, session=async_session
        )

    msgs = list((await async_session.exec(select(Message))).all())
    assert len(msgs) == 1
    assert "@boss wichtig:" in msgs[0].body
    assert "der gesprochene Teil" in msgs[0].body


# ── Failure modes must be loud in the channel, not silent in a log ────────


@pytest.mark.asyncio
async def test_failed_transcription_tells_the_operator(async_session):
    """A voice message that cannot be transcribed must not just vanish —
    the operator watches the channel, not the backend log."""
    await _boss(async_session)
    adapter = _Adapter()

    with _channel_ours(), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
        return_value=None,
    ):
        await ingest_slack_event(_voice_event(), adapter=adapter, session=async_session)

    msgs = list((await async_session.exec(select(Message))).all())
    assert msgs == [], "no transcript -> nothing to store"
    assert adapter.sent, "the operator must be told in the channel"
    assert "Sprachnachricht" in adapter.sent[0][1]


@pytest.mark.asyncio
async def test_a_failed_non_audio_share_is_announced_not_silently_dropped(async_session):
    """A shared PDF whose download fails must be SAID. The file ingest itself
    (the success path) lives in test_slack_file_ingest.py — here the event has
    no download URL, so the ingest rejects it; the operator sends a document
    and must not, as far as he can tell, be ignored — the exact failure mode
    the voice work already closed for audio."""
    await _boss(async_session)
    adapter = _Adapter()
    event = _voice_event()
    event["files"] = [{"id": "F1", "name": "doku.pdf", "mimetype": "application/pdf"}]

    with _channel_ours(), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
    ) as stt:
        await ingest_slack_event(event, adapter=adapter, session=async_session)

    stt.assert_not_awaited()
    assert list((await async_session.exec(select(Message))).all()) == []
    assert adapter.sent, "the failed ingest must be announced in the channel"
    assert "doku.pdf" in adapter.sent[0][1]
    assert "⚠️" in adapter.sent[0][1]


@pytest.mark.asyncio
async def test_a_caption_beside_a_non_audio_share_survives(async_session):
    """PDF + typed text: before, BOTH were lost — the handler returned before
    the caption was even read (the concept called this the caption bug). Even
    when the file itself cannot be taken (no download URL here), the typed
    words are a message like any other and must reach Boss."""
    await _boss(async_session)
    adapter = _Adapter()
    event = _voice_event(text="schau dir Kapitel 3 an")
    event["files"] = [{"id": "F1", "name": "doku.pdf", "mimetype": "application/pdf"}]

    with _channel_ours(), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
    ) as stt:
        await ingest_slack_event(event, adapter=adapter, session=async_session)

    stt.assert_not_awaited()
    msgs = list((await async_session.exec(select(Message))).all())
    assert [m.body for m in msgs] == ["schau dir Kapitel 3 an"]
    assert adapter.sent and "doku.pdf" in adapter.sent[0][1]


@pytest.mark.asyncio
async def test_foreign_channel_audio_is_never_downloaded(async_session):
    """The channel gate must run BEFORE any file is fetched — MC must not
    download bytes on behalf of channels it does not serve."""
    adapter = _Adapter()

    with patch(
        "app.services.slack_inbound.channel_is_ours", new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "app.services.slack_voice.transcribe_event_audio", new_callable=AsyncMock,
    ) as stt:
        await ingest_slack_event(_voice_event(), adapter=adapter, session=async_session)

    stt.assert_not_awaited()


# ── Unit: picking the audio file and the Slack-preview fallback ───────────


def test_pick_audio_file_finds_the_voice_clip():
    assert pick_audio_file(_voice_event())["id"] == "F0AUDIO"


def test_pick_audio_file_ignores_documents():
    event = _voice_event()
    event["files"] = [{"id": "F1", "mimetype": "application/pdf"}]
    assert pick_audio_file(event) is None


def test_slack_preview_is_the_fallback_not_the_default():
    """Slack's own transcription preview may be truncated (`has_more`) — it is
    a fallback for when our STT chain is unavailable, and it says so."""
    file = _voice_event()["files"][0]
    text = slack_transcript_fallback(file)
    assert "Slack-eigene Vorschau" in text


def test_slack_preview_fallback_flags_truncation():
    file = _voice_event()["files"][0]
    file["transcription"]["preview"]["has_more"] = True
    text = slack_transcript_fallback(file)
    assert "…" in text or "unvollständig" in text
