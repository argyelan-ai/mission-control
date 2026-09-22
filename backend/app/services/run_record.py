"""Task Run Record ("Laufakte") — a curated, human-readable summary of one
task's full history, built from six existing sources (TaskEvent,
TaskComment, ActivityEvent, TaskDeliverable, Approval, ModelUsageEvent).

Bauplan 6.1 / Analyse Stufe 1 §3.3 (Kuratierung), §4.3 (UTF-8-Landmine),
Nachtrag team-lead (siehe backend/tests/test_task_run_record.py Docstring)
— dieser Nachtrag ist die massgebliche, jüngere Fassung des Vertrags.

The record has EIGHT boxes, always in this order:
  auftrag, zeiten, plan, schritte, beweise, kosten, entscheidungen, reibung

`auftrag` carries the children counter (by status) — per the Nachtrag
this lives inside `auftrag`, there is no separate "kinder" box. At <=20
children it also lists each one (title + status); above that, counts
only (Kopf-Entscheid Runde 3, nach Pruefbericht).

Curation (Analyse §3.3 / Kuratierung + Nachtrag):
  - plan: TaskComments with comment_type in (message, handoff, checkpoint)
    — progress/heartbeat noise stays out.
  - schritte: TaskEvent status transitions (carry actor_label) plus
    ActivityEvents with severity in (warning, error, critical) — pure
    info-level noise (e.g. heartbeats) stays out.
  - beweise: TaskDeliverables plus evidence/deliverable-typed comments,
    grouped by type.
  - kosten: sum(cost_usd) + tokens from ModelUsageEvent rows with task_id
    IN (the card itself + all its children) — Nacharbeit (Live-Smoke
    22.09.): usage rows hang off the CHILD cards, so a parent-only sum
    silently showed 0 USD on real epics. `kinder_anteil_usd` isolates how
    much of that sum came from children. Mandatory hint while no
    anthropic-provider event is attributed anywhere in that set
    (spawn_session_key is never populated today — Analyse §4.1/§4.2 — so
    Claude/Anthropic spend is invisible here).
  - entscheidungen: Approvals whose action_type is NOT a disturbance type
    (dispatch_escalation, lead_escalation, review_stuck, dependency_zombie
    — those NEVER count here, whatever their status; they belong to
    `reibung`). Decided ones (approved/rejected) keep that status; a
    still-open one (pending) shows with status "offen" — Kopf-Entscheid
    team-lead: Mark must see open real questions in the record, not just
    resolved ones. Any other status (e.g. expired) is still not a
    decision.
  - reibung: the disturbance-typed approvals above (any status) plus
    ActivityEvents with severity in (warning, error, critical) — collapsed
    per type into one row carrying a count and first/last timestamp, never
    listed individually. Kopf-Entscheid team-lead (Runde 3, nach
    Pruefbericht): `critical` was missing here even though `schritte`
    already included it — a first critical friction event would have been
    invisible in the one box meant to surface friction.

UTF-8: the original justification for "truncate in Python, never in SQL"
was a claimed `substr()`/`left()` crash on two specific comment rows
(`0xe2` observed via psql). Pruefbericht (Runde 3) could not reproduce
that crash against the same rows or the wider table — Postgres substr/
left on `text` cut by character, not by byte, so this was likely a psql
client-side display artifact, not a server-side truncation bug. The
Python-side truncation stays regardless: it is the safe, unambiguous
choice for character-accurate cropping independent of what caused the
one-time psql observation, and it is not a code change from Runde 3 —
only this justification is corrected.

Known gap, deferred (Pruefbericht Runde 3, Punkt 6): `model_usage_events
.task_id` has no index on the live DB even though the SQLModel field
declares `index=True` — the Kosten query above is a parallel full scan
(13.8ms / 304k rows today, grows linearly). Out of scope for this
read-only PR (needs an Alembic migration); tracked as a follow-up card,
not fixed here.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.activity import ActivityEvent
from app.models.approval import Approval
from app.models.deliverable import TaskDeliverable
from app.models.model_usage import ModelUsageEvent
from app.models.task import Task, TaskComment, TaskEvent

PLAN_COMMENT_TYPES = ("message", "handoff", "checkpoint")
BEWEIS_COMMENT_TYPES = ("evidence", "deliverable")
DECISION_STATUSES = ("approved", "rejected")
DISTURBANCE_APPROVAL_TYPES = (
    "dispatch_escalation",
    "lead_escalation",
    "review_stuck",
    "dependency_zombie",
)
FRICTION_SEVERITIES = ("warning", "error", "critical")  # schritte: keep blockers visible
REIBUNG_SEVERITIES = ("warning", "error", "critical")  # Kopf-Entscheid Runde 3: critical gehoert dazu
STEP_TRUNCATE = 25  # Runde 3: 30->25, damit Schritte vor Reibung geschrumpft werden kann
CHILD_LISTING_THRESHOLD = 20
CLAUDE_COST_HINT = "Claude-Kosten nicht zugeordnet (keine session-Verknuepfung)."

# Nacharbeit (Live-Smoke 22.09.) — per-box item caps for the Markdown
# renderer, so one box can never alone blow the 120-line budget.
PLAN_CAP = 10
BEWEISE_TYP_CAP = 10
ENTSCHEIDUNGEN_CAP = 10
REIBUNG_CAP = 10
FREITEXT_LIMIT = 200  # comment/event text — single line, character-capped
BESCHREIBUNG_LIMIT = 300  # auftrag.beschreibung — same rule, longer budget
MARKDOWN_LINE_CAP = 120


def _short(text: str | None, limit: int = 400) -> str | None:
    """Character-based truncation, done in Python rather than SQL.

    Not because a SQL-side substr()/left() was proven to crash (Pruefbericht
    Runde 3 could not reproduce the originally claimed UTF-8 error against
    the live table — see module docstring) — but because Python string
    slicing is unambiguously character-accurate regardless of encoding
    quirks, and loading full text then cropping here is the simpler,
    safer default either way.
    """
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _oneline(text: str | None, limit: int = FREITEXT_LIMIT) -> str | None:
    """Collapse newlines/whitespace to single spaces, then character-cap.

    Nacharbeit (Live-Smoke 22.09.): a multi-line description or comment
    pushed the Markdown well past the 120-line budget by itself — one
    stored field became many rendered lines. `text.split()` is codepoint-
    based, same rationale as `_short` above.
    """
    if text is None:
        return None
    collapsed = " ".join(text.split())
    return _short(collapsed, limit)


def _capped(items: list, cap: int) -> tuple[list, int]:
    """Keep the most recent `cap` items, report how many were dropped."""
    if len(items) <= cap:
        return items, 0
    return items[-cap:], len(items) - cap


async def build_run_record(session: AsyncSession, task_id: uuid.UUID) -> dict[str, Any]:
    task = await session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found")

    # ── Auftrag ──────────────────────────────────────────────────────────
    auftrag = {
        "task_id": str(task.id),
        "board_id": str(task.board_id),
        "titel": task.title,
        "beschreibung": _oneline(task.description, BESCHREIBUNG_LIMIT),
        "status": task.status,
    }

    # ── Children — loaded once, feeds both `auftrag.kinder` (counts) and
    # the `kosten` box (usage rows hang off children, not the parent —
    # Live-Smoke 22.09.) ─────────────────────────────────────────────────
    children = (
        await session.exec(select(Task).where(Task.parent_task_id == task_id))
    ).all()
    child_ids = [c.id for c in children]
    child_id_set = set(child_ids)

    # ── Load raw rows once (full text, no SQL-side truncation) ─────────
    comments = (
        await session.exec(
            select(TaskComment)
            .where(TaskComment.task_id == task_id)
            .order_by(TaskComment.created_at)
        )
    ).all()

    task_events = (
        await session.exec(
            select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.created_at)
        )
    ).all()

    activity_events = (
        await session.exec(
            select(ActivityEvent)
            .where(ActivityEvent.task_id == task_id)
            .order_by(ActivityEvent.created_at)
        )
    ).all()

    deliverables = (
        await session.exec(
            select(TaskDeliverable)
            .where(TaskDeliverable.task_id == task_id)
            .order_by(TaskDeliverable.created_at)
        )
    ).all()

    approvals = (
        await session.exec(
            select(Approval).where(Approval.task_id == task_id).order_by(Approval.created_at)
        )
    ).all()

    cost_task_ids = [task_id, *child_ids]
    usage_events = (
        await session.exec(
            select(ModelUsageEvent).where(ModelUsageEvent.task_id.in_(cost_task_ids))
        )
    ).all()

    # ── Plan: curated comments only ─────────────────────────────────────
    plan = [
        {
            "ts": c.created_at,
            "typ": c.comment_type,
            "autor": c.author_type,
            "inhalt": _oneline(c.content),
        }
        for c in comments
        if c.comment_type in PLAN_COMMENT_TYPES
    ]

    # ── Schritte: status transitions + non-info-severity activity ──────
    schritte: list[dict[str, Any]] = []
    for e in task_events:
        text = f"{e.from_status} -> {e.to_status}"
        if e.reason:
            text += f" ({e.reason})"
        schritte.append({
            "ts": e.created_at,
            "quelle": "status",
            "actor_label": e.actor_label,
            "changed_by": e.changed_by,
            "text": _oneline(text),
        })
    for a in activity_events:
        if a.severity not in FRICTION_SEVERITIES:
            continue
        schritte.append({
            "ts": a.created_at,
            "quelle": "ereignis",
            "actor_label": None,
            "changed_by": a.event_type,
            "text": _oneline(a.title),
        })
    schritte.sort(key=lambda x: x["ts"])

    # ── Beweise: deliverables + evidence-typed comments, grouped ───────
    nach_typ: dict[str, int] = {}
    beweis_items: list[dict[str, Any]] = []
    for d in deliverables:
        nach_typ[d.deliverable_type] = nach_typ.get(d.deliverable_type, 0) + 1
        beweis_items.append({
            "ts": d.created_at,
            "typ": d.deliverable_type,
            "titel": _oneline(d.title),
            "pfad": d.path,
        })
    for c in comments:
        if c.comment_type not in BEWEIS_COMMENT_TYPES:
            continue
        nach_typ[c.comment_type] = nach_typ.get(c.comment_type, 0) + 1
        beweis_items.append({
            "ts": c.created_at,
            "typ": c.comment_type,
            "titel": _oneline(c.content),
            "pfad": None,
        })
    beweis_items.sort(key=lambda x: x["ts"])
    beweise = {"anzahl": len(beweis_items), "nach_typ": nach_typ, "items": beweis_items}

    # ── Entscheidungen: real (non-disturbance) approvals — decided ones
    # keep their status, a still-open one shows as "offen" (Kopf-Entscheid
    # team-lead: Mark muss offene echte Fragen in der Akte sehen).
    # Disturbance types (dispatch_escalation/lead_escalation/review_stuck/
    # dependency_zombie) never land here, whatever their status — they
    # belong to `reibung` instead.
    entscheidungen: list[dict[str, Any]] = []
    for a in approvals:
        if a.action_type in DISTURBANCE_APPROVAL_TYPES:
            continue
        if a.status in DECISION_STATUSES:
            status_label = a.status
        elif a.status == "pending":
            status_label = "offen"
        else:
            continue  # e.g. expired, non-disturbance — still not a decision
        entscheidungen.append({
            "ts": a.created_at,
            "typ": a.action_type,
            "status": status_label,
            "description": _oneline(a.description),
            "resolver_note": _oneline(a.resolver_note),
        })

    # ── Kosten: sum from ModelUsageEvent rows attributed to this task OR
    # any of its children (Live-Smoke 22.09.: usage rows hang off the
    # CHILD cards in practice — a parent-only sum silently showed 0 USD
    # on a real epic that clearly had local-model spend). ──────────────
    gesamt_usd = 0.0
    kinder_anteil_usd = 0.0
    je_anbieter: dict[str, dict[str, float | int]] = {}
    hat_anthropic = False
    for u in usage_events:
        cost = u.cost_usd or 0.0
        gesamt_usd += cost
        if u.task_id in child_id_set:
            kinder_anteil_usd += cost
        provider = u.provider or "unbekannt"
        bucket = je_anbieter.setdefault(
            provider, {"usd": 0.0, "input_tokens": 0, "output_tokens": 0}
        )
        bucket["usd"] += cost
        bucket["input_tokens"] += u.input_tokens
        bucket["output_tokens"] += u.output_tokens
        if provider == "anthropic":
            hat_anthropic = True

    kosten: dict[str, Any] = {
        "gesamt_usd": gesamt_usd,
        "je_anbieter": je_anbieter,
        "kinder_anteil_usd": kinder_anteil_usd,
    }
    if not hat_anthropic:
        kosten["hinweis"] = CLAUDE_COST_HINT

    # ── Kinder: counted by status, lives inside `auftrag` per Nachtrag (no
    # separate "kinder" box). Kopf-Entscheid Runde 3: at <=20 children,
    # also list each one (title + status, single-line, <=80 chars) — above
    # that threshold, counts only, same as before. ─────────────────────
    by_status: dict[str, int] = {}
    for c in children:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    kinder: dict[str, Any] = {"total": len(children), "by_status": by_status}
    if len(children) <= CHILD_LISTING_THRESHOLD:
        kinder["items"] = [
            {"titel": _oneline(c.title, 80), "status": c.status} for c in children
        ]
    auftrag["kinder"] = kinder

    # ── Zeiten (Bauplan 6.1) ─────────────────────────────────────────────
    dauer_sekunden = None
    if task.completed_at and task.created_at:
        dauer_sekunden = (task.completed_at - task.created_at).total_seconds()
    zeiten = {
        "erstellt": task.created_at,
        "dispatched": task.dispatched_at,
        "bestaetigt": task.ack_at,
        "abgeschlossen": task.completed_at,
        "dauer_sekunden": dauer_sekunden,
    }

    # ── Reibung (Bauplan 6.1 / Nachtrag): friction events (severity
    # warning/error only — exact Nachtrag wording) + disturbance
    # approvals, any status, collapsed per type with a count ───────────
    reibung: dict[str, dict[str, Any]] = {}
    for a in activity_events:
        if a.severity not in REIBUNG_SEVERITIES:
            continue
        bucket = reibung.setdefault(
            a.event_type, {"anzahl": 0, "erste": a.created_at, "letzte": a.created_at}
        )
        bucket["anzahl"] += 1
        bucket["erste"] = min(bucket["erste"], a.created_at)
        bucket["letzte"] = max(bucket["letzte"], a.created_at)
    for ap in approvals:
        if ap.action_type not in DISTURBANCE_APPROVAL_TYPES:
            continue
        bucket = reibung.setdefault(
            ap.action_type, {"anzahl": 0, "erste": ap.created_at, "letzte": ap.created_at}
        )
        bucket["anzahl"] += 1
        bucket["erste"] = min(bucket["erste"], ap.created_at)
        bucket["letzte"] = max(bucket["letzte"], ap.created_at)

    # Order matches EIGHT_BOXES exactly (Nachtrag):
    # auftrag, zeiten, plan, schritte, beweise, kosten, entscheidungen, reibung
    return {
        "auftrag": auftrag,
        "zeiten": zeiten,
        "plan": plan,
        "schritte": schritte,
        "beweise": beweise,
        "kosten": kosten,
        "entscheidungen": entscheidungen,
        "reibung": reibung,
    }


def _build_markdown_lines(
    record: dict[str, Any], schritte_cap: int, plan_cap: int, beweise_typ_cap: int
) -> list[str]:
    """Assemble the full line list for the given (possibly shrunk) caps.

    Only Schritte/Plan/Beweise-Arten take a variable cap — Entscheidungen
    and Reibung always render at their fixed, full caps (ENTSCHEIDUNGEN_CAP/
    REIBUNG_CAP) and are never touched by the shrink loop below, so Reibung
    in particular can never be silently squeezed out (Pruefbericht Runde 3,
    Punkt 3: the old blind end-truncation could drop it unnoticed).
    """
    auftrag = record.get("auftrag") or {}
    lines: list[str] = [f"# Laufakte: {auftrag.get('titel', '')}", ""]

    lines.append("## Auftrag")
    lines.append(f"- Status: {auftrag.get('status')}")
    beschreibung = auftrag.get("beschreibung")
    if beschreibung:
        lines.append(f"- Beschreibung: {beschreibung}")  # already _oneline-cropped, no double crop
    kinder = auftrag.get("kinder") or {}
    if kinder.get("total"):
        lines.append(f"- Kinder gesamt: {kinder.get('total', 0)}")
        for status, count in (kinder.get("by_status") or {}).items():
            lines.append(f"  - {status}: {count}")
        for item in kinder.get("items") or []:  # only present at <=20 children
            lines.append(f"  - [{item.get('status')}] {item.get('titel')}")
    lines.append("")

    zeiten = record.get("zeiten") or {}
    if any(zeiten.get(k) for k in ("erstellt", "dispatched", "bestaetigt", "abgeschlossen")):
        lines.append("## Zeiten")
        for label, key in (
            ("Erstellt", "erstellt"),
            ("Dispatched", "dispatched"),
            ("Bestaetigt", "bestaetigt"),
            ("Abgeschlossen", "abgeschlossen"),
        ):
            val = zeiten.get(key)
            if val:
                lines.append(f"- {label}: {val}")
        if zeiten.get("dauer_sekunden") is not None:
            lines.append(f"- Dauer: {zeiten['dauer_sekunden']:.0f}s")
        lines.append("")

    lines.append("## Plan")
    plan = record.get("plan") or []
    if not plan:
        lines.append("- (keine Eintraege)")
    else:
        shown_plan, skipped_plan = _capped(plan, plan_cap)
        if skipped_plan > 0:
            lines.append(f"- … {skipped_plan} weitere nicht angezeigt")
        for p in shown_plan:
            lines.append(f"- [{p.get('typ')}] {p.get('autor')}: {p.get('inhalt')}")
    lines.append("")

    lines.append("## Schritte")
    schritte = record.get("schritte") or []
    if not schritte:
        lines.append("- (keine Eintraege)")
    else:
        shown, skipped = _capped(schritte, schritte_cap)
        if skipped > 0:
            lines.append(f"- … {skipped} weitere Schritte nicht angezeigt")
        for s in shown:
            actor = s.get("actor_label") or s.get("changed_by") or "?"
            lines.append(f"- {actor}: {s.get('text')}")
    lines.append("")

    lines.append("## Beweise")
    beweise = record.get("beweise") or {}
    if beweise.get("anzahl"):
        lines.append(f"- Anzahl: {beweise['anzahl']}")
        nach_typ_items = list((beweise.get("nach_typ") or {}).items())
        shown_typen, skipped_typen = _capped(nach_typ_items, beweise_typ_cap)
        for typ, count in shown_typen:
            lines.append(f"  - {typ}: {count}")
        if skipped_typen > 0:
            lines.append(f"  - … {skipped_typen} weitere Arten nicht angezeigt")
    else:
        lines.append("- (keine Eintraege)")
    lines.append("")

    lines.append("## Kosten")
    kosten = record.get("kosten") or {}
    lines.append(f"- Gesamt: {kosten.get('gesamt_usd', 0):.4f} USD")
    if kosten.get("kinder_anteil_usd"):
        lines.append(f"  - davon Kinder: {kosten['kinder_anteil_usd']:.4f} USD")
    if kosten.get("hinweis"):
        lines.append(f"- Hinweis: {kosten['hinweis']}")
    lines.append("")

    lines.append("## Entscheidungen")
    entscheidungen = record.get("entscheidungen") or []
    if not entscheidungen:
        lines.append("- (keine Entscheidungen)")
    else:
        shown_ent, skipped_ent = _capped(entscheidungen, ENTSCHEIDUNGEN_CAP)
        if skipped_ent > 0:
            lines.append(f"- … {skipped_ent} weitere nicht angezeigt")
        for e in shown_ent:
            lines.append(f"- [{e.get('status')}] {e.get('description')}")
    lines.append("")

    lines.append("## Reibung")
    reibung = record.get("reibung") or {}
    if not reibung:
        lines.append("- (keine Stoerungen)")
    else:
        reibung_items = list(reibung.items())
        shown_reib, skipped_reib = _capped(reibung_items, REIBUNG_CAP)
        if skipped_reib > 0:
            lines.append(f"- … {skipped_reib} weitere Arten nicht angezeigt")
        for typ, info in shown_reib:
            lines.append(
                f"- {typ}: {info.get('anzahl')}x (erste {info.get('erste')}, letzte {info.get('letzte')})"
            )
    lines.append("")

    return lines


def render_run_record_markdown(record: dict[str, Any]) -> str:
    """Deutsch, sortiert wie die Kastenreihenfolge (Nachtrag):
    Auftrag, Zeiten, Plan, Schritte, Beweise, Kosten, Entscheidungen,
    Reibung. Jede Liste ist einzeln gedeckelt (Plan/Beweis-Arten <= 10,
    Schritte <= 25, Entscheidungen/Reibung <= 10, je mit "… n weitere"),
    Beschreibung/Freitexte sind einzeilig und zeichenbasiert gekuerzt.
    Kinder (in Auftrag) werden bei <= 20 Stueck einzeln aufgelistet
    (Titel + Status), darueber nur als Zähler.

    Wenn das Ergebnis trotz der festen Deckel > 120 Zeilen waere, wird
    NICHT hart am Ende abgeschnitten (Pruefbericht Runde 3, Punkt 3: das
    haette den Reibungs-Kasten unbemerkt verschlucken koennen, Puffer war
    nur ~2 Zeilen). Stattdessen schrumpft zuerst Schritte weiter, dann
    Plan, dann Beweis-Arten (Kopf-Entscheid Runde 3) — Entscheidungen und
    Reibung bleiben dabei immer bei ihrem vollen Deckel. Ein absoluter
    Notnagel bleibt als letzte Sicherung, sollte selbst das nicht reichen.
    """
    schritte_cap = STEP_TRUNCATE
    plan_cap = PLAN_CAP
    beweise_typ_cap = BEWEISE_TYP_CAP

    lines = _build_markdown_lines(record, schritte_cap, plan_cap, beweise_typ_cap)

    # Shrink priority: Schritte -> Plan -> Beweise-Arten. Never touches
    # Entscheidungen/Reibung, which is the whole point of this loop.
    while len(lines) > MARKDOWN_LINE_CAP and (schritte_cap > 0 or plan_cap > 0 or beweise_typ_cap > 0):
        if schritte_cap > 0:
            schritte_cap -= 1
        elif plan_cap > 0:
            plan_cap -= 1
        else:
            beweise_typ_cap -= 1
        lines = _build_markdown_lines(record, schritte_cap, plan_cap, beweise_typ_cap)

    # Absolute backstop — should never trigger given the shrink loop above
    # (Schritte/Plan/Beweise can all reach 0), but guarantees the promise
    # regardless, e.g. against an unexpectedly huge Auftrag/Kosten box.
    if len(lines) > MARKDOWN_LINE_CAP:
        lines = lines[: MARKDOWN_LINE_CAP - 1] + ["… gekuerzt"]

    return "\n".join(lines).rstrip() + "\n"
