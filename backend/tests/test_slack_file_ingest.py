"""The operator drops a file in Slack — MC takes it as a reference (§3D).

Before this feature a shared PDF got a "kann ich hier noch nicht annehmen"
reply. Now it becomes a ReferenceFile: in a task thread it belongs to the
task, top-level in the chat it belongs to the routed agent (usually Boss —
the new ``agent_id`` owner, migration 0172). The agent reads it straight off
the shared ~/.mc mount; the absolute path in the thread message is the
delivery.

These tests pin the ownership decision, the safety order (allowlist and size
cap BEFORE any download), and that both halves of "caption + file" survive.
All Slack downloads are faked — no test talks to the network.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.reference_file import ReferenceFile
from app.models.task import Task
from app.models.thread import Message, Thread
from app.services.slack_inbound import ingest_slack_event
from tests.conftest import test_engine


@pytest.fixture
def refs_root(tmp_path, monkeypatch):
    """Alle mc_home()-Aufrufe auf ein Temp-Verzeichnis umbiegen."""
    from app.config import settings
    monkeypatch.setattr(settings, "home_host", str(tmp_path))
    return tmp_path / ".mc" / "references"


def _file_event(*, text="", thread_ts=None, files=None):
    """A ``file_share`` with one PDF, as Socket Mode delivers it."""
    event = {
        "type": "message",
        "subtype": "file_share",
        "user": "U0MARK",
        "channel": "C0TEAM",
        "ts": "1753900000.000200",
        "text": text,
        "files": files if files is not None else [
            {
                "id": "F0PDF",
                "name": "spez.pdf",
                "mimetype": "application/pdf",
                "size": 1234,
                "url_private_download": "https://files.slack.com/files-pri/T0-F0PDF/download/spez.pdf",
            }
        ],
    }
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    return event


class _Adapter:
    """Records replies; resolves rooms like the real Slack adapter."""

    def __init__(self):
        self.sent = []

    async def send(self, room, message):
        self.sent.append((room, message.body))
        return True

    async def resolve_thread_for_room(self, session, room):
        return (
            await session.exec(
                select(Thread).where(Thread.slack_thread_ts == str(room))
            )
        ).one_or_none()


async def _boss(session: AsyncSession) -> Agent:
    from app.auth import generate_agent_token

    _raw, token_hash = generate_agent_token()
    agent = Agent(
        name="Boss",
        slug="boss",
        agent_runtime="host",
        agent_token_hash=token_hash,
        comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _task_thread(session: AsyncSession, ts="1753900000.000100"):
    board = Board(
        id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}",
        auto_dispatch_enabled=False,
    )
    session.add(board)
    await session.commit()
    task = Task(board_id=board.id, title="Landingpage bauen", status="in_progress")
    session.add(task)
    await session.commit()
    await session.refresh(task)
    thread = Thread(kind="task", task_id=task.id, slack_thread_ts=ts)
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    return task, thread


def _channel_ours():
    return patch(
        "app.services.slack_inbound.channel_is_ours", new_callable=AsyncMock,
        return_value=True,
    )


def _download_returns(data):
    return patch(
        "app.services.slack_files.download_slack_file", new_callable=AsyncMock,
        return_value=data,
    )


# ── Ownership: where does the file belong? ────────────────────────────────


@pytest.mark.asyncio
async def test_a_pdf_in_the_general_chat_becomes_a_boss_reference(
    async_session, refs_root
):
    boss = await _boss(async_session)
    adapter = _Adapter()

    with _channel_ours(), _download_returns(b"%PDF-1.4 echte bytes"):
        await ingest_slack_event(
            _file_event(), adapter=adapter, session=async_session
        )

    refs = list((await async_session.exec(select(ReferenceFile))).all())
    assert len(refs) == 1
    ref = refs[0]
    assert ref.agent_id == boss.id, "top-level file belongs to the routed agent"
    assert ref.task_id is None and ref.project_id is None
    assert ref.rel_path.startswith(f"agent/{boss.id}/")
    assert (refs_root / ref.rel_path).is_file(), "bytes must be on disk"
    assert ref.uploaded_by == "slack"

    assert adapter.sent, "the operator gets a confirmation where he posted"
    assert "spez.pdf" in adapter.sent[0][1] and "Boss" in adapter.sent[0][1]

    msgs = list((await async_session.exec(select(Message))).all())
    assert len(msgs) == 1, "the agent hears about the file via its thread"
    assert "📎" in msgs[0].body
    assert str(refs_root / ref.rel_path) in msgs[0].body, (
        "the absolute path is the delivery — the agent reads the mount"
    )


@pytest.mark.asyncio
async def test_a_pdf_in_a_task_thread_attaches_to_the_task(
    async_session, refs_root
):
    await _boss(async_session)
    task, thread = await _task_thread(async_session)
    adapter = _Adapter()

    with _channel_ours(), _download_returns(b"%PDF-1.4 echte bytes"):
        await ingest_slack_event(
            _file_event(thread_ts=thread.slack_thread_ts),
            adapter=adapter, session=async_session,
        )

    refs = list((await async_session.exec(select(ReferenceFile))).all())
    assert len(refs) == 1
    assert refs[0].task_id == task.id, "a file in a task thread belongs to the task"
    assert refs[0].board_id == task.board_id
    assert refs[0].agent_id is None

    assert adapter.sent and "Landingpage bauen" in adapter.sent[0][1]

    msgs = list((await async_session.exec(select(Message))).all())
    assert len(msgs) == 1 and msgs[0].thread_id == thread.id


# ── Safety order: refuse BEFORE fetching bytes ────────────────────────────


@pytest.mark.asyncio
async def test_disallowed_mime_is_refused_without_download(async_session, refs_root):
    """text/html is Stored XSS in the browsable files root (Review-Fund M1) —
    and a refused type must cost zero download bytes."""
    await _boss(async_session)
    adapter = _Adapter()
    event = _file_event(files=[{
        "id": "F0EVIL", "name": "seite.html", "mimetype": "text/html",
        "size": 10, "url_private_download": "https://files.slack.com/x",
    }])

    with _channel_ours(), _download_returns(b"<html>") as dl:
        await ingest_slack_event(event, adapter=adapter, session=async_session)

    dl.assert_not_awaited(), "allowlist runs BEFORE the download"
    assert list((await async_session.exec(select(ReferenceFile))).all()) == []
    assert adapter.sent and "seite.html" in adapter.sent[0][1]
    assert "⚠️" in adapter.sent[0][1]


@pytest.mark.asyncio
async def test_declared_oversize_is_refused_without_download(
    async_session, refs_root
):
    """Slack declares the size in the event — an oversized file is refused
    before any HTTP request, not after 25 MB of buffering."""
    await _boss(async_session)
    adapter = _Adapter()
    event = _file_event(files=[{
        "id": "F0BIG", "name": "riesig.zip", "mimetype": "application/zip",
        "size": 500 * 1024 * 1024,
        "url_private_download": "https://files.slack.com/x",
    }])

    with _channel_ours(), _download_returns(b"zip") as dl:
        await ingest_slack_event(event, adapter=adapter, session=async_session)

    dl.assert_not_awaited()
    assert list((await async_session.exec(select(ReferenceFile))).all()) == []
    assert adapter.sent and "riesig.zip" in adapter.sent[0][1]


# ── Caption + file: both halves survive, in ONE message ───────────────────


@pytest.mark.asyncio
async def test_caption_and_file_land_as_one_message(async_session, refs_root):
    await _boss(async_session)
    adapter = _Adapter()

    with _channel_ours(), _download_returns(b"%PDF-1.4 echte bytes"):
        await ingest_slack_event(
            _file_event(text="Kapitel 3 ist relevant"),
            adapter=adapter, session=async_session,
        )

    msgs = list((await async_session.exec(select(Message))).all())
    assert len(msgs) == 1, "caption and file note belong to the same utterance"
    assert "Kapitel 3 ist relevant" in msgs[0].body
    assert "📎" in msgs[0].body and "spez.pdf" in msgs[0].body


@pytest.mark.asyncio
async def test_one_bad_file_never_blocks_its_siblings(async_session, refs_root):
    await _boss(async_session)
    adapter = _Adapter()
    event = _file_event(files=[
        {"id": "F1", "name": "boese.html", "mimetype": "text/html", "size": 5,
         "url_private_download": "https://files.slack.com/1"},
        {"id": "F2", "name": "gut.pdf", "mimetype": "application/pdf", "size": 20,
         "url_private_download": "https://files.slack.com/2"},
    ])

    with _channel_ours(), _download_returns(b"%PDF-1.4 ok"):
        await ingest_slack_event(event, adapter=adapter, session=async_session)

    refs = list((await async_session.exec(select(ReferenceFile))).all())
    assert [r.original_name for r in refs] == ["gut.pdf"]
    reply = adapter.sent[0][1]
    assert "gut.pdf" in reply and "boese.html" in reply
