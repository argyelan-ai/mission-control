"""Group-Runner — die Runden-Engine des Multi-Agent-Gruppenchats (ADR-075, PR B).

Moderations-LOGIK = Code, Moderations-URTEIL = Lead-Agent. Dieser Runner ruft
NIE ein LLM: er baut Briefe, verteilt sie parallel an alle Sprecher (Runde 1
blind), sammelt Antworten (Timeout überspringt Säumige ehrlich), erteilt dem
Lead den Synthese-Turn (Beiträge als Delta, Dokument-Pflicht, Zwangsformat)
und wertet das Verdikt:

    ZIEL ERREICHT: … → one_shot: done · standing: idle
    WEITER: …        → nächste Runde (Delta + PASS-Klausel im Brief)
    FRAGE AN OPERATOR: … → waiting_gate + group_gate-Approval
    formwidrig/Timeout → Fehlrunde (Circuit-Breaker nach N in Folge)

Kürze ist Teil des Auftrags (Kursänderung 22.08.2026): der Chat trägt die
Meinungsbildung (2–4 Sätze je Beitrag), das Ergebnis-Dokument trägt die
Substanz. Wer nichts Neues hat, antwortet `PASS` — das zählt als geliefert,
nicht als Fehlrunde. Passen ALLE Sprecher einer Runde, endet der Lauf regulär,
ohne den Lead überhaupt zu wecken (`all_passed` → `room_settled`).

Deckel-Kaskade an jeder Rundengrenze (Reihenfolge wie loop_runner, ADR-051):
Circuit-Breaker → Gate-Frage → Ziel → stille Runde (alle gepasst) →
Fortschritts-Bremse (2× ohne neue Substanz) → max_rounds (der HARTE Deckel)
→ max_duration → Budget (weiche Bremse: Zeitfenster × Mitglieder,
Harvester-Lag — kann ~1 Runde überschiessen) → Human-Gate → nächste Runde.

Sturm-Schutz bleibt strukturell (PR A): nur dieser Runner, Marks @-Mentions
und explizite Agenten-@-Mentions erteilen das Wort; die Timeout-Notiz trägt
bewusst KEINE mentions.
"""

import asyncio
import logging
import os
import re
from datetime import timedelta

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import engine
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.group import (
    DOC_SNAPSHOT_MAX_BYTES,
    AgentGroup,
    GroupRound,
)
from app.models.thread import Message
from app.redis_client import RedisKeys, get_redis
from app.services import group_service, reference_ingest
from app.services.activity import emit_event
from app.services.messaging import post_message
from app.utils import ensure_aware, utcnow

logger = logging.getLogger("mc.group_runner")

LOCK_KEY = "mc:group_runner:cycle_lock"

# Marker des Lead-Zwangsformats — es zählt der Marker, der im Urteil ZUERST
# auftaucht (ein Lead, der "WEITER" erklärt und später "FRAGE AN OPERATOR"
# erwähnt, meint das Erste).
_VERDICT_MARKERS = (
    ("ZIEL ERREICHT", "goal_reached"),
    ("FRAGE AN OPERATOR", "ask_operator"),
    ("WEITER", "continue"),
)

# Passen ist eine vollwertige Antwort (Kursänderung 22.08.2026). Streng wie
# bei Hermes: nur wer NICHTS ausser "PASS" sagt, hat gepasst — sonst würde
# "PASS wäre hier falsch, weil …" als Schweigen durchgehen.
_PASS_RE = re.compile(r"^\(?\s*pass\s*\)?[.!]?$", re.IGNORECASE)
# Deutsche Altform. Bleibt gültig, damit laufende Gruppen nicht brechen: ihre
# alten Briefe im Thread fordern noch wörtlich "NICHTS NEUES". Bewusst
# prefix-tolerant — genau das war das bisherige Verhalten.
_STALE_PREFIX = "NICHTS NEUES"

# Längen-Deckel für alles, was die Engine zwischen Agenten weiterreicht.
# Vorher 2000 Zeichen je Beitrag — das waren 30 000–41 800 Token pro Runde
# (Live-Messung 22.08.). Der Chat trägt die Meinungsbildung, die Substanz
# steht im Ergebnis-Dokument; zum Weiterdenken reicht der Kern.
_CONTRIB_LIMIT = 400   # Beitrag eines Sprechers im Lead-Auftrag
_HEADER_LIMIT = 300    # Vorrunden-Delta und Operator-Einwürfe im Brief


def _short(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_pass(text: str | None) -> bool:
    """Hat dieser Beitrag bewusst nichts Neues beigetragen?

    Erkennt `PASS` / `(pass)` / `pass.` (Hermes-Muster), die leere Antwort
    (Schweigen zählt wie passen) und die deutsche Altform `NICHTS NEUES`.
    """
    body = (text or "").strip()
    if not body:
        return True
    if _PASS_RE.match(body):
        return True
    return body.upper().startswith(_STALE_PREFIX)


def _parse_verdict(text: str | None) -> tuple[str | None, str]:
    """(outcome, Text nach dem Marker) — None = formwidrig."""
    up = (text or "").upper()
    best: tuple[int, str, str] | None = None
    for marker, outcome in _VERDICT_MARKERS:
        pos = up.find(marker)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, marker, outcome)
    if best is None:
        return None, ""
    pos, marker, outcome = best
    remainder = (text or "")[pos + len(marker):].lstrip(" :—-\n")
    return outcome, remainder.strip()


class GroupRunnerService:
    """Singleton nach loop_runner-Muster: Intervall-Tick + Per-Cycle-Redis-Lock."""

    def __init__(self, interval: int = 15) -> None:
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Group-Runner gestartet (Intervall %ss)", self.interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Group-Runner gestoppt")

    async def _run_loop(self) -> None:
        await asyncio.sleep(15)  # Boot-Grace
        while self._running:
            try:
                redis = await get_redis()
                got_lock = await redis.set(LOCK_KEY, "1", nx=True, ex=self.interval * 3)
                if got_lock:
                    async with AsyncSession(engine, expire_on_commit=False) as session:
                        await self.tick(session)
                    await redis.delete(LOCK_KEY)
            except Exception:  # noqa: BLE001 — ein Fehler darf den Runner nie killen
                logger.exception("Group-Runner-Tick fehlgeschlagen")
            await asyncio.sleep(self.interval)

    # ── Kern-Tick (separat aufrufbar für Tests) ─────────────────────────

    async def tick(self, session: AsyncSession) -> None:
        result = await session.exec(
            select(AgentGroup).where(AgentGroup.status == "running")
        )
        for group in list(result.all()):
            try:
                await self._advance(session, group)
            except Exception:  # noqa: BLE001
                logger.exception("Gruppe %s: advance fehlgeschlagen", group.id)
                try:
                    await session.rollback()
                except Exception:  # noqa: BLE001
                    pass

    async def _advance(self, session: AsyncSession, group: AgentGroup) -> None:
        round_row = await _latest_round(session, group)
        if round_row is None or round_row.finished_at is not None:
            await self._start_round(session, group)
            return
        await self._continue_round(session, group, round_row)

    # ── Runde starten ───────────────────────────────────────────────────

    async def _start_round(self, session: AsyncSession, group: AgentGroup) -> None:
        members = await group_service.group_member_agents(session, group)
        speakers = [
            group_service.canonical_handle(a)
            for a in members
            if a.id != group.lead_agent_id and a.archived_at is None
        ]
        if not speakers or group.lead_agent_id is None:
            await self._pause_with_gate(
                session, group,
                reason="no_speakers",
                description=(
                    f"Gruppe '{group.name}' pausiert: kein Lead oder keine "
                    "Sprecher übrig — Mitglieder prüfen."
                ),
            )
            return

        round_no = group.current_round_no + 1
        round_row = GroupRound(
            group_id=group.id,
            round_no=round_no,
            kind="autonomous",
            pending_speakers=speakers,
        )
        session.add(round_row)
        group.current_round_no = round_no
        group.updated_at = utcnow()
        session.add(group)
        await session.commit()
        await session.refresh(round_row)

        await self._post_brief(session, group, round_row, members)

        await emit_event(
            session, "group.round_started",
            f"Gruppe '{group.name}': Runde {round_no}/{group.max_rounds} gestartet",
            detail={"group_id": str(group.id), "round_no": round_no},
        )
        await self._broadcast(group, "group.round_started", {
            "group_id": str(group.id),
            "round_no": round_no,
            "max_rounds": group.max_rounds,
            "pending_speakers": speakers,
        })

    async def _post_brief(
        self, session: AsyncSession, group: AgentGroup,
        round_row: GroupRound, members: list[Agent],
    ) -> None:
        """Brief posten + brief_seq/started_at setzen. Recovery-sicher:
        Crash zwischen Runden-Row und Brief → der nächste Tick postet den
        Brief erneut (at-least-once, für die Agenten idempotent genug)."""
        brief = await self._build_brief(session, group, round_row, members)
        msg = await post_message(
            session,
            thread_id=group.thread_id,
            sender_type="system",
            message_type="system",
            body=brief,
            mentions=list(round_row.pending_speakers),
            mirror_to_telegram=False,
        )
        round_row.brief_seq = msg.seq
        round_row.started_at = utcnow()
        session.add(round_row)
        await session.commit()

    async def _build_brief(
        self, session: AsyncSession, group: AgentGroup,
        round_row: GroupRound, members: list[Agent],
    ) -> str:
        n, mx = round_row.round_no, group.max_rounds
        parts = [
            f"# Gruppe: {group.name} — Runde {n}/{mx}",
            "",
            "## Ziel",
            group.goal.strip(),
        ]

        prev = await _previous_finished_round(session, group, before_no=n)
        if prev is not None:
            _outcome, delta = await _resolve_lead_verdict(session, group, prev)
            if delta:
                parts += ["", "## Stand der Vorrunde (Lead)", _short(delta, _HEADER_LIMIT)]
            operator_notes = await _user_messages_since(
                session, group, since_seq=prev.brief_seq or 0
            )
            if operator_notes:
                parts += ["", "## Operator-Einwürfe seit der letzten Runde"]
                parts += [f"- {_short(m.body, _HEADER_LIMIT)}" for m in operator_notes]

        parts += [
            "",
            f"## Deine Aufgabe (Runde {n}/{mx})",
            "- Recherchiere/denke selbstständig im Sinne des Ziels und antworte "
            f"mit GENAU EINEM Beitrag: `mc msg --thread {group.thread_id} \"…\"`.",
            # Der grösste Hebel gegen Textwände: ohne Längenbudget schreibt ein
            # so beauftragter Agent einen Aufsatz — er tut genau, was dasteht.
            "- **Antworte in 2–4 Sätzen**: deine Position, ein Grund, eine "
            "Quelle als Link. Ausführliche Belege gehören ins Ergebnis-Dokument, "
            "nicht in den Raum.",
            "- Quellen-Pflicht bleibt: eine Behauptung ohne Quellen-URL ist kein "
            "Beitrag — der nackte Link genügt, kein Zitat-Block.",
            "- Antworte NICHT auf andere Mitglieder per @-Mention — die Engine "
            "sammelt alle Beiträge und gibt sie weiter.",
        ]
        member_roles = await _member_roles(session, group)
        critic_handles = [
            f"@{slug}" for slug, role in member_roles.items() if role == "critic"
        ]
        if critic_handles:
            parts += [
                f"- Kritiker-Rolle ({', '.join(critic_handles)}): nenne mindestens "
                "einen konkreten Einwand oder eine Lücke — in einem Satz.",
            ]
        if round_row.round_no >= 2:
            # Erst ab Runde 2: in Runde 1 hat noch niemand etwas gehört, ein
            # PASS wäre dort kein Schweigen, sondern Arbeitsverweigerung.
            parts += [
                "- Hast du nichts Neues beizutragen, antworte nur mit `PASS`. "
                "Passen ist eine vollwertige Antwort; reine Zustimmung dagegen "
                "ist kein Beitrag. Passen ALLE, endet die Gruppe.",
            ]
        return "\n".join(parts)

    # ── Runde fortführen: sammeln → Lead-Turn → Verdikt ─────────────────

    async def _continue_round(
        self, session: AsyncSession, group: AgentGroup, round_row: GroupRound,
    ) -> None:
        members = await group_service.group_member_agents(session, group)

        if round_row.brief_seq is None:
            await self._post_brief(session, group, round_row, members)
            return

        if round_row.lead_prompt_seq is None:
            await self._collect(session, group, round_row, members)
            return

        await self._await_lead(session, group, round_row, members)

    async def _collect(
        self, session: AsyncSession, group: AgentGroup,
        round_row: GroupRound, members: list[Agent],
    ) -> None:
        slug_by_id = {a.id: group_service.canonical_handle(a) for a in members}
        replies = await _agent_messages_since(
            session, group, since_seq=round_row.brief_seq or 0
        )
        answered = {slug_by_id.get(m.sender_id) for m in replies}
        pending = [s for s in round_row.pending_speakers if s not in answered]
        if pending != round_row.pending_speakers:
            round_row.pending_speakers = pending
            session.add(round_row)
            await session.commit()

        skipped_by_timeout: list[str] = []
        if pending:
            started = ensure_aware(round_row.started_at) if round_row.started_at else None
            timed_out = (
                started is not None
                and utcnow() - started
                >= timedelta(seconds=group.speaker_timeout_seconds)
            )
            if not timed_out:
                return
            # Ehrlich überspringen — Notiz OHNE mentions (weckt niemanden).
            await post_message(
                session,
                thread_id=group.thread_id,
                sender_type="system",
                message_type="system",
                body=(
                    "⏳ Timeout — übersprungen: "
                    + ", ".join(f"@{s}" for s in pending)
                    + f" (keine Antwort nach {group.speaker_timeout_seconds}s)."
                ),
                mentions=[],
                mirror_to_telegram=False,
            )
            skipped_by_timeout = list(pending)
            round_row.pending_speakers = []
            session.add(round_row)
            await session.commit()

        # ── Stille Runde beendet den Lauf ───────────────────────────────
        # Hermes: „the room settles when a full round stays silent." Haben ALLE
        # Sprecher gepasst, gibt es nichts zu synthetisieren — die Gruppe endet
        # regulär, und der teuerste Turn (Lead-Synthese) entfällt gleich mit.
        #
        # Bewusst NUR bei aktivem Passen aller: wer per Timeout übersprungen
        # wurde, ist nicht einverstanden, sondern unbekannt — dann läuft die
        # Runde normal weiter zum Lead.
        #
        # Die Fortschritts-Bremse (continue_stale) bleibt DANEBEN bestehen: sie
        # deckt den Teil-Fall ab, in dem nur die Hälfte passt und der Lead
        # trotzdem WEITER urteilt. Ersetzen würde diese Lücke aufreissen.
        lead_slug = slug_by_id.get(group.lead_agent_id)
        first_by_slug = _first_reply_per_speaker(replies, slug_by_id, lead_slug)
        if (
            not skipped_by_timeout
            and first_by_slug
            and all(_is_pass(body) for body in first_by_slug.values())
        ):
            await post_message(
                session,
                thread_id=group.thread_id,
                sender_type="system",
                message_type="system",
                body=(
                    "🤫 Alle Sprecher haben gepasst — die Runde bleibt still. "
                    "Es gibt nichts Neues zu synthetisieren, der Lauf endet hier."
                ),
                mentions=[],  # Sturm-Schutz: die Schluss-Notiz weckt niemanden.
                mirror_to_telegram=False,
            )
            await self._complete_round(
                session, group, round_row, outcome="all_passed",
                note="Alle Sprecher haben gepasst — stille Runde.",
            )
            return

        await self._prompt_lead(
            session, group, round_row, members, skipped=skipped_by_timeout,
        )

    async def _prompt_lead(
        self, session: AsyncSession, group: AgentGroup,
        round_row: GroupRound, members: list[Agent],
        skipped: list[str] | None = None,
    ) -> None:
        lead = next((a for a in members if a.id == group.lead_agent_id), None)
        if lead is None:
            await self._complete_round(
                session, group, round_row, outcome="failed",
                note="Lead ist nicht mehr Mitglied — Runde ohne Urteil geschlossen.",
            )
            return
        lead_slug = group_service.canonical_handle(lead)
        slug_by_id = {a.id: group_service.canonical_handle(a) for a in members}

        contributions = await _agent_messages_since(
            session, group, since_seq=round_row.brief_seq or 0
        )
        first_by_slug = _first_reply_per_speaker(contributions, slug_by_id, lead_slug)
        contrib_parts: list[str] = []
        passed: list[str] = []
        for slug, body in first_by_slug.items():
            # Wer gepasst hat, kostet eine Zeile statt eines Blocks — sein
            # "PASS" trägt keine Information, nur Token.
            if _is_pass(body):
                passed.append(slug)
                continue
            contrib_parts += [f"### @{slug}", _short(body, _CONTRIB_LIMIT)]
        # `skipped` reicht der Sammler durch: `round_row.pending_speakers` ist
        # zu diesem Zeitpunkt IMMER schon leer (der Timeout-Zweig räumt es
        # selbst), die Liste hier aus der Runden-Zeile zu lesen ergab nie einen
        # Namen — der Lead erfuhr nie, wer gefehlt hat.
        skipped = [s for s in (skipped or []) if s not in first_by_slug]

        # Lesen darf der Agent die Datei (Mount ist da), SCHREIBEN nicht —
        # der References-Mount ist in den Agenten-Containern read-only (live
        # belegt 21.08.2026). Deshalb nennt der Auftrag beide Wege getrennt:
        # Pfad zum Lesen, API zum Schreiben.
        doc_abs = ""
        if group.result_doc_rel_path:
            doc_abs = os.path.join(
                reference_ingest.references_root(), group.result_doc_rel_path
            )

        body_parts = [
            f"@{lead_slug} — Synthese-Turn Runde {round_row.round_no}/{group.max_rounds}.",
            "",
            "## Beiträge dieser Runde",
            *(contrib_parts or ["(keine Beiträge — alle Sprecher übersprungen)"]),
        ]
        if passed:
            body_parts += [
                "", "Gepasst (nichts Neues): " + ", ".join(f"@{s}" for s in passed),
            ]
        if skipped:
            body_parts += ["", "Übersprungen (Timeout): " + ", ".join(f"@{s}" for s in skipped)]
        body_parts += [
            "",
            "## Deine Pflichten — in DIESER Reihenfolge",
            "**Zuerst das Urteil, dann das Dokument.** Scheitert das Dokument-Update, poste trotzdem dein Urteil — eine Runde ohne Urteil zählt als Fehlrunde und die Gruppe verliert eine von wenigen Runden.",
            (
                "2. Aktualisiere das Ergebnis-Dokument — schreib den vollstaendigen neuen "
                "Stand, die Datei wird ersetzt. Lesen kannst du es unter "
                f"`{doc_abs}`; SCHREIBEN geht nur ueber das CLI (der Mount ist "
                "read-only):\n"
                "```\n"
                "cat > /tmp/result.md <<'EOF'\n"
                "# … dein Dokument …\n"
                "EOF\n"
                f"mc group-doc {group.id} --file /tmp/result.md\n"
                "```\n"
                "   Halte Quellen UND Dissens fest, glaette nichts."
                if doc_abs else
                "2. (Kein Ergebnis-Dokument konfiguriert.)"
            ),
            f"1. Antworte mit `mc msg --thread {group.thread_id}` und beginne dein "
            "Urteil mit GENAU EINEM Marker:",
            "   - `ZIEL ERREICHT: <dein Verdikt in zwei bis drei Sätzen>`",
            "   - `WEITER: <was noch offen ist>`",
            "   - `FRAGE AN OPERATOR: <deine Frage>`",
            # Der Synthese-Beitrag war die grösste einzelne Textwand im Raum
            # (bis 4900 Zeichen). Die Substanz ist im Dokument nicht verloren,
            # sondern dort erst am richtigen Platz.
            "**Halte den Chat-Beitrag kurz: Marker + zwei bis drei Sätze.** Die "
            "ausführliche Synthese mit Quellen und Dissens gehört ins "
            "Ergebnis-Dokument, nicht in den Raum.",
            "Eine Antwort ohne Marker wertet die Runde als gescheitert.",
        ]
        msg = await post_message(
            session,
            thread_id=group.thread_id,
            sender_type="system",
            message_type="system",
            body="\n".join(body_parts),
            mentions=[lead_slug],
            mirror_to_telegram=False,
        )
        round_row.lead_prompt_seq = msg.seq
        session.add(round_row)
        await session.commit()
        await self._broadcast(group, "group.turn_started", {
            "group_id": str(group.id),
            "round_no": round_row.round_no,
            "speaker": lead_slug,
            "phase": "synthesis",
        })

    async def _await_lead(
        self, session: AsyncSession, group: AgentGroup,
        round_row: GroupRound, members: list[Agent],
    ) -> None:
        # Es zählt der Marker, nicht die erste Nachricht: ein Lead, der beim
        # Erkunden des CLI eine Probe schickt (live 02.09.2026: „Test-
        # Nachricht"), hat damit noch nicht geurteilt. Markerlose Nachrichten
        # werden übersprungen; erst der Timeout schliesst die Runde als
        # formwidrig — mit der letzten markerlosen Nachricht als Beleg.
        lead_messages = list(
            await session.exec(
                select(Message)
                .where(
                    Message.thread_id == group.thread_id,
                    Message.sender_type == "agent",
                    Message.sender_id == group.lead_agent_id,
                    Message.seq > (round_row.lead_prompt_seq or 0),
                )
                .order_by(Message.seq.asc())  # type: ignore[union-attr]
            )
        )
        reply = next(
            (m for m in lead_messages if _parse_verdict(m.body)[0] is not None),
            None,
        )

        if reply is None:
            prompt = (
                await session.exec(
                    select(Message).where(
                        Message.thread_id == group.thread_id,
                        Message.seq == round_row.lead_prompt_seq,
                    )
                )
            ).first()
            prompted_at = ensure_aware(prompt.created_at) if prompt else None
            if (
                prompted_at is not None
                and utcnow() - prompted_at
                >= timedelta(seconds=group.speaker_timeout_seconds)
            ):
                if lead_messages:
                    await self._complete_round(
                        session, group, round_row, outcome="failed",
                        note="Lead-Urteil ohne Marker (formwidrig).",
                        verdict_text=lead_messages[-1].body,
                    )
                else:
                    await self._complete_round(
                        session, group, round_row, outcome="failed",
                        note="Lead-Timeout — Runde ohne Urteil geschlossen.",
                    )
            return

        outcome, remainder = _parse_verdict(reply.body)
        await self._complete_round(
            session, group, round_row, outcome=outcome, verdict_text=remainder,
        )

    # ── Rundenabschluss + Kaskade ───────────────────────────────────────

    async def _complete_round(
        self, session: AsyncSession, group: AgentGroup, round_row: GroupRound,
        *, outcome: str, verdict_text: str = "", note: str = "",
    ) -> None:
        members = await group_service.group_member_agents(session, group)
        slug_by_id = {a.id: group_service.canonical_handle(a) for a in members}
        lead_slug = slug_by_id.get(group.lead_agent_id)
        speakers_total = len([a for a in members if a.id != group.lead_agent_id])

        # Fortschritts-Substanz: erster Beitrag je Sprecher — gepasst?
        replies = await _agent_messages_since(
            session, group, since_seq=round_row.brief_seq or 0
        )
        first_by_slug = _first_reply_per_speaker(replies, slug_by_id, lead_slug)
        stale_count = sum(1 for body in first_by_slug.values() if _is_pass(body))
        is_stale = speakers_total > 0 and stale_count * 2 >= speakers_total
        if outcome == "continue" and is_stale:
            outcome = "continue_stale"

        # Dokument-Snapshot + Unverändert-Prüfung (ehrlich statt still).
        # Beides gilt nur, wenn der Lead überhaupt einen Turn hatte — in einer
        # stillen Runde (alle gepasst) wurde er nie gefragt, weder „aktualisiert"
        # noch „unverändert" wäre da eine ehrliche Aussage.
        lead_had_turn = round_row.lead_prompt_seq is not None
        doc_note = ""
        snapshot = await self._snapshot_doc(session, group, round_row)
        if snapshot is not None:
            round_row.doc_snapshot = snapshot
        if snapshot is not None and lead_had_turn:
            prev_snapshot = await _previous_doc_snapshot(session, group, round_row)
            if prev_snapshot is None:
                prev_snapshot = group_service._DOC_SKELETON.format(
                    name=group.name, goal=group.goal
                )
            if snapshot == prev_snapshot:
                doc_note = (
                    "Ergebnis-Dokument unverändert — der Lead hat es in dieser "
                    "Runde nicht angefasst."
                )

        # Budget-Snapshot: Zeitfenster × Mitglieder (Python-seitig gefiltert —
        # korrekt vor clever; Volumen pro Runde ist klein).
        tokens, cost = await self._usage_in_window(
            session, members, since=round_row.started_at
        )
        round_row.tokens_used = tokens
        round_row.cost_usd = cost

        report_lines = [f"**Outcome:** {outcome}"]
        if note:
            report_lines.append(f"**Hinweis:** {note}")
        if verdict_text:
            report_lines.append(f"**Lead:** {_short(verdict_text)}")
        if doc_note:
            report_lines.append(f"**Dokument:** {doc_note}")
        elif snapshot is not None and lead_had_turn:
            report_lines.append(
                f"**Dokument:** aktualisiert (Snapshot Runde {round_row.round_no})."
            )
        if stale_count:
            report_lines.append(
                f"**Substanz:** {stale_count}/{speakers_total} Sprecher haben "
                "gepasst."
            )
        if cost:
            report_lines.append(f"**Kosten (Fenster):** ~{cost:.2f} USD")

        round_row.outcome = outcome
        round_row.report = "\n".join(report_lines)
        round_row.finished_at = utcnow()
        session.add(round_row)

        group.rounds_completed += 1
        if outcome == "failed":
            group.consecutive_failed_rounds += 1
        else:
            group.consecutive_failed_rounds = 0
        group.updated_at = utcnow()
        session.add(group)
        await session.commit()

        await emit_event(
            session, "group.round_completed",
            f"Gruppe '{group.name}': Runde {round_row.round_no} → {outcome}",
            severity="info" if outcome != "failed" else "warning",
            detail={"group_id": str(group.id), "round_no": round_row.round_no,
                    "outcome": outcome},
        )
        await self._broadcast(group, "group.round_completed", {
            "group_id": str(group.id),
            "round_no": round_row.round_no,
            "outcome": outcome,
            "cost_usd": cost,
        })
        if snapshot is not None and lead_had_turn and not doc_note:
            await self._broadcast(group, "group.doc_updated", {
                "group_id": str(group.id),
                "version": round_row.round_no,
            })
        await self._send_operator_report(group, round_row, outcome, verdict_text or note)

        # ── Kaskade ────────────────────────────────────────────────────
        if group.consecutive_failed_rounds >= max(group.pause_on_failed_rounds, 1):
            await self._pause_with_gate(
                session, group,
                reason="circuit_breaker",
                description=(
                    f"Gruppe '{group.name}' pausiert: "
                    f"{group.consecutive_failed_rounds} Fehlrunden in Folge."
                ),
            )
            return

        if outcome == "ask_operator":
            await self._wait_for_gate(session, group, question=verdict_text)
            return

        if outcome == "goal_reached":
            await self._finish_run(session, group, reason="goal_reached")
            return

        # Stille Runde: alle haben gepasst → der Raum hat sich gesetzt. Kein
        # Fehler, kein Deckel — ein reguläres Ende wie ZIEL ERREICHT, nur ohne
        # Verdikt (der Lead wurde gar nicht erst geweckt, siehe _collect).
        if outcome == "all_passed":
            await self._finish_run(session, group, reason="room_settled")
            return

        if outcome == "continue_stale":
            prev = await _previous_finished_round(
                session, group, before_no=round_row.round_no
            )
            if prev is not None and prev.outcome == "continue_stale":
                await self._finish_run(session, group, reason="progress_stalled")
                return

        if group.current_round_no >= group.max_rounds:
            await self._finish_run(session, group, reason="max_rounds")
            return

        if (
            group.max_duration_minutes
            and group.started_at
            and utcnow() - ensure_aware(group.started_at)
            >= timedelta(minutes=group.max_duration_minutes)
        ):
            await self._finish_run(session, group, reason="max_duration")
            return

        if group.budget_usd is not None or group.budget_tokens is not None:
            run_tokens, run_cost = await self._run_usage(session, group)
            over_usd = group.budget_usd is not None and run_cost >= group.budget_usd
            over_tok = (
                group.budget_tokens is not None and run_tokens >= group.budget_tokens
            )
            if over_usd or over_tok:
                await self._pause_with_gate(
                    session, group,
                    reason="budget_exceeded",
                    description=(
                        f"Gruppe '{group.name}' pausiert: Budget erschöpft "
                        f"(~{run_cost:.2f} USD / {run_tokens} Tokens in diesem Lauf). "
                        "Budget erhöhen oder beenden."
                    ),
                )
                return

        if (
            group.human_every_n_rounds > 0
            and group.current_round_no % group.human_every_n_rounds == 0
        ):
            await self._wait_for_gate(
                session, group,
                question=f"Gate nach Runde {group.current_round_no} — weiterlaufen?",
                reason="scheduled_gate",
            )
            return

        await self._start_round(session, group)

    # ── Hilfen: Dokument, Budget, Reports ───────────────────────────────

    async def _snapshot_doc(
        self, session: AsyncSession, group: AgentGroup, round_row: GroupRound,
    ) -> str | None:
        if not group.result_doc_rel_path:
            return None
        abs_path = os.path.join(
            reference_ingest.references_root(), group.result_doc_rel_path
        )
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(DOC_SNAPSHOT_MAX_BYTES + 1)
        except OSError:
            return None
        if len(content.encode("utf-8", errors="replace")) > DOC_SNAPSHOT_MAX_BYTES:
            content = content[:DOC_SNAPSHOT_MAX_BYTES] + "\n\n_[gekürzt — Snapshot-Cap]_"
        return content

    async def _usage_in_window(
        self, session: AsyncSession, members: list[Agent], *, since,
    ) -> tuple[int, float]:
        """Summe (input+output Tokens, USD) der Mitglieder seit `since`.

        Bekannte Unschärfe (ADR-075): parallele Task-Arbeit eines Mitglieds
        zählt mit ins Fenster (konservativ — stoppt eher zu früh), und der
        Token-Harvester liest nachlaufend. Cache-Tokens zählen nicht
        (Begründung loop_runner._loop_usage)."""
        from app.models.model_usage import ModelUsageEvent

        if since is None:
            return 0, 0.0
        member_ids = [a.id for a in members]
        rows = (
            await session.exec(
                select(ModelUsageEvent).where(
                    ModelUsageEvent.agent_id.in_(member_ids)  # type: ignore[union-attr]
                )
            )
        ).all()
        since_aware = ensure_aware(since)
        tokens = 0
        cost = 0.0
        for e in rows:
            if ensure_aware(e.ts) < since_aware:
                continue
            tokens += (e.input_tokens or 0) + (e.output_tokens or 0)
            cost += e.cost_usd or 0.0
        return tokens, cost

    async def _run_usage(
        self, session: AsyncSession, group: AgentGroup,
    ) -> tuple[int, float]:
        """Summe der Runden-Snapshots dieses Laufs (Runden seit started_at)."""
        rows = (
            await session.exec(
                select(GroupRound).where(GroupRound.group_id == group.id)
            )
        ).all()
        start = ensure_aware(group.started_at) if group.started_at else None
        tokens = 0
        cost = 0.0
        for r in rows:
            if start is not None and r.created_at is not None:
                if ensure_aware(r.created_at) < start:
                    continue
            tokens += r.tokens_used or 0
            cost += r.cost_usd or 0.0
        return tokens, cost

    async def _send_operator_report(
        self, group: AgentGroup, round_row: GroupRound, outcome: str, excerpt: str,
    ) -> None:
        if not group.operator_reports:
            return
        try:
            from app.services.operator_reports import report_backends, send_report
            if not report_backends():
                return
            lines = [
                f"👥 <b>{group.name}</b> — Runde {round_row.round_no}/"
                f"{group.max_rounds}: <b>{outcome.upper()}</b>",
            ]
            short = _short(excerpt, 220)
            if short:
                lines.append(short)
            await send_report("\n".join(lines))
        except Exception as e:  # noqa: BLE001
            logger.warning("Gruppen-Runden-Report fehlgeschlagen: %s", e)

    # ── Gates / Finish ──────────────────────────────────────────────────

    async def _create_gate_approval(
        self, session: AsyncSession, group: AgentGroup, *,
        reason: str, description: str, question: str = "",
    ) -> Approval:
        approval = Approval(
            board_id=None,
            task_id=None,
            agent_id=None,
            action_type="group_gate",
            description=description,
            payload={
                "group_id": str(group.id),
                "group_name": group.name,
                "round_no": group.current_round_no,
                "rounds_completed": group.rounds_completed,
                "max_rounds": group.max_rounds,
                "reason": reason,
                "question": question,
            },
            expires_at=utcnow() + timedelta(hours=24),
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        try:
            from app.services import operator_approvals
            await operator_approvals.send_approval(
                approval.id, f"Gruppe '{group.name}'", description,
                "Approve = weiterlaufen, Reject = pausiert lassen.",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Group-Gate-Report fehlgeschlagen: %s", e)
        return approval

    async def _pause_with_gate(
        self, session: AsyncSession, group: AgentGroup, *,
        reason: str, description: str,
    ) -> None:
        group.status = "paused"
        group.updated_at = utcnow()
        session.add(group)
        await session.commit()
        await self._create_gate_approval(
            session, group, reason=reason, description=description,
        )
        await emit_event(
            session, "group.paused", description, severity="warning",
            detail={"group_id": str(group.id), "reason": reason},
        )
        await self._broadcast(group, "group.status_changed", {
            "group_id": str(group.id), "status": "paused", "reason": reason,
        })

    async def _wait_for_gate(
        self, session: AsyncSession, group: AgentGroup, *,
        question: str, reason: str = "ask_operator",
    ) -> None:
        group.status = "waiting_gate"
        group.updated_at = utcnow()
        session.add(group)
        await session.commit()
        await self._create_gate_approval(
            session, group, reason=reason,
            description=f"Gruppe '{group.name}' wartet auf dich: {_short(question, 200)}",
            question=question,
        )
        await emit_event(
            session, "group.gate_requested",
            f"Gruppe '{group.name}' wartet auf dein Go",
            detail={"group_id": str(group.id), "question": question},
        )
        await self._broadcast(group, "group.gate_requested", {
            "group_id": str(group.id), "question": question, "reason": reason,
        })

    async def _finish_run(
        self, session: AsyncSession, group: AgentGroup, *, reason: str,
    ) -> None:
        if group.lifecycle == "standing":
            group.status = "idle"  # Dauergruppe lebt weiter
        else:
            group.status = "done"
            group.finished_at = utcnow()
        group.updated_at = utcnow()
        session.add(group)
        await session.commit()
        await emit_event(
            session, "group.run_finished",
            f"Gruppe '{group.name}': Lauf beendet ({reason}) — "
            f"{group.current_round_no} Runden",
            detail={"group_id": str(group.id), "reason": reason},
        )
        await self._broadcast(group, "group.status_changed", {
            "group_id": str(group.id), "status": group.status, "reason": reason,
        })

    async def _broadcast(self, group: AgentGroup, event_type: str, data: dict) -> None:
        try:
            from app.services import sse as sse_service
            await sse_service.broadcast(
                RedisKeys.group_events(str(group.id)), event_type, data
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Group-SSE fehlgeschlagen (%s): %s", event_type, e)


# ── Query-Hilfen (modulweit, auch für Router nutzbar) ───────────────────────


async def _latest_round(session: AsyncSession, group: AgentGroup) -> GroupRound | None:
    return (
        await session.exec(
            select(GroupRound)
            .where(GroupRound.group_id == group.id)
            .order_by(GroupRound.created_at.desc(), GroupRound.round_no.desc())  # type: ignore[union-attr]
            .limit(1)
        )
    ).first()


async def _previous_finished_round(
    session: AsyncSession, group: AgentGroup, *, before_no: int,
) -> GroupRound | None:
    """Die zuletzt abgeschlossene Runde vor `before_no` in DIESEM Lauf."""
    start = ensure_aware(group.started_at) if group.started_at else None
    rows = (
        await session.exec(
            select(GroupRound)
            .where(
                GroupRound.group_id == group.id,
                GroupRound.round_no < before_no,
                GroupRound.finished_at != None,  # noqa: E711
            )
            .order_by(GroupRound.created_at.desc())  # type: ignore[union-attr]
        )
    ).all()
    for r in rows:
        if start is not None and r.created_at is not None:
            if ensure_aware(r.created_at) < start:
                continue
        return r
    return None


async def _previous_doc_snapshot(
    session: AsyncSession, group: AgentGroup, current: GroupRound,
) -> str | None:
    rows = (
        await session.exec(
            select(GroupRound)
            .where(
                GroupRound.group_id == group.id,
                GroupRound.id != current.id,
                GroupRound.doc_snapshot != None,  # noqa: E711
            )
            .order_by(GroupRound.created_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
    ).first()
    return rows.doc_snapshot if rows else None


def _first_reply_per_speaker(
    replies: list[Message], slug_by_id: dict, lead_slug: str | None,
) -> dict[str, str]:
    """Erster Beitrag je Sprecher (Lead ausgenommen), Reihenfolge = seq.

    Es zählt der ERSTE Beitrag: wer nachlegt, bekommt keinen zweiten Platz im
    Lead-Auftrag — sonst hätte die Sprecherliste keine Wirkung mehr.
    """
    first: dict[str, str] = {}
    for m in replies:
        slug = slug_by_id.get(m.sender_id)
        if slug is None or slug == lead_slug or slug in first:
            continue
        first[slug] = m.body or ""
    return first


async def _agent_messages_since(
    session: AsyncSession, group: AgentGroup, *, since_seq: int,
) -> list[Message]:
    return list(
        (
            await session.exec(
                select(Message)
                .where(
                    Message.thread_id == group.thread_id,
                    Message.sender_type == "agent",
                    Message.seq > since_seq,
                )
                .order_by(Message.seq.asc())  # type: ignore[union-attr]
            )
        ).all()
    )


async def _user_messages_since(
    session: AsyncSession, group: AgentGroup, *, since_seq: int,
) -> list[Message]:
    return list(
        (
            await session.exec(
                select(Message)
                .where(
                    Message.thread_id == group.thread_id,
                    Message.sender_type == "user",
                    Message.seq > since_seq,
                )
                .order_by(Message.seq.asc())  # type: ignore[union-attr]
            )
        ).all()
    )


async def _member_roles(session: AsyncSession, group: AgentGroup) -> dict[str, str]:
    from app.models.group import GroupMember

    rows = (
        await session.exec(
            select(GroupMember, Agent)
            .join(Agent, Agent.id == GroupMember.agent_id)  # type: ignore[arg-type]
            .where(GroupMember.group_id == group.id)
        )
    ).all()
    return {group_service.canonical_handle(a): m.role for m, a in rows}


async def _resolve_lead_verdict(
    session: AsyncSession, group: AgentGroup, round_row: GroupRound,
) -> tuple[str | None, str]:
    if round_row.lead_prompt_seq is None:
        return None, ""
    reply = (
        await session.exec(
            select(Message)
            .where(
                Message.thread_id == group.thread_id,
                Message.sender_type == "agent",
                Message.sender_id == group.lead_agent_id,
                Message.seq > round_row.lead_prompt_seq,
            )
            .order_by(Message.seq.asc())  # type: ignore[union-attr]
            .limit(1)
        )
    ).first()
    if reply is None:
        return None, ""
    return _parse_verdict(reply.body)


# ── Steuerung (Router + UI) ─────────────────────────────────────────────────


async def supersede_pending_gates(session: AsyncSession, group_id) -> None:
    pending = (
        await session.exec(
            select(Approval).where(
                Approval.action_type == "group_gate",
                Approval.status == "pending",
            )
        )
    ).all()
    for a in pending:
        if (a.payload or {}).get("group_id") == str(group_id):
            a.status = "superseded"
            a.resolved_at = utcnow()
            session.add(a)


async def start_group(session: AsyncSession, group: AgentGroup) -> AgentGroup:
    """idle/draft/paused/waiting_gate → running.

    Resume (offene Runde vorhanden) behält Lauf-Zähler und started_at —
    die Runde läuft weiter, wo sie stand. Frischer Lauf setzt
    current_round_no=0 und started_at neu (max_rounds zählt PRO Lauf)."""
    if group.status not in ("draft", "idle", "paused", "waiting_gate"):
        raise ValueError(
            f"Gruppe kann aus Status '{group.status}' nicht gestartet werden"
        )
    open_round = await _latest_round(session, group)
    resuming = open_round is not None and open_round.finished_at is None

    group.status = "running"
    group.consecutive_failed_rounds = 0
    if not resuming:
        group.started_at = utcnow()
        group.current_round_no = 0
    group.updated_at = utcnow()
    await supersede_pending_gates(session, group.id)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    await emit_event(
        session, "group.started",
        f"Gruppe '{group.name}' läuft ({'Resume' if resuming else 'neuer Lauf'})",
        detail={"group_id": str(group.id)},
    )
    return group


async def pause_group(session: AsyncSession, group: AgentGroup) -> AgentGroup:
    if group.status not in ("running", "waiting_gate"):
        raise ValueError(f"Gruppe ist nicht aktiv (Status '{group.status}')")
    group.status = "paused"
    group.updated_at = utcnow()
    session.add(group)
    await session.commit()
    await session.refresh(group)
    await emit_event(
        session, "group.paused", f"Gruppe '{group.name}' pausiert (Operator)",
        detail={"group_id": str(group.id), "reason": "operator"},
    )
    return group


async def stop_group(session: AsyncSession, group: AgentGroup) -> AgentGroup:
    """one_shot → done (abgeschlossen); standing → idle. Offene Runde wird
    als `stopped` geschlossen — danach postet die Engine NICHTS mehr."""
    open_round = await _latest_round(session, group)
    if open_round is not None and open_round.finished_at is None:
        open_round.outcome = "stopped"
        open_round.finished_at = utcnow()
        session.add(open_round)

    if group.lifecycle == "standing":
        group.status = "idle"
    else:
        group.status = "done"
        group.finished_at = utcnow()
    group.updated_at = utcnow()
    await supersede_pending_gates(session, group.id)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    await emit_event(
        session, "group.stopped",
        f"Gruppe '{group.name}' gestoppt (Operator) → {group.status}",
        detail={"group_id": str(group.id)},
    )
    return group


async def apply_group_gate_decision(
    session: AsyncSession, approval: Approval, decision: str,
) -> None:
    """group_gate-Entscheidung — geteilter Pfad für resolve_approval (UI)
    UND Telegram-Quick-Resolve (Zwilling von apply_loop_gate_decision)."""
    import uuid as _uuid

    _group_id = (approval.payload or {}).get("group_id")
    if not _group_id:
        return
    try:
        group = await session.get(AgentGroup, _uuid.UUID(str(_group_id)))
    except (ValueError, TypeError):
        logger.warning("group_gate mit kaputter group_id im Payload: %r", _group_id)
        return
    if group is None or group.status not in ("paused", "waiting_gate"):
        return

    if decision == "approved":
        group.status = "running"
        group.consecutive_failed_rounds = 0
    else:
        group.status = "paused"
    group.updated_at = utcnow()
    session.add(group)
    await session.commit()
    await emit_event(
        session, "group.gate_resolved",
        f"Gruppe '{group.name}': Gate {decision}",
        detail={"group_id": str(group.id), "decision": decision},
    )


group_runner = GroupRunnerService()
