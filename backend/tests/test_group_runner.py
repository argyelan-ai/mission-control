"""GroupRunnerService — die Runden-Engine des Gruppenchats (PR B, ADR-075).

Moderations-LOGIK = Code (dieser Runner: Briefe bauen, parallel verteilen,
sammeln, zählen, deckeln — ruft NIE ein LLM), Moderations-URTEIL = Lead-Agent
(normaler Teilnehmer-Turn mit Zwangsformat ZIEL ERREICHT | WEITER |
FRAGE AN OPERATOR).

Runden-Ablauf (parallel, Mark-Entscheid 2026-08-20):
Brief an alle Sprecher (Runde 1 blind) → sammeln (Timeout überspringt
Säumige ehrlich) → Lead-Turn (Beiträge als Delta, Dokument-Pflicht,
Verdikt) → Rundenabschluss-Kaskade (Circuit-Breaker → Gate → Ziel →
Fortschritts-Bremse → max_rounds/Dauer → Budget → nächste Runde).
"""
import datetime as dt
import uuid
from pathlib import Path

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.group import AgentGroup, GroupRound
from app.models.thread import Message
from app.services import group_service
from app.services.group_runner import (
    GroupRunnerService,
    apply_group_gate_decision,
    pause_group,
    start_group,
    stop_group,
)
from app.services.messaging import post_message


@pytest.fixture(autouse=True)
def _references_in_tmp(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.services.reference_ingest.references_root", lambda: str(tmp_path)
    )
    return tmp_path


async def _make_agent(session: AsyncSession, name: str) -> Agent:
    agent = Agent(
        name=name, slug=name.lower(), agent_runtime="cli-bridge", comm_v2=True,
    )
    session.add(agent)
    await session.commit()
    await session.refresh(agent)
    return agent


async def _make_running_group(
    session: AsyncSession, *, lifecycle: str = "one_shot", **cfg
):
    """Gruppe alpha(Lead)+beta+gamma, gestartet (status=running)."""
    alpha = await _make_agent(session, "Alpha")
    beta = await _make_agent(session, "Beta")
    gamma = await _make_agent(session, "Gamma")
    group = await group_service.create_group(
        session,
        name="Spark-Runde",
        goal="DFlash2 vs vLLM entscheiden",
        member_ids=[alpha.id, beta.id, gamma.id],
        lead_agent_id=alpha.id,
        lifecycle=lifecycle,
        **cfg,
    )
    group = await start_group(session, group)
    return group, alpha, beta, gamma


async def _tick(session: AsyncSession) -> None:
    await GroupRunnerService().tick(session)


async def _thread_messages(session: AsyncSession, thread_id) -> list[Message]:
    return list(
        (
            await session.exec(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.seq.asc())  # type: ignore[union-attr]
            )
        ).all()
    )


async def _current_round(session: AsyncSession, group) -> GroupRound:
    return (
        await session.exec(
            select(GroupRound)
            .where(GroupRound.group_id == group.id)
            .order_by(GroupRound.created_at.desc())  # type: ignore[union-attr]
        )
    ).first()


async def _agent_says(session: AsyncSession, group, agent: Agent, body: str):
    return await post_message(
        session,
        thread_id=group.thread_id,
        sender_type="agent",
        sender_id=agent.id,
        message_type="message",
        body=body,
        mirror_to_telegram=False,
    )


async def _run_full_round(
    session, group, alpha, beta, gamma, *, lead_verdict: str,
    beta_text: str = "DFlash2 liegt vorn. Quelle: https://example.org/a",
    gamma_text: str = "Einwand: nur 262K Kontext. Quelle: https://example.org/b",
):
    """Eine komplette Runde durchspielen: Brief → Antworten → Lead-Turn."""
    await _tick(session)  # Brief
    await _agent_says(session, group, beta, beta_text)
    await _agent_says(session, group, gamma, gamma_text)
    await _tick(session)  # sammelt + Lead-Prompt
    await _agent_says(session, group, alpha, lead_verdict)
    await _tick(session)  # wertet + Kaskade


# ── Brief / Runde 1 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_round_brief_is_blind_and_mentions_all_speakers(
    async_session: AsyncSession,
):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    assert round_row is not None
    assert round_row.round_no == 1
    assert round_row.brief_seq is not None
    assert round_row.pending_speakers == ["beta", "gamma"]

    msgs = await _thread_messages(async_session, group.thread_id)
    brief = next(m for m in msgs if m.seq == round_row.brief_seq)
    assert brief.sender_type == "system"
    assert sorted(brief.mentions) == ["beta", "gamma"]
    assert "DFlash2 vs vLLM entscheiden" in brief.body       # Ziel
    assert "mc msg --thread" in brief.body                    # Antwort-Anleitung
    assert "Quellen-URL" in brief.body                        # Quellen-Pflicht
    # Runde 1 ist blind: kein Vorrunden-Delta, keine Anti-Lob-Klausel
    assert "Stand der Vorrunde" not in brief.body
    assert "Zustimmung" not in brief.body


@pytest.mark.asyncio
async def test_tick_without_answers_changes_nothing(async_session: AsyncSession):
    """Sammeln ist geduldig: solange Antworten fehlen und kein Timeout
    greift, postet der Tick nichts Neues (idempotent)."""
    group, *_ = await _make_running_group(async_session)
    await _tick(async_session)
    before = len(await _thread_messages(async_session, group.thread_id))
    await _tick(async_session)
    await _tick(async_session)
    assert len(await _thread_messages(async_session, group.thread_id)) == before


# ── Sammeln → Lead-Turn ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_answers_trigger_lead_prompt_with_contributions(
    async_session: AsyncSession,
):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "DFlash2 liegt vorn. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "Einwand: Kontext. Quelle: https://y.org")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    assert round_row.pending_speakers == []
    assert round_row.lead_prompt_seq is not None

    msgs = await _thread_messages(async_session, group.thread_id)
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert lead_prompt.mentions == ["alpha"]
    assert "DFlash2 liegt vorn" in lead_prompt.body     # Beiträge als Delta
    assert "Einwand: Kontext" in lead_prompt.body
    assert "ZIEL ERREICHT" in lead_prompt.body           # Zwangsformat
    assert "WEITER" in lead_prompt.body
    assert "FRAGE AN OPERATOR" in lead_prompt.body
    assert "result.md" in lead_prompt.body               # Dokument-Pflicht


@pytest.mark.asyncio
async def test_speaker_timeout_skips_with_honest_note(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "Beitrag. Quelle: https://x.org")

    round_row = await _current_round(async_session, group)
    round_row.started_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        seconds=group.speaker_timeout_seconds + 60
    )
    async_session.add(round_row)
    await async_session.commit()

    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    assert round_row.pending_speakers == []
    assert round_row.lead_prompt_seq is not None
    msgs = await _thread_messages(async_session, group.thread_id)
    note = next(m for m in msgs if "übersprungen" in m.body)
    assert "gamma" in note.body.lower()


# ── Verdikte ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weiter_verdict_starts_next_round_with_delta_and_antilob(
    async_session: AsyncSession,
):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    # Operator-Einwurf während der Runde — muss im nächsten Brief stehen
    await _tick(async_session)
    await group_service.post_user_message(
        async_session, group, "denkt an den 1M-Kontext!"
    )
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)
    await _agent_says(
        async_session, group, alpha, "WEITER: offen ist der Kontext-Tradeoff"
    )
    await _tick(async_session)

    round1 = (
        await async_session.exec(
            select(GroupRound).where(
                GroupRound.group_id == group.id, GroupRound.round_no == 1
            )
        )
    ).one()
    assert round1.outcome == "continue"
    assert round1.finished_at is not None

    round2 = await _current_round(async_session, group)
    assert round2.round_no == 2
    msgs = await _thread_messages(async_session, group.thread_id)
    brief2 = next(m for m in msgs if m.seq == round2.brief_seq)
    assert "offen ist der Kontext-Tradeoff" in brief2.body  # Lead-Delta
    assert "Zustimmung ist kein Beitrag" in brief2.body      # Anti-Lob
    assert "NICHTS NEUES" in brief2.body
    assert "denkt an den 1M-Kontext!" in brief2.body         # Operator-Einwurf


@pytest.mark.asyncio
async def test_ziel_erreicht_finishes_one_shot_group(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="ZIEL ERREICHT: DFlash2 als Standard. Quellen: https://x.org",
    )
    await async_session.refresh(group)
    assert group.status == "done"
    assert group.finished_at is not None
    round_row = await _current_round(async_session, group)
    assert round_row.outcome == "goal_reached"


@pytest.mark.asyncio
async def test_ziel_erreicht_returns_standing_group_to_idle(
    async_session: AsyncSession,
):
    group, alpha, beta, gamma = await _make_running_group(
        async_session, lifecycle="standing"
    )
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="ZIEL ERREICHT: erledigt. Quelle: https://x.org",
    )
    await async_session.refresh(group)
    assert group.status == "idle"
    assert group.finished_at is None  # Dauergruppe lebt weiter


@pytest.mark.asyncio
async def test_frage_an_mark_waits_at_gate(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="FRAGE AN OPERATOR: 1M-Kontext wichtiger als Speed?",
    )
    await async_session.refresh(group)
    assert group.status == "waiting_gate"
    gate = (
        await async_session.exec(
            select(Approval).where(
                Approval.action_type == "group_gate", Approval.status == "pending"
            )
        )
    ).one()
    assert gate.payload["group_id"] == str(group.id)
    assert "1M-Kontext" in gate.payload["question"]


@pytest.mark.asyncio
async def test_malformed_lead_verdict_fails_round_and_circuit_breaker_pauses(
    async_session: AsyncSession,
):
    """Formwidriges Lead-Urteil = Fehlrunde; 2 in Folge (Default
    pause_on_failed_rounds=2) → paused + group_gate (Circuit-Breaker)."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    for _ in range(2):
        await _run_full_round(
            async_session, group, alpha, beta, gamma,
            lead_verdict="hm, schwierig zu sagen",
        )
    await async_session.refresh(group)
    assert group.status == "paused"
    assert group.consecutive_failed_rounds == 2
    gate = (
        await async_session.exec(
            select(Approval).where(
                Approval.action_type == "group_gate", Approval.status == "pending"
            )
        )
    ).one()
    assert gate.payload["reason"] == "circuit_breaker"


@pytest.mark.asyncio
async def test_lead_timeout_fails_round(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)  # Lead-Prompt raus

    round_row = await _current_round(async_session, group)
    lead_prompt = (
        await async_session.exec(
            select(Message).where(
                Message.thread_id == group.thread_id,
                Message.seq == round_row.lead_prompt_seq,
            )
        )
    ).one()
    lead_prompt.created_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        seconds=group.speaker_timeout_seconds + 60
    )
    async_session.add(lead_prompt)
    await async_session.commit()

    await _tick(async_session)
    round1 = (
        await async_session.exec(
            select(GroupRound).where(
                GroupRound.group_id == group.id, GroupRound.round_no == 1
            )
        )
    ).one()
    assert round1.outcome == "failed"
    assert "Lead" in (round1.report or "")
    # Fehlrunde 1 von 2: der Circuit-Breaker greift noch nicht — die Engine
    # startet ehrlich die nächste Runde.
    await async_session.refresh(group)
    assert group.status == "running"
    assert group.consecutive_failed_rounds == 1


# ── Deckel & Bremsen ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_rounds_is_hard_cap(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=1
    )
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="WEITER: es gäbe noch viel zu tun",
    )
    await async_session.refresh(group)
    assert group.status == "done"  # one_shot: Deckel beendet den Auftrag
    rounds = (
        await async_session.exec(
            select(GroupRound).where(GroupRound.group_id == group.id)
        )
    ).all()
    assert len(rounds) == 1  # GENAU eine Runde — Sabotage-Probe aus dem Plan


@pytest.mark.asyncio
async def test_progress_brake_stops_after_two_stale_rounds(
    async_session: AsyncSession,
):
    """≥ Hälfte der Sprecher meldet NICHTS NEUES, 2 Runden in Folge →
    Auto-Stopp vor dem Deckel («Entscheidung oder Abbruch»)."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=10
    )
    for _ in range(2):
        await _run_full_round(
            async_session, group, alpha, beta, gamma,
            lead_verdict="WEITER: weiter suchen",
            beta_text="NICHTS NEUES",
            gamma_text="NICHTS NEUES",
        )
    await async_session.refresh(group)
    assert group.status == "done"
    rounds = (
        await async_session.exec(
            select(GroupRound).where(GroupRound.group_id == group.id)
        )
    ).all()
    assert len(rounds) == 2


@pytest.mark.asyncio
async def test_budget_pauses_with_gate(async_session: AsyncSession):
    """Budget = weiche Bremse an der Rundengrenze (Zeitfenster × Mitglieder).
    Erschöpft → paused + Gate (Mark kann erhöhen), NICHT still weiter."""
    from app.models.model_usage import ModelUsageEvent

    group, alpha, beta, gamma = await _make_running_group(
        async_session, budget_usd=0.01
    )
    await _tick(async_session)  # Brief → Runde offen, started_at gesetzt
    async_session.add(ModelUsageEvent(
        agent_id=beta.id,
        harness="cli-bridge",
        model="test-model",
        session_id="s1",
        message_uuid=str(uuid.uuid4()),
        input_tokens=1000,
        output_tokens=1000,
        cost_usd=5.0,
        ts=dt.datetime.now(tz=dt.timezone.utc),
        source_file="test.jsonl",
    ))
    await async_session.commit()
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)
    await _agent_says(async_session, group, alpha, "WEITER: mehr Recherche nötig")
    await _tick(async_session)

    await async_session.refresh(group)
    assert group.status == "paused"
    round_row = await _current_round(async_session, group)
    assert (round_row.cost_usd or 0) >= 5.0
    gate = (
        await async_session.exec(
            select(Approval).where(Approval.action_type == "group_gate")
        )
    ).one()
    assert gate.payload["reason"] == "budget_exceeded"


# ── Dokument ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_doc_snapshot_stored_and_unchanged_doc_noted(
    async_session: AsyncSession, _references_in_tmp: Path
):
    group, alpha, beta, gamma = await _make_running_group(async_session)

    # Runde 1: der Lead aktualisiert das Dokument (simuliert per Datei-Write)
    doc_path = _references_in_tmp / group.result_doc_rel_path
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)
    doc_path.write_text(doc_path.read_text() + "\n## Runde 1\nDFlash2 vorn.\n")
    await _agent_says(async_session, group, alpha, "WEITER: Kontext-Frage offen")
    await _tick(async_session)

    round1 = (
        await async_session.exec(
            select(GroupRound).where(
                GroupRound.group_id == group.id, GroupRound.round_no == 1
            )
        )
    ).one()
    assert "Runde 1" in (round1.doc_snapshot or "")
    assert "unverändert" not in (round1.report or "")

    # Runde 2: der Lead fasst das Dokument NICHT an → ehrliche Notiz
    await _agent_says(async_session, group, beta, "C. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "D. Quelle: https://y.org")
    await _tick(async_session)
    await _agent_says(async_session, group, alpha, "WEITER: immer noch offen")
    await _tick(async_session)

    round2 = (
        await async_session.exec(
            select(GroupRound).where(
                GroupRound.group_id == group.id, GroupRound.round_no == 2
            )
        )
    ).one()
    assert "unverändert" in (round2.report or "")


# ── Steuerung: Start/Stop/Pause/Gate ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_closes_open_round_and_sends_no_more_briefs(
    async_session: AsyncSession,
):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    before = len(await _thread_messages(async_session, group.thread_id))

    await stop_group(async_session, group)
    await async_session.refresh(group)
    assert group.status == "done"  # one_shot
    round_row = await _current_round(async_session, group)
    assert round_row.outcome == "stopped"

    await _tick(async_session)  # Sabotage-Probe: danach kommt NICHTS mehr
    assert len(await _thread_messages(async_session, group.thread_id)) == before


@pytest.mark.asyncio
async def test_standing_group_restart_resets_run_counters(
    async_session: AsyncSession,
):
    """Dauergruppe: Neustart nach Zielerreichung — Runden zählen pro LAUF
    (current_round_no reset), nicht pro Lebenszeit."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, lifecycle="standing", max_rounds=3
    )
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="ZIEL ERREICHT: fertig. Quelle: https://x.org",
    )
    await async_session.refresh(group)
    assert group.status == "idle"
    assert group.rounds_completed == 1  # Lebenszeit-Statistik bleibt

    group = await start_group(async_session, group)
    assert group.current_round_no == 0  # neuer Lauf zählt von vorn
    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    assert round_row.round_no == 1


@pytest.mark.asyncio
async def test_pause_freezes_engine_resume_continues(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await pause_group(async_session, group)
    before = len(await _thread_messages(async_session, group.thread_id))
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)  # pausiert → Engine rührt sich nicht
    msgs = await _thread_messages(async_session, group.thread_id)
    assert len(msgs) == before + 2  # nur die 2 Agenten-Posts, kein Lead-Prompt

    group = await start_group(async_session, group)
    await _tick(async_session)  # Resume: Sammeln geht weiter → Lead-Prompt
    round_row = await _current_round(async_session, group)
    assert round_row.lead_prompt_seq is not None


@pytest.mark.asyncio
async def test_gate_decision_approved_resumes_running(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _run_full_round(
        async_session, group, alpha, beta, gamma,
        lead_verdict="FRAGE AN OPERATOR: weitermachen?",
    )
    gate = (
        await async_session.exec(
            select(Approval).where(Approval.action_type == "group_gate")
        )
    ).one()
    await apply_group_gate_decision(async_session, gate, "approved")
    await async_session.refresh(group)
    assert group.status == "running"
    assert group.consecutive_failed_rounds == 0
