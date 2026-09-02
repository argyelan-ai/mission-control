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
    _is_pass,
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


async def _age_lead_prompt(session: AsyncSession, group, round_row) -> None:
    """Den Lead-Auftrag rückdatieren, sodass der Lead-Timeout abgelaufen ist."""
    lead_prompt = (
        await session.exec(
            select(Message).where(
                Message.thread_id == group.thread_id,
                Message.seq == round_row.lead_prompt_seq,
            )
        )
    ).one()
    lead_prompt.created_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        seconds=group.speaker_timeout_seconds + 60
    )
    session.add(lead_prompt)
    await session.commit()


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
async def test_brief_carries_length_budget(async_session: AsyncSession):
    """Kursänderung (ADR-075, 22.08.): der Chat trägt die Meinungsbildung,
    das Dokument die Substanz. Der Brief MUSS die Länge deckeln — ohne
    Vorgabe schreiben die Agenten Aufsätze (Live-Befund: 1600–4900 Zeichen)."""
    group, *_ = await _make_running_group(async_session)
    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    brief = next(m for m in msgs if m.seq == round_row.brief_seq)

    assert "Kein Absatz länger als 2 Sätze" in brief.body   # Längenbudget
    assert "Ergebnis-Dokument" in brief.body           # wohin die Substanz gehört
    assert "Quellen-URL" in brief.body                 # Quellen-Pflicht bleibt
    # Der Brief selbst bleibt knapp — er ist der grösste Kostenhebel je Runde.
    assert len(brief.body) < 1700


@pytest.mark.asyncio
async def test_brief_invites_tables_and_shows_multiline_send(async_session: AsyncSession):
    """Marks Wunsch 02.09.2026: die Agenten sollen Tabellen machen können.
    Der Raum rendert GFM (Tabellen, Listen) und klappt Beiträge zu — eine
    kompakte Tabelle stört also niemanden mehr. Der Brief muss das SAGEN, und
    zeigen, wie man mehrzeilig sendet (Heredoc über stdin), sonst zerreisst
    die Shell-Quotierung die Pipes."""
    group, *_ = await _make_running_group(async_session)
    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    brief = next(m for m in msgs if m.seq == round_row.brief_seq)

    assert "Tabelle" in brief.body
    assert f"mc msg --thread {group.thread_id} - <<'EOF'" in brief.body


@pytest.mark.asyncio
async def test_brief_demands_structured_format(async_session: AsyncSession):
    """Marks Wunsch 02.09.2026 (zweiter Teil): nicht im Fliesstext, sondern
    mit Tabellen, Bulletpoints und sauber formatierten Nachrichten. Der Raum
    klappt Beiträge zu und zeigt nur die erste Zeile — die MUSS die
    Kernaussage sein. Danach Stichpunkte mit festem Gerüst, keine Absätze."""
    group, *_ = await _make_running_group(async_session)
    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    brief = next(m for m in msgs if m.seq == round_row.brief_seq)

    assert "Kernaussage" in brief.body        # Zeile 1 = Vorschau-Zeile
    assert "Fliesstext" in brief.body         # explizit verboten
    assert "- Grund:" in brief.body           # Bullet-Gerüst
    assert "- Quelle:" in brief.body
    # Das Beispiel im Brief lebt das Gerüst vor: Kernsatz, Bullets, Tabelle.
    body = brief.body
    pos = body.index("Position in einem Satz.")
    assert pos < body.index("- Grund:", pos) < body.index("| Option |", pos)


@pytest.mark.asyncio
async def test_lead_prompt_demands_structured_verdict(async_session: AsyncSession):
    """Auch das Urteil des Leads: Marker + Kernaussage in Zeile 1, dann
    Stichpunkte — der Lead-Beitrag steht offen im Raum und prägt den Ton."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert "Kernaussage" in lead_prompt.body
    assert "Fliesstext" in lead_prompt.body
    assert "- Konsens:" in lead_prompt.body
    assert "- Dissens:" in lead_prompt.body


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


@pytest.mark.asyncio
async def test_speaker_timeout_default_is_generous(async_session: AsyncSession):
    """Operator-Korrektur 22.08.: die First-Token-Latenz lokaler Motoren ist
    bei langem Kontext hoch — der Turn-Timeout gehört HÖHER, nicht tiefer.
    Ein zu kurzer Deckel überspringt Agenten, die noch am Denken sind."""
    group, *_ = await _make_running_group(async_session)
    assert group.speaker_timeout_seconds == 900


@pytest.mark.asyncio
async def test_lead_prompt_shortens_contributions(async_session: AsyncSession):
    """Kontext als Delta statt Volltext: 2000 Zeichen je Beitrag waren der
    Hauptgrund für 30 000+ Token pro Runde. Deckel 400 → 1200 (02.09.2026),
    damit eine kompakte Vergleichstabelle den Lead ganz erreicht — ein
    Aufsatz aber weiterhin nicht."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(
        async_session, group, beta, "A" * 2000 + " Quelle: https://x.org"
    )
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert "A" * 1100 in lead_prompt.body      # der Anfang steht drin
    assert "A" * 1300 not in lead_prompt.body  # aber gekürzt


@pytest.mark.asyncio
async def test_lead_prompt_demands_short_verdict_and_long_document(
    async_session: AsyncSession,
):
    """Der Lead postet kurz und schreibt lang ins Dokument — sein
    Synthese-Beitrag war die grösste einzelne Textwand im Raum."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert "zwei bis drei Sätze" in lead_prompt.body
    assert "Tabelle" in lead_prompt.body        # ein Vergleich darf tabellarisch sein
    assert "mc group-doc" in lead_prompt.body   # die Substanz geht ins Dokument
    # Reihenfolge bleibt: erst Urteil, dann Dokument.
    assert "Zuerst das Urteil, dann das Dokument" in lead_prompt.body


# ── PASS: passen ist eine vollwertige Antwort ──────────────────────────────


def test_pass_is_recognised_tolerantly():
    """Hermes-Muster `/^\\(?\\s*pass\\s*\\)?\\.?$/i` plus die deutsche Altform
    NICHTS NEUES (laufende Gruppen dürfen nicht brechen)."""
    assert _is_pass("PASS")
    assert _is_pass("pass")
    assert _is_pass("(pass)")
    assert _is_pass(" pass. ")
    assert _is_pass("")            # Schweigen zählt wie passen
    assert _is_pass("NICHTS NEUES")
    assert _is_pass("nichts neues — sehe ich genauso")  # Altform, prefix-tolerant
    # Kein Freibrief: wer PASS nur erwähnt, hat trotzdem etwas gesagt.
    assert not _is_pass("PASS wäre hier falsch, denn Quelle: https://x.org")
    assert not _is_pass("Ich bin dagegen.")


@pytest.mark.asyncio
async def test_pass_counts_as_delivered_not_as_failed_round(
    async_session: AsyncSession,
):
    """Ein PASS ist geliefert: kein Timeout, keine Fehlrunde — und der Text
    des Passenden belastet den Lead-Auftrag nicht."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "PASS")
    await _agent_says(async_session, group, gamma, "Einwand X. Quelle: https://y.org")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    assert round_row.pending_speakers == []
    assert round_row.lead_prompt_seq is not None   # Lead-Turn kommt trotzdem
    msgs = await _thread_messages(async_session, group.thread_id)
    assert not any("übersprungen" in m.body for m in msgs)  # kein Timeout
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert "@beta" in lead_prompt.body            # als gepasst ausgewiesen
    assert "### @beta" not in lead_prompt.body    # aber nicht als Beitrag

    await _agent_says(async_session, group, alpha, "WEITER: noch offen")
    await _tick(async_session)
    await async_session.refresh(group)
    assert group.consecutive_failed_rounds == 0


@pytest.mark.asyncio
async def test_all_speakers_pass_ends_group_regularly(async_session: AsyncSession):
    """Hermes: „the room settles when a full round stays silent."
    Passen ALLE, ist nichts mehr zu synthetisieren — die Gruppe endet
    regulär, ohne den teuren Lead-Turn."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=10
    )
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "PASS")
    await _agent_says(async_session, group, gamma, "(pass)")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    assert round_row.outcome == "all_passed"
    assert round_row.finished_at is not None
    assert round_row.lead_prompt_seq is None      # der Lead wurde nicht geweckt
    await async_session.refresh(group)
    assert group.status == "done"                # one_shot: Lauf beendet
    assert group.consecutive_failed_rounds == 0  # passen ist keine Fehlrunde
    msgs = await _thread_messages(async_session, group.thread_id)
    settle = next(m for m in msgs if "still" in m.body.lower())
    assert settle.mentions == []                 # Sturm-Schutz: weckt niemanden


@pytest.mark.asyncio
async def test_legacy_nichts_neues_still_settles_the_room(
    async_session: AsyncSession,
):
    """Laufende Gruppen tragen die alte Anweisung im Brief — die deutsche
    Altform muss weiter als PASS zählen."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=10
    )
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "NICHTS NEUES")
    await _agent_says(async_session, group, gamma, "NICHTS NEUES")
    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    assert round_row.outcome == "all_passed"
    await async_session.refresh(group)
    assert group.status == "done"


@pytest.mark.asyncio
async def test_timeout_speaker_does_not_settle_the_room(
    async_session: AsyncSession,
):
    """Ein per Timeout Übersprungener ist NICHT einverstanden, sondern
    unbekannt — die Runde läuft dann normal zum Lead."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "PASS")

    round_row = await _current_round(async_session, group)
    round_row.started_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        seconds=group.speaker_timeout_seconds + 60
    )
    async_session.add(round_row)
    await async_session.commit()

    await _tick(async_session)
    round_row = await _current_round(async_session, group)
    assert round_row.outcome is None              # Runde läuft weiter
    assert round_row.lead_prompt_seq is not None  # Lead urteilt


@pytest.mark.asyncio
async def test_lead_learns_who_was_skipped(async_session: AsyncSession):
    """Der Lead muss erfahren, WER gefehlt hat.

    Die Liste war immer leer: der Timeout-Zweig räumt `pending_speakers`
    selbst, bevor `_prompt_lead` sie ausliest — der Lead bekam nie einen Namen
    zu sehen und hielt jede Runde für vollzählig. Ein Synthese-Urteil über
    zwei Meinungen, das drei gehört zu haben glaubt, ist genau der stille
    Fehler, den kein Statuswert anzeigt.
    """
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "Position B. Quelle: https://b.org")

    round_row = await _current_round(async_session, group)
    round_row.started_at = dt.datetime.now(tz=dt.timezone.utc) - dt.timedelta(
        seconds=group.speaker_timeout_seconds + 60
    )
    async_session.add(round_row)
    await async_session.commit()

    await _tick(async_session)

    round_row = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    lead_prompt = next(m for m in msgs if m.seq == round_row.lead_prompt_seq)
    assert "Übersprungen (Timeout): @gamma" in lead_prompt.body
    assert "### @beta" in lead_prompt.body   # der Anwesende zählt normal


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
    # Ab Runde 2 ersetzt die PASS-Klausel die alte Anti-Lob-Formel: passen ist
    # eine normale Option, kein Eingeständnis (Kursänderung 22.08.).
    assert "PASS" in brief2.body
    assert "denkt an den 1M-Kontext!" in brief2.body         # Operator-Einwurf


@pytest.mark.asyncio
async def test_lead_delta_and_operator_notes_are_shortened(
    async_session: AsyncSession,
):
    """Auch der Brief-Kopf bleibt knapp: Vorrunden-Delta und Operator-Einwürfe
    je ~300 Zeichen — sonst wächst der Brief mit jeder Runde."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=5
    )
    await _tick(async_session)
    await group_service.post_user_message(async_session, group, "O" * 800)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)
    await _agent_says(async_session, group, alpha, "WEITER: " + "D" * 800)
    await _tick(async_session)

    round2 = await _current_round(async_session, group)
    msgs = await _thread_messages(async_session, group.thread_id)
    brief2 = next(m for m in msgs if m.seq == round2.brief_seq)
    assert "D" * 250 in brief2.body
    assert "D" * 400 not in brief2.body
    assert "O" * 250 in brief2.body
    assert "O" * 400 not in brief2.body


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
    """Formwidriges Lead-Urteil = Fehlrunde (geschlossen beim Lead-Timeout,
    damit ein Lead sich noch mit einem Marker korrigieren kann); 2 in Folge
    (Default pause_on_failed_rounds=2) → paused + group_gate (Circuit-Breaker)."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    for round_no in (1, 2):
        await _tick(async_session)  # Brief
        await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
        await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
        await _tick(async_session)  # Lead-Prompt raus
        await _agent_says(async_session, group, alpha, "hm, schwierig zu sagen")
        await _tick(async_session)
        round_row = await _current_round(async_session, group)
        assert round_row.round_no == round_no and round_row.outcome is None
        await _age_lead_prompt(async_session, group, round_row)
        await _tick(async_session)  # Timeout → formwidrig
        await async_session.refresh(round_row)
        assert round_row.outcome == "failed"
        assert "Marker" in (round_row.report or "")
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
async def test_lead_scratch_message_without_marker_does_not_end_the_turn(
    async_session: AsyncSession,
):
    """Live 02.09.2026 (omp-Lead, Gruppe 001a5ed5): der Lead schickte beim
    Erkunden des CLI eine Probe („Test-Nachricht") — die Engine nahm sie als
    Urteil, „formwidrig", Runde verloren. Es zählt der Marker: markerlose
    Lead-Nachrichten werden übersprungen, das echte Urteil danach gewertet."""
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)  # Lead-Prompt raus

    await _agent_says(async_session, group, alpha, "Test-Nachricht")
    await _tick(async_session)
    round1 = await _current_round(async_session, group)
    assert round1.round_no == 1 and round1.outcome is None  # Turn läuft weiter

    await _agent_says(
        async_session, group, alpha, "ZIEL ERREICHT: A und B decken sich."
    )
    await _tick(async_session)
    round1 = (
        await async_session.exec(
            select(GroupRound).where(
                GroupRound.group_id == group.id, GroupRound.round_no == 1
            )
        )
    ).one()
    assert round1.outcome == "goal_reached"


@pytest.mark.asyncio
async def test_lead_timeout_fails_round(async_session: AsyncSession):
    group, alpha, beta, gamma = await _make_running_group(async_session)
    await _tick(async_session)
    await _agent_says(async_session, group, beta, "A. Quelle: https://x.org")
    await _agent_says(async_session, group, gamma, "B. Quelle: https://y.org")
    await _tick(async_session)  # Lead-Prompt raus

    round_row = await _current_round(async_session, group)
    await _age_lead_prompt(async_session, group, round_row)

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
    """≥ Hälfte der Sprecher passt, 2 Runden in Folge → Auto-Stopp vor dem
    Deckel («Entscheidung oder Abbruch»).

    Der TEIL-Fall: passen ALLE, endet die Gruppe schon in derselben Runde
    (test_all_speakers_pass_ends_group_regularly) — die Bremse deckt genau
    die Lücke dazwischen ab, in der die Runde noch zum Lead geht."""
    group, alpha, beta, gamma = await _make_running_group(
        async_session, max_rounds=10
    )
    for _ in range(2):
        await _run_full_round(
            async_session, group, alpha, beta, gamma,
            lead_verdict="WEITER: weiter suchen",
            beta_text="PASS",
            gamma_text="Immer noch offen. Quelle: https://y.org",
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
