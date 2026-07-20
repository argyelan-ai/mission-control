import pytest

from app.models import Thread, Message, AgentThreadCursor


@pytest.mark.asyncio
async def test_thread_message_roundtrip(session):
    t = Thread(kind="task")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    m = Message(
        thread_id=t.id, seq=1, sender_type="system",
        message_type="system", body="Briefing",
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)
    assert m.seq == 1 and m.thread_id == t.id


@pytest.mark.asyncio
async def test_seq_unique_per_thread(session):
    t = Thread(kind="task")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    session.add(Message(
        thread_id=t.id, seq=1, sender_type="system",
        message_type="system", body="a",
    ))
    await session.commit()
    session.add(Message(
        thread_id=t.id, seq=1, sender_type="system",
        message_type="system", body="b",
    ))
    with pytest.raises(Exception):
        await session.commit()


@pytest.mark.asyncio
async def test_cursor_defaults(session):
    t = Thread(kind="task")
    session.add(t)
    await session.commit()
    await session.refresh(t)
    import uuid
    c = AgentThreadCursor(agent_id=uuid.uuid4(), thread_id=t.id)
    session.add(c)
    await session.commit()
    assert c.last_delivered_seq == 0 and c.last_acked_seq == 0
