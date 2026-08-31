"""Agent löschen räumt JEDE Fremdschlüssel-Referenz ab — auch neu dazugekommene.

Live-Befund 31.08.2026: DELETE /agents/{id} endete mit HTTP 500
``ForeignKeyViolationError: model_usage_events_agent_id_fkey``. Die Aufräum-
Logik pflegte zwei handgeschriebene Tabellenlisten; Tabellen, die später per
Migration dazukamen, fehlten darin — still, bis jemand löschen wollte.

Statt die Listen zu erweitern (der Fehler wiederholt sich beim nächsten Mal)
werden die Referenzen jetzt aus dem Schema abgeleitet. Diese Tests decken die
drei Fälle ab, die den Livetest blockierten bzw. Waisen hinterliessen.
"""
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.utils import utcnow


async def _refs(session, table: str, col: str, agent_id: uuid.UUID) -> int:
    """Treffer auf einen Agenten — beide UUID-Schreibweisen (SQLite/Postgres)."""
    rows = await session.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {col} IN (:dashed, :hex)"),
        {"dashed": str(agent_id), "hex": agent_id.hex},
    )
    return rows.scalar()


@pytest.mark.asyncio
async def test_delete_clears_model_usage_events(auth_client, make_agent, session):
    """Der Blocker aus dem Livetest: model_usage_events stand in keiner Liste."""
    from app.models.model_usage import ModelUsageEvent

    agent = await make_agent(name="UsageOwner", agent_runtime="cli-bridge", archived_at=utcnow())
    session.add(
        ModelUsageEvent(
            agent_id=agent.id,
            harness="claude",
            model="sonnet",
            session_id="s1",
            message_uuid=str(uuid.uuid4()),
            ts=utcnow(),
            source_file="/tmp/transcript.jsonl",
        )
    )
    await session.commit()

    assert await _refs(session, "model_usage_events", "agent_id", agent.id) == 1

    with patch("app.services.docker_agent_sync.remove_docker_agent_container", return_value={"ok": "true"}):
        resp = await auth_client.delete(f"/api/v1/agents/{agent.id}")
    assert resp.status_code == 204, resp.text

    assert await _refs(session, "model_usage_events", "agent_id", agent.id) == 0


@pytest.mark.asyncio
async def test_no_table_still_references_the_deleted_agent(auth_client, make_agent, session):
    """Nach dem Löschen zeigt keine Fremdschlüssel-Spalte mehr auf den Agenten.

    Prüft das Schema selbst durch: jede Spalte, die auf agents.id verweist,
    wird nach dem Delete auf verbliebene Treffer abgefragt. Eine künftig
    hinzugefügte Tabelle ist damit automatisch mitgeprüft.
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models.model_usage import ModelUsageEvent
    from app.models.thread import AgentThreadCursor, Thread

    agent = await make_agent(name="SweepOwner", agent_runtime="cli-bridge", archived_at=utcnow())
    agent_id = agent.id

    thread = Thread(kind="task")
    session.add(thread)
    await session.commit()
    await session.refresh(thread)
    session.add(AgentThreadCursor(agent_id=agent_id, thread_id=thread.id))
    session.add(
        ModelUsageEvent(
            agent_id=agent_id,
            harness="claude",
            model="sonnet",
            session_id="s2",
            message_uuid=str(uuid.uuid4()),
            ts=utcnow(),
            source_file="/tmp/transcript.jsonl",
        )
    )
    await session.commit()

    assert await _refs(session, "agent_thread_cursor", "agent_id", agent_id) == 1

    with patch("app.services.docker_agent_sync.remove_docker_agent_container", return_value={"ok": "true"}):
        resp = await auth_client.delete(f"/api/v1/agents/{agent_id}")
    assert resp.status_code == 204, resp.text

    def _fk_columns(sync_session):
        insp = sa_inspect(sync_session.get_bind())
        found = []
        for table in insp.get_table_names():
            for fk in insp.get_foreign_keys(table):
                if fk.get("referred_table") != "agents":
                    continue
                found.extend((table, col) for col in fk.get("constrained_columns", []))
        return found

    for table, col in await session.run_sync(_fk_columns):
        assert await _refs(session, table, col, agent_id) == 0, (
            f"{table}.{col} zeigt noch auf den geloeschten Agenten"
        )
