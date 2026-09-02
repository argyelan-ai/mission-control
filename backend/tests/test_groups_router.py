"""Gruppen-API (Gruppenchat V1, PR A) — CRUD, Mitglieder, Nachrichten, Dokument.

Fehler-Vokabular: 422 = ungültige Eingabe (GroupValidationError),
409 = Mitglied nicht gruppenfähig (comm_v2 fehlt — Spiegel von
input_not_supported im Sessions-Chat), 404 = gibt es nicht.
"""
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent


@pytest.fixture(autouse=True)
def _references_in_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.services.reference_ingest.references_root", lambda: str(tmp_path)
    )
    return tmp_path


async def _make_agent(
    session: AsyncSession, name: str, *, comm_v2: bool = True, archived: bool = False
) -> Agent:
    import datetime as dt

    agent = Agent(
        name=name,
        slug=name.lower(),
        agent_runtime="cli-bridge",
        comm_v2=comm_v2,
        archived_at=dt.datetime.now(tz=dt.timezone.utc) if archived else None,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _create_group(
    auth_client: AsyncClient, member_ids: list[uuid.UUID], **overrides
) -> dict:
    payload = {
        "goal": "DFlash2 vs vLLM entscheiden",
        "member_ids": [str(i) for i in member_ids],
        **overrides,
    }
    resp = await auth_client.post("/api/v1/groups", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_create_and_list_groups(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    body = await _create_group(auth_client, [a.id, b.id], name="Spark-Runde")
    assert body["name"] == "Spark-Runde"
    assert body["goal"] == "DFlash2 vs vLLM entscheiden"
    assert body["status"] == "idle"
    assert body["max_rounds"] == 3
    member_slugs = {m["slug"] for m in body["members"]}
    assert member_slugs == {"alpha", "beta"}

    listing = await auth_client.get("/api/v1/groups")
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 1
    assert rows[0]["member_count"] == 2
    assert rows[0]["status"] == "idle"


@pytest.mark.asyncio
async def test_list_carries_preview_and_avatars_for_the_sidebar(
    auth_client: AsyncClient, async_session
):
    """Die Sidebar-Zeile zeigt Avatare + Vorschau der letzten Nachricht —
    beides kommt aus DIESEM Aufruf, nicht aus N Zusatz-Requests."""
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    await auth_client.post(
        f"/api/v1/groups/{body['id']}/messages", json={"text": "@beta erste Frage"}
    )

    row = (await auth_client.get("/api/v1/groups")).json()[0]
    assert row["member_count"] == 2
    assert {m["name"] for m in row["member_avatars"]} == {"Alpha", "Beta"}
    assert row["last_message"]["body"] == "@beta erste Frage"
    # Absender-Label bewusst neutral: MC liegt öffentlich, jede Installation
    # hat ihren eigenen Operator — ein Klarname im API-Feld wäre der Name des
    # Entwicklers in fremden Oberflächen.
    assert row["last_message"]["sender"] == "Operator"


@pytest.mark.asyncio
async def test_stream_endpoint_exists_and_404s_for_unknown_group(
    auth_client: AsyncClient, async_session
):
    """Der SSE-Endpoint hängt am Gruppen-Kanal; eine unbekannte Gruppe wird
    abgewiesen, statt einen leeren Strom zu öffnen (nie raten)."""
    from app.routers.groups import router as groups_router

    paths = {getattr(r, "path", "") for r in groups_router.routes}
    assert "/api/v1/groups/{group_id}/stream" in paths

    resp = await auth_client.get(f"/api/v1/groups/{uuid.uuid4()}/stream")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_group_validation_errors(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")

    resp = await auth_client.post(
        "/api/v1/groups",
        json={"goal": "   ", "member_ids": [str(a.id), str(b.id)]},
    )
    assert resp.status_code == 422

    resp = await auth_client.post(
        "/api/v1/groups", json={"goal": "Ziel", "member_ids": [str(a.id)]}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_group_rejects_non_comm_v2_with_409(
    auth_client: AsyncClient, async_session
):
    a = await _make_agent(async_session, "Alpha")
    legacy = await _make_agent(async_session, "Legacy", comm_v2=False)

    resp = await auth_client.post(
        "/api/v1/groups",
        json={"goal": "Ziel", "member_ids": [str(a.id), str(legacy.id)]},
    )
    assert resp.status_code == 409
    assert "Legacy" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_group_detail_and_document(
    auth_client: AsyncClient, async_session, _references_in_tmp: Path
):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    detail = await auth_client.get(f"/api/v1/groups/{gid}")
    assert detail.status_code == 200
    d = detail.json()
    assert d["goal"] == "DFlash2 vs vLLM entscheiden"
    assert d["result_doc_rel_path"].startswith("groups/")
    assert {m["slug"] for m in d["members"]} == {"alpha", "beta"}
    lead = next(m for m in d["members"] if m["role"] == "lead")
    assert lead["slug"] == "alpha"

    doc = await auth_client.get(f"/api/v1/groups/{gid}/document")
    assert doc.status_code == 200
    assert "DFlash2 vs vLLM entscheiden" in doc.json()["content"]

    missing = await auth_client.get(f"/api/v1/groups/{uuid.uuid4()}")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_member_add_and_remove(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    c = await _make_agent(async_session, "Gamma")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    gid = body["id"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/members", json={"agent_id": str(c.id)}
    )
    assert resp.status_code == 201

    resp = await auth_client.delete(f"/api/v1/groups/{gid}/members/{c.id}")
    assert resp.status_code == 204

    # Lead nicht entfernbar → 422 (erst neuen Lead wählen)
    resp = await auth_client.delete(f"/api/v1/groups/{gid}/members/{a.id}")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_and_read_messages(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    gid = body["id"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/messages", json={"text": "@beta bitte prüfen"}
    )
    assert resp.status_code == 201
    assert resp.json()["mentions"] == ["beta"]

    resp = await auth_client.post(
        f"/api/v1/groups/{gid}/messages", json={"text": "und weiter"}
    )
    assert resp.status_code == 201
    assert resp.json()["mentions"] == ["alpha"]  # keine Mention → Lead

    listing = await auth_client.get(f"/api/v1/groups/{gid}/messages")
    assert listing.status_code == 200
    msgs = listing.json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2]
    assert msgs[0]["body"] == "@beta bitte prüfen"
    assert msgs[0]["sender_type"] == "user"


@pytest.mark.asyncio
async def test_start_pause_stop_endpoints(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    resp = await auth_client.post(f"/api/v1/groups/{gid}/start")
    assert resp.status_code == 200
    assert resp.json()["status"] == "running"

    resp = await auth_client.post(f"/api/v1/groups/{gid}/pause")
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"

    resp = await auth_client.post(f"/api/v1/groups/{gid}/stop")
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"  # one_shot

    # Aus done gibt es kein Zurück über /start → 422
    resp = await auth_client.post(f"/api/v1/groups/{gid}/start")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rounds_endpoint_lists_reports(auth_client: AsyncClient, async_session):
    from app.models.group import AgentGroup, GroupRound

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    group = await async_session.get(AgentGroup, uuid.UUID(gid))
    async_session.add(GroupRound(
        group_id=group.id, round_no=1, outcome="continue",
        report="**Outcome:** continue", doc_snapshot="# Doc v1",
        cost_usd=0.42,
    ))
    await async_session.commit()

    resp = await auth_client.get(f"/api/v1/groups/{gid}/rounds")
    assert resp.status_code == 200
    rounds = resp.json()["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["round_no"] == 1
    assert rounds[0]["outcome"] == "continue"
    assert rounds[0]["has_doc_snapshot"] is True
    assert rounds[0]["cost_usd"] == 0.42
    assert "doc_snapshot" not in rounds[0]  # Volltext nur via /document?version=

    doc = await auth_client.get(f"/api/v1/groups/{gid}/document?version=1")
    assert doc.status_code == 200
    assert doc.json()["content"] == "# Doc v1"
    assert doc.json()["version"] == 1


@pytest.mark.asyncio
async def test_group_gate_resolves_via_approvals_endpoint(
    auth_client: AsyncClient, async_session
):
    """PATCH /approvals/{id} mit status=approved muss ein group_gate an die
    Gruppe durchreichen (Zwilling des loop_gate-Hooks)."""
    from app.models.approval import Approval
    from app.models.group import AgentGroup

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    group = await async_session.get(AgentGroup, uuid.UUID(gid))
    group.status = "waiting_gate"
    async_session.add(group)
    approval = Approval(
        board_id=None,
        action_type="group_gate",
        description="Gruppe wartet",
        payload={"group_id": gid, "reason": "ask_operator", "question": "weiter?"},
    )
    async_session.add(approval)
    await async_session.commit()
    await async_session.refresh(approval)

    resp = await auth_client.patch(
        f"/api/v1/approvals/{approval.id}", json={"status": "approved"}
    )
    assert resp.status_code == 200, resp.text
    await async_session.refresh(group)
    assert group.status == "running"


@pytest.mark.asyncio
async def test_eligible_members_only_comm_v2_unarchived(
    auth_client: AsyncClient, async_session
):
    await _make_agent(async_session, "Alpha")
    await _make_agent(async_session, "Legacy", comm_v2=False)
    await _make_agent(async_session, "Alt", archived=True)

    resp = await auth_client.get("/api/v1/groups/eligible-members")
    assert resp.status_code == 200
    slugs = {a["slug"] for a in resp.json()}
    assert slugs == {"alpha"}


@pytest.mark.asyncio
async def test_lead_can_be_switched_to_another_member(
    auth_client: AsyncClient, async_session
):
    """Fällt der Lead aus (hängendes CLI, archiviert), muss die Gruppe einen
    neuen bekommen — sonst steckt sie für immer fest: der alte Lead ist nicht
    entfernbar, und niemand sonst darf urteilen. Live-Befund vom 21.08.2026."""
    from app.models.group import AgentGroup, GroupMember

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))
    gid = body["id"]

    resp = await auth_client.patch(
        f"/api/v1/groups/{gid}", json={"lead_agent_id": str(b.id)}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["lead_agent_id"] == str(b.id)

    roles = {m["slug"]: m["role"] for m in resp.json()["members"]}
    assert roles == {"alpha": "member", "beta": "lead"}

    group = await async_session.get(AgentGroup, uuid.UUID(gid))
    await async_session.refresh(group)
    assert str(group.lead_agent_id) == str(b.id)

    # Der neue Lead ist jetzt der Unentfernbare, der alte darf gehen.
    assert (await auth_client.delete(f"/api/v1/groups/{gid}/members/{b.id}")).status_code == 422
    assert (await auth_client.delete(f"/api/v1/groups/{gid}/members/{a.id}")).status_code == 204


@pytest.mark.asyncio
async def test_lead_switch_rejects_a_non_member(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    outsider = await _make_agent(async_session, "Gamma")
    body = await _create_group(auth_client, [a.id, b.id], lead_agent_id=str(a.id))

    resp = await auth_client.patch(
        f"/api/v1/groups/{body['id']}", json={"lead_agent_id": str(outsider.id)}
    )
    assert resp.status_code == 422
    assert "Mitglied" in resp.json()["detail"]


# ── Abschluss: archivieren, löschen, ins Memory übernehmen ─────────────────
# Operator-Wunsch 22.08.2026: „ich sollte in der Lage sein selber zu bestimmen
# ob ich es mit den Daten oder ohne Daten löschen/archivieren möchte, nur den
# Chat. Diese Ergebnisse sollten auch in unserem Memorysystem aufgenommen
# werden, embedded, wie die Tasks."
#
# Drei Stufen, weil sie drei verschiedene Dinge bedeuten — und die
# Memory-Übernahme ist von allen dreien UNABHÄNGIG: eine Erkenntnis darf ein
# gelöschtes Arbeitsmaterial überleben.


@pytest.mark.asyncio
async def test_archive_hides_the_group_but_keeps_everything(
    auth_client: AsyncClient, async_session
):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])

    resp = await auth_client.post(f"/api/v1/groups/{group['id']}/archive")
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived_at"] is not None

    # Aus der Liste raus …
    listed = (await auth_client.get("/api/v1/groups")).json()
    assert all(g["id"] != group["id"] for g in listed)
    # … aber weiterhin lesbar, und auf Wunsch wieder in der Liste.
    assert (await auth_client.get(f"/api/v1/groups/{group['id']}")).status_code == 200
    with_archived = (await auth_client.get("/api/v1/groups?include_archived=true")).json()
    assert any(g["id"] == group["id"] for g in with_archived)
    # Die Liste muss Archiviertes MARKIEREN, nicht nur mitliefern — sonst kann
    # die Archiv-Sektion im Frontend aktive und archivierte Zeilen nicht
    # auseinanderhalten und müsste zwei Listen gegeneinander diffen.
    ours = next(g for g in with_archived if g["id"] == group["id"])
    assert ours["archived_at"] is not None

    resp = await auth_client.post(f"/api/v1/groups/{group['id']}/unarchive")
    assert resp.status_code == 200
    assert resp.json()["archived_at"] is None


@pytest.mark.asyncio
async def test_delete_chat_only_keeps_the_result_document(
    auth_client: AsyncClient, async_session, tmp_path
):
    """„Nur den Chat löschen" — der Verlauf war Arbeitsmaterial, das Ergebnis
    zählt. Genau diese Trennung muss die Sabotage-Probe halten."""
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])

    await auth_client.post(
        f"/api/v1/groups/{group['id']}/messages", json={"text": "Ein Zwischenruf."}
    )
    before = (await auth_client.get(f"/api/v1/groups/{group['id']}/messages")).json()
    assert before["messages"], "Vorbedingung: es gibt Nachrichten"

    resp = await auth_client.delete(f"/api/v1/groups/{group['id']}?scope=chat")
    assert resp.status_code == 200, resp.text

    # Gruppe und Dokument bleiben, der Verlauf ist leer.
    detail = (await auth_client.get(f"/api/v1/groups/{group['id']}")).json()
    assert detail["archived_at"] is not None
    doc = (await auth_client.get(f"/api/v1/groups/{group['id']}/document")).json()
    assert doc["content"], "Das Ergebnis-Dokument darf NICHT mitgelöscht werden"
    after = (await auth_client.get(f"/api/v1/groups/{group['id']}/messages")).json()
    assert after["messages"] == []


@pytest.mark.asyncio
async def test_delete_all_removes_the_group_entirely(auth_client: AsyncClient, async_session):
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])

    resp = await auth_client.delete(f"/api/v1/groups/{group['id']}?scope=all")
    assert resp.status_code == 204, resp.text
    assert (await auth_client.get(f"/api/v1/groups/{group['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_running_group_is_not_deleted_by_accident(auth_client: AsyncClient, async_session):
    """Eine laufende Gruppe zu löschen würde Agenten mitten im Turn den Thread
    unter den Füssen wegziehen. Erst anhalten, dann löschen."""
    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])
    await auth_client.post(f"/api/v1/groups/{group['id']}/start")

    resp = await auth_client.delete(f"/api/v1/groups/{group['id']}?scope=all")
    assert resp.status_code == 409
    assert (await auth_client.get(f"/api/v1/groups/{group['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_memorize_writes_the_result_into_the_vault(auth_client: AsyncClient, async_session):
    """Das Ergebnis wandert als BoardMemory ins Gedächtnis — dieselbe Ablage,
    die auch Tasks nutzen, damit die Einbettung greift."""
    from sqlmodel import select

    from app.models.memory import BoardMemory

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])

    resp = await auth_client.post(
        f"/api/v1/groups/{group['id']}/memorize",
        json={"title": "Spark-Standard", "memory_type": "research", "tags": ["gruppe"]},
    )
    assert resp.status_code == 201, resp.text
    memory_id = resp.json()["memory_id"]

    rows = (await async_session.exec(select(BoardMemory))).all()
    saved = next(m for m in rows if str(m.id) == memory_id)
    assert saved.memory_type == "research"
    assert "gruppe" in saved.tags
    assert saved.content, "Der Dokument-Inhalt muss mitgehen, nicht nur der Titel"


@pytest.mark.asyncio
async def test_memory_survives_deleting_everything(auth_client: AsyncClient, async_session):
    """Der Kern des Operator-Wunsches: die Erkenntnis überlebt das
    Arbeitsmaterial. Wer 'alles löschen' wählt, verliert den Memory-Eintrag
    NICHT."""
    from sqlmodel import select

    from app.models.memory import BoardMemory

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    group = await _create_group(auth_client, [a.id, b.id])
    await auth_client.post(f"/api/v1/groups/{group['id']}/memorize", json={})

    await auth_client.delete(f"/api/v1/groups/{group['id']}?scope=all")

    rows = (await async_session.exec(select(BoardMemory))).all()
    assert rows, "Der Memory-Eintrag darf beim Löschen der Gruppe nicht verschwinden"


@pytest.mark.asyncio
async def test_preview_sources_are_only_live_members_with_a_transcript(
    async_session, monkeypatch
):
    """Gruppen-Vorschau (Live-Schicht A im Gruppenraum): Quellen sind die
    Mitglieder, die eine Live-Sitzung mit Transkript haben. Archivierte
    Mitglieder und Host-Agenten ohne Transkript fallen still weg — der
    Gruppen-Stream darf daran nie scheitern."""
    from pathlib import Path

    from fastapi.responses import JSONResponse

    from app.routers import groups as groups_router
    from app.services import group_service

    live = await _make_agent(async_session, "Live")
    silent = await _make_agent(async_session, "Silent")
    gone = await _make_agent(async_session, "Gone")
    group = await group_service.create_group(
        async_session, goal="Vorschau", member_ids=[live.id, silent.id, gone.id]
    )
    import datetime as dt

    gone.archived_at = dt.datetime.now(tz=dt.timezone.utc)
    async_session.add(gone)
    await async_session.commit()

    async def _fake_resolve(agent_id, session):
        if agent_id == live.id:
            return (live, Path("/tmp/live.jsonl"), None)
        return JSONResponse(status_code=404, content={"reason": "no_transcript"})

    monkeypatch.setattr(groups_router, "_resolve_transcript_path", _fake_resolve)

    sources = await groups_router._group_preview_sources(async_session, group)
    assert [(a.id, p) for a, p in sources] == [(live.id, Path("/tmp/live.jsonl"))]


@pytest.mark.asyncio
async def test_rounds_endpoint_recounts_late_usage(auth_client: AsyncClient, async_session):
    """Nachzuegler-Event (Harvester lief erst nach Rundenschluss) muss beim
    Lesen der Runden mitgezaehlt werden — sonst bleibt eine 1-Runden-Gruppe
    ewig auf tokens_used=0 (live 03.09.)."""
    import datetime as dt

    from app.models.group import AgentGroup, GroupRound
    from app.models.model_usage import ModelUsageEvent

    a = await _make_agent(async_session, "Alpha")
    b = await _make_agent(async_session, "Beta")
    body = await _create_group(auth_client, [a.id, b.id])
    gid = body["id"]

    group = await async_session.get(AgentGroup, uuid.UUID(gid))
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    group.started_at = t0
    async_session.add(group)
    async_session.add(GroupRound(
        group_id=group.id, round_no=1, outcome="done", report="x",
        started_at=t0 + dt.timedelta(seconds=1),
        finished_at=t0 + dt.timedelta(minutes=5),
        tokens_used=0, cost_usd=0.0,
    ))
    async_session.add(ModelUsageEvent(
        agent_id=b.id, harness="cli-bridge", model="m", session_id="s",
        message_uuid=str(uuid.uuid4()), input_tokens=40, output_tokens=2,
        cost_usd=0.0, ts=t0 + dt.timedelta(minutes=2), source_file="t.jsonl",
    ))
    await async_session.commit()

    resp = await auth_client.get(f"/api/v1/groups/{gid}/rounds")
    assert resp.status_code == 200
    assert resp.json()["rounds"][0]["tokens_used"] == 42
