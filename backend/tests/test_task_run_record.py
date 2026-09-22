"""Tests for the Task Run Record ("Laufakte") — a curated, human-readable
summary of one task's full history, built from six existing sources
(TaskEvent, TaskComment, ActivityEvent, TaskDeliverable, Approval,
ModelUsageEvent).

Contract (Lauf 5, Bauplan Abschnitt 6.1, Nachtrag team-lead):
  GET  /api/v1/tasks/{task_id}/run-record          -> JSON   (viewer+)
  GET  /api/v1/tasks/{task_id}/run-record.md        -> Markdown (viewer+)
  POST /api/v1/tasks/{task_id}/run-record/to-vault   -> writes to Vault (operator+)

Router:
  app/routers/run_record.py, roles like app/routers/workflows.py:93/:99
  (require_role(Role.VIEWER) for reads, require_role(Role.OPERATOR) for export).

Service:
  app/services/run_record.py
    async def build_run_record(session, task_id) -> dict
    def render_run_record_markdown(record: dict) -> str

The record has EIGHT boxes, always in this order:
  auftrag, zeiten, plan, schritte, beweise, kosten, entscheidungen, reibung

`auftrag` includes the children counter (by status, no individual rows
above 20 children) — per the Nachtrag this moved out of a separate
"kinder" box into `auftrag`.

Curation rules under test (kuratierung.md + Nachtrag):
  - plan: TaskComments with comment_type in a curated allowlist (message,
    handoff, checkpoint) — NOT progress/heartbeat noise.
  - schritte: curated status/activity events, carrying actor_label.
  - beweise: TaskDeliverables + evidence/deliverable-typed comments.
  - kosten: sum(cost_usd) + tokens from model_usage_events with task_id set,
    plus a mandatory "Claude-Kosten nicht zugeordnet" hint until an
    anthropic-provider event is attributed to the card.
  - entscheidungen: Approvals with status in (approved, rejected) AND
    action_type NOT IN a disturbance-type list (dispatch_escalation,
    lead_escalation, review_stuck, dependency_zombie) — those go to
    `reibung` instead, never appear as decisions regardless of status.
  - reibung: healer/escalation/disturbance signals — the disturbance-typed
    approvals above, plus ActivityEvents with severity in (warning, error,
    critical) — Kopf-Entscheid Runde 3, nach Pruefbericht: `critical` was
    missing and is now included, same as `schritte` already had it —
    counted (repeats collapsed into one row with a count), not listed
    individually.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


EIGHT_BOXES = [
    "auftrag",
    "zeiten",
    "plan",
    "schritte",
    "beweise",
    "kosten",
    "entscheidungen",
    "reibung",
]

DISTURBANCE_ACTION_TYPES = (
    "dispatch_escalation",
    "lead_escalation",
    "review_stuck",
    "dependency_zombie",
)


# ── Helpers ──────────────────────────────────────────────────────────────

async def _add(*objs):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for obj in objs:
            s.add(obj)
        await s.commit()


async def _viewer_token() -> str:
    """JWT for a viewer user (pattern from tests/test_hosts_api.py)."""
    from app.auth import create_access_token
    from app.models.user import User

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"viewer-{uid.hex[:8]}@mc.local", name="Viewer",
                   role="viewer", is_active=True))
        await s.commit()
    return create_access_token(str(uid), "viewer")


# ── 1. Eight boxes, fixed order ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_record_has_eight_boxes_in_order(auth_client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="done", title="Parent Task")

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    assert list(record.keys())[:8] == EIGHT_BOXES


# ── 2. auftrag carries the children counter, not individual rows ────────

@pytest.mark.asyncio
async def test_run_record_children_counted_not_listed_in_auftrag(auth_client, make_board, make_task):
    board = await make_board()
    parent = await make_task(board.id, status="in_progress", title="Epic")

    statuses = ["done"] * 15 + ["in_progress"] * 7 + ["failed"] * 3
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        for i, status in enumerate(statuses):
            s.add(Task(
                board_id=board.id, parent_task_id=parent.id,
                title=f"Child {i}", status=status,
            ))
        await s.commit()

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, parent.id)

    auftrag = record["auftrag"]
    kinder = auftrag["kinder"]
    # 25 children total, well over the >20 "no individual rows" threshold.
    assert kinder["total"] == 25
    assert kinder["by_status"]["done"] == 15
    assert kinder["by_status"]["in_progress"] == 7
    assert kinder["by_status"]["failed"] == 3
    assert "items" not in kinder or not kinder.get("items")


@pytest.mark.asyncio
async def test_run_record_children_listed_individually_at_or_under_20(auth_client, make_board, make_task):
    """Kopf-Entscheid Runde 3 (nach Pruefbericht Punkt 1): at <= 20
    children, `auftrag.kinder.items` additionally lists each child (title,
    status) — no per-child plan/step detail, just the one summary row.
    The Markdown renders one "  - [status] titel" line per child. Above
    20, still counts only (covered by the sibling test above: 25 -> no
    lines, no `items` key)."""
    board = await make_board()
    parent = await make_task(board.id, status="in_progress", title="Small Epic")

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task
        for i, status in enumerate(["done", "done", "in_progress", "failed", "done"]):
            s.add(Task(
                board_id=board.id, parent_task_id=parent.id,
                title=f"Kleine Karte {i}", status=status,
            ))
        await s.commit()

    from app.services.run_record import build_run_record, render_run_record_markdown

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, parent.id)

    kinder = record["auftrag"]["kinder"]
    assert kinder["total"] == 5
    assert len(kinder["items"]) == 5
    assert {"titel": "Kleine Karte 0", "status": "done"} in kinder["items"]

    markdown = render_run_record_markdown(record)
    auftrag_section = markdown.split("## Auftrag")[1].split("## Zeiten")[0]
    child_lines = [ln for ln in auftrag_section.splitlines() if ln.strip().startswith("- [")]
    assert len(child_lines) == 5
    assert any("Kleine Karte 0" in ln for ln in child_lines)


# ── 3. Noise curation in plan/schritte ──────────────────────────────────

@pytest.mark.asyncio
async def test_run_record_curates_noise_out(auth_client, make_board, make_task):
    """progress/checkpoint-comments and heartbeat-like activity events stay
    out; handoff/blocker-carrying content shows up."""
    board = await make_board()
    task = await make_task(board.id, status="in_progress")

    from app.models.task import TaskComment
    from app.models.activity import ActivityEvent

    await _add(
        TaskComment(
            task_id=task.id, author_type="agent", comment_type="progress",
            content="still working, 40% done",
        ),
        TaskComment(
            task_id=task.id, author_type="agent", comment_type="handoff",
            content="Handing off to reviewer, tests green.",
        ),
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.heartbeat",
            title="heartbeat", severity="info",
        ),
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.blocked",
            title="Blocked: missing credentials", severity="warning",
        ),
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    plan_and_steps_text = repr(record["plan"]) + repr(record["schritte"])
    assert "40% done" not in plan_and_steps_text
    assert "heartbeat" not in plan_and_steps_text.lower()
    assert "Handing off to reviewer" in plan_and_steps_text


# ── 4. Steps carry actor_label ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_record_steps_carry_actor_label(auth_client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="review")

    from app.models.task import TaskEvent

    await _add(
        TaskEvent(
            task_id=task.id, from_status="in_progress", to_status="review",
            changed_by="user", actor_label="Mark (operator)",
            reason="manual review request",
        ),
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    steps_text = repr(record["schritte"])
    assert "Mark (operator)" in steps_text


# ── 5. Costs flag unattributed Claude spend ─────────────────────────────

@pytest.mark.asyncio
async def test_run_record_costs_flag_unattributed_claude(auth_client, make_board, make_task):
    """Only local-model usage carries task_id in this dataset; Claude/
    anthropic usage is never attributed (session_id has no link to the
    card). The cost box must sum what IS attributed and must still show
    the mandatory hint that Claude costs are missing."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.model_usage import ModelUsageEvent
    from app.utils import utcnow

    await _add(
        ModelUsageEvent(
            task_id=task.id, harness="workera", model="qwen38-27b",
            provider="local", session_id="s-1", message_uuid=str(uuid.uuid4()),
            input_tokens=1000, output_tokens=500, cost_usd=0.02,
            ts=utcnow(), source_file="/tmp/a.jsonl",
        ),
        # Anthropic usage exists but is NEVER linked to this task_id
        # (spawn_session_key is always empty — see Bauplan 4.1).
        ModelUsageEvent(
            task_id=None, harness="host", model="claude-sonnet-4-6",
            provider="anthropic", session_id="s-2", message_uuid=str(uuid.uuid4()),
            input_tokens=50000, output_tokens=20000, cost_usd=5.00,
            ts=utcnow(), source_file="/tmp/b.jsonl",
        ),
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    kosten = record["kosten"]
    assert kosten["gesamt_usd"] == pytest.approx(0.02)
    assert "hinweis" in kosten
    assert "Claude-Kosten nicht zugeordnet" in kosten["hinweis"]
    # The unattributed 5.00 USD must never silently appear in the sum.
    assert kosten["gesamt_usd"] < 1.0


@pytest.mark.asyncio
async def test_run_record_costs_no_hint_once_anthropic_attributed(auth_client, make_board, make_task):
    """Once an anthropic-provider event IS attributed to the card (task_id
    set), the mandatory hint must not misleadingly claim it's missing."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.model_usage import ModelUsageEvent
    from app.utils import utcnow

    await _add(
        ModelUsageEvent(
            task_id=task.id, harness="host", model="claude-sonnet-4-6",
            provider="anthropic", session_id="s-3", message_uuid=str(uuid.uuid4()),
            input_tokens=1000, output_tokens=500, cost_usd=0.30,
            ts=utcnow(), source_file="/tmp/c.jsonl",
        ),
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    kosten = record["kosten"]
    assert kosten["gesamt_usd"] == pytest.approx(0.30)
    assert not kosten.get("hinweis")


@pytest.mark.asyncio
async def test_run_record_costs_roll_up_from_children(auth_client, make_board, make_task):
    """Nacharbeit (Live-Smoke 22.09.): on the real epic, ModelUsageEvent
    rows hang off the CHILD cards, not the parent — a parent-only sum
    showed 0 USD despite real local-model spend on the epic. Costs must
    roll up from task_id IN (parent, *children), and `kinder_anteil_usd`
    must isolate the children's share."""
    board = await make_board()
    parent = await make_task(board.id, status="done", title="Epic With Child Spend")
    child = await make_task(
        board.id, status="done", title="Child With Spend", parent_task_id=parent.id,
    )

    from app.models.model_usage import ModelUsageEvent
    from app.utils import utcnow

    await _add(
        ModelUsageEvent(
            task_id=child.id, harness="workera", model="qwen38-27b",
            provider="local", session_id="s-child", message_uuid=str(uuid.uuid4()),
            input_tokens=2000, output_tokens=1000, cost_usd=1.23,
            ts=utcnow(), source_file="/tmp/child.jsonl",
        ),
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, parent.id)

    kosten = record["kosten"]
    assert kosten["gesamt_usd"] == pytest.approx(1.23)
    assert kosten["kinder_anteil_usd"] == pytest.approx(1.23)


@pytest.mark.asyncio
async def test_plan_and_entscheidungen_are_individually_capped(auth_client, make_board, make_task):
    """Nacharbeit (Live-Smoke 22.09.): each box is capped on its own
    (Plan/Entscheidungen <= 10 rendered rows), independent of the global
    120-line backstop — a box with e.g. 15 real handoff comments must
    never render all 15. `build_run_record` still returns the FULL data
    (the API's JSON view isn't capped, only the Markdown rendering is).

    Sabotage target: delete the `_capped(...)` call for plan (or its
    ENTSCHEIDUNGEN_CAP counterpart) — the rendered section would then show
    all 15 rows with no "… weitere" notice, and this test must fail."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.task import TaskComment
    from app.models.approval import Approval

    handoffs = [
        TaskComment(
            task_id=task.id, author_type="agent", comment_type="handoff",
            content=f"Handoff {i}",
        )
        for i in range(15)
    ]
    approvals = [
        Approval(
            board_id=board.id, task_id=task.id, action_type="deploy",
            description=f"Deploy {i}", status="approved",
        )
        for i in range(15)
    ]
    await _add(*handoffs, *approvals)

    from app.services.run_record import build_run_record, render_run_record_markdown

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    # The JSON record itself carries all 15 — only rendering caps them.
    assert len(record["plan"]) == 15
    assert len(record["entscheidungen"]) == 15

    markdown = render_run_record_markdown(record)
    plan_section = markdown.split("## Plan")[1].split("## Schritte")[0]
    assert plan_section.count("Handoff ") <= 10
    assert "weitere" in plan_section

    entscheidungen_section = markdown.split("## Entscheidungen")[1].split("## Reibung")[0]
    assert entscheidungen_section.count("Deploy ") <= 10
    assert "weitere" in entscheidungen_section


# ── 6. Decisions vs. disturbances (the Nachtrag's central rule) ─────────

@pytest.mark.asyncio
async def test_run_record_separates_decisions_from_disturbances(auth_client, make_board, make_task):
    """Real, resolved decisions (approved/rejected, non-disturbance
    action_type) land in `entscheidungen`. Disturbance-typed approvals
    (dispatch_escalation/lead_escalation/review_stuck/dependency_zombie)
    NEVER count as decisions, whatever their status — they belong under
    `reibung` instead.

    Kopf-Entscheid (team-lead, Nachtrag 2): a plain PENDING question that
    is NOT a disturbance type is a still-open real decision — Mark needs
    to see open questions in the record, so it lands in `entscheidungen`
    with status "offen" (erlaubte Teststaenderung: vorher wurde "weder
    Entscheidung noch Reibung" erwartet, jetzt zeigt der Kopf offene
    echte Fragen explizit)."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.approval import Approval

    approved = Approval(
        board_id=board.id, task_id=task.id, action_type="deploy",
        description="Deploy to prod", status="approved",
        resolver_note="looks good",
    )
    rejected = Approval(
        board_id=board.id, task_id=task.id, action_type="config_change",
        description="Risky config change", status="rejected",
    )
    pending_question = Approval(
        board_id=board.id, task_id=task.id, action_type="question",
        description="Still waiting", status="pending",
    )
    # Disturbance-typed, even though "approved" — must still NOT be a decision.
    escalation_approved = Approval(
        board_id=board.id, task_id=task.id, action_type="dispatch_escalation",
        description="Escalation but approved", status="approved",
    )
    # 22 dispatch_escalation-style approvals — incident noise, not decisions.
    escalations = [
        Approval(
            board_id=board.id, task_id=task.id, action_type="dispatch_escalation",
            description=f"Escalation {i}", status="expired",
        )
        for i in range(22)
    ]
    lead_escalation = Approval(
        board_id=board.id, task_id=task.id, action_type="lead_escalation",
        description="Lead escalation", status="rejected",
    )
    review_stuck = Approval(
        board_id=board.id, task_id=task.id, action_type="review_stuck",
        description="Review stuck watchdog", status="expired",
    )
    dependency_zombie = Approval(
        board_id=board.id, task_id=task.id, action_type="dependency_zombie",
        description="Dependency zombie watchdog", status="pending",
    )

    await _add(
        approved, rejected, pending_question, escalation_approved,
        *escalations, lead_escalation, review_stuck, dependency_zombie,
    )

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    entscheidungen = record["entscheidungen"]
    # 3, not 2: the open, non-disturbance "Still waiting" question now
    # counts too (Kopf-Entscheid — offene echte Fragen gehoeren in die Akte).
    assert len(entscheidungen) == 3
    descriptions = {e["description"] if isinstance(e, dict) else str(e) for e in entscheidungen}
    assert descriptions == {"Deploy to prod", "Risky config change", "Still waiting"}

    by_description = {e["description"]: e for e in entscheidungen}
    assert by_description["Deploy to prod"]["status"] == "approved"
    assert by_description["Risky config change"]["status"] == "rejected"
    assert by_description["Still waiting"]["status"] == "offen"

    joined = repr(entscheidungen)
    assert "Escalation" not in joined
    assert "Lead escalation" not in joined
    assert "Review stuck" not in joined
    assert "Dependency zombie" not in joined


@pytest.mark.asyncio
async def test_reibung_lists_healer_events_with_counts(auth_client, make_board, make_task):
    """`reibung` collects healer/escalation/disturbance signals: the
    disturbance-typed approvals from above, plus ActivityEvents with
    severity warning/error/critical — repeats collapsed into ONE row
    carrying a count, never one row per occurrence.

    Kopf-Entscheid Runde 3 (nach Pruefbericht Punkt 2): `critical` was
    missing from the severity filter even though `schritte` already
    included it — a `critical` event added below, erlaubte
    Teststaenderung, Grund: die urspruengliche Fassung deckte diese Luecke
    nicht ab.

    Sabotage target: the line filtering ActivityEvent.severity in
    (warning, error, critical) — remove it and this test must fail
    (info-severity noise would leak in, or the count would include info
    rows / drop the critical one)."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.approval import Approval
    from app.models.activity import ActivityEvent

    escalations = [
        Approval(
            board_id=board.id, task_id=task.id, action_type="dispatch_escalation",
            description=f"Escalation {i}", status="expired",
        )
        for i in range(5)
    ]
    activity_events = [
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.recovery_triggered",
            title="Recovery triggered", severity="warning",
        )
        for _ in range(3)
    ] + [
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.critical_fail",
            title="Critical failure", severity="error",
        )
    ] + [
        # critical must count too (Kopf-Entscheid Runde 3) — a lone
        # critical row before this fix was invisible in `reibung`.
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.data_loss",
            title="Data loss detected", severity="critical",
        )
    ] + [
        # Must NOT be counted as friction — plain info noise.
        ActivityEvent(
            task_id=task.id, board_id=board.id, event_type="task.info_note",
            title="Just FYI", severity="info",
        )
    ]

    await _add(*escalations, *activity_events)

    from app.services.run_record import build_run_record

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    reibung = record["reibung"]
    reibung_text = repr(reibung)

    # dispatch_escalation must show up here (not in entscheidungen), and
    # repeats must be COUNTED, not listed 5 times individually.
    assert "dispatch_escalation" in reibung_text or "Escalation" in reibung_text
    assert reibung_text.count("Escalation 0") <= 1, "disturbance repeats must be collapsed with a count, not listed individually"

    # recovery_triggered (3x, severity warning) collapsed with a count >= 3
    # somewhere, not three separate "Recovery triggered" rows.
    assert reibung_text.count("Recovery triggered") <= 1

    # Info-severity noise never appears.
    assert "Just FYI" not in reibung_text
    assert "task.info_note" not in reibung_text

    # critical must be counted too (Kopf-Entscheid Runde 3).
    assert "task.data_loss" in reibung_text

    # The count itself is visible (some numeric evidence of 3 occurrences).
    assert "3" in reibung_text


# ── 7. Markdown stays short for a big epic ──────────────────────────────

@pytest.mark.asyncio
async def test_markdown_under_120_lines_for_epic(auth_client, make_board, make_task):
    """Nacharbeit (Live-Smoke 22.09.): the real epic 0edc4595 blew the
    120-line budget to 156 lines because its multi-line description went
    in ungekuerzt. This now also piles on 50 long handoff comments (plan
    box) on top of the original 100 children / 300 status events, so the
    per-box caps AND the single-line description rule are both exercised,
    not just the schritte truncation that was already covered."""
    board = await make_board()

    long_description = "\n".join(
        f"Zeile {i}: Kontext und Hintergrund fuer die Laufakte, viel Fliesstext hier zum Aufblaehen."
        for i in range(40)
    )
    assert len(long_description) >= 2000, "Testdaten muessen die 2000-Zeichen-Schwelle wirklich reissen"

    parent = await make_task(
        board.id, status="done", title="Big Epic", description=long_description,
    )

    base = datetime(2026, 9, 1, 8, 0, 0)

    from app.models.task import TaskComment

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        from app.models.task import Task, TaskEvent

        for i in range(100):
            s.add(Task(
                board_id=board.id, parent_task_id=parent.id,
                title=f"Child {i}", status="done" if i % 3 else "failed",
            ))
        for i in range(300):
            s.add(TaskEvent(
                task_id=parent.id, from_status="in_progress", to_status="in_progress",
                changed_by="agent", reason=f"step {i}",
                created_at=base + timedelta(minutes=i),
            ))
        for i in range(50):
            s.add(TaskComment(
                task_id=parent.id, author_type="agent", comment_type="handoff",
                content=(f"Handoff {i}: " + "Uebergabe-Detail mit viel Text. " * 10),
                created_at=base + timedelta(minutes=i),
            ))
        await s.commit()

    from app.services.run_record import build_run_record, render_run_record_markdown

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, parent.id)

    # Beschreibung ist einzeilig und gekuerzt — kein Zeilenumbruch mehr drin.
    assert "\n" not in record["auftrag"]["beschreibung"]

    markdown = render_run_record_markdown(record)
    line_count = len(markdown.splitlines())
    assert line_count <= 120, f"Markdown had {line_count} lines, must be <= 120"

    # All eight headings, in order.
    headings = ["Auftrag", "Zeiten", "Plan", "Schritte", "Beweise", "Kosten", "Entscheidungen", "Reibung"]
    positions = [markdown.find(h) for h in headings]
    assert all(p != -1 for p in positions), f"Missing heading(s): {headings} in {markdown[:500]}"
    assert positions == sorted(positions)

    # Truncation hint for the long step list.
    assert "weitere" in markdown


@pytest.mark.asyncio
async def test_reibung_survives_overflow_shrink(auth_client, make_board, make_task):
    """Kopf-Entscheid Runde 3 (nach Pruefbericht Punkt 3): when even the
    fixed per-box caps overflow 120 lines, the renderer shrinks Schritte
    further first, then Plan, then Beweise-Arten — Entscheidungen and
    Reibung are never touched, so all 10 friction types always render in
    full, never silently dropped by a blind end-truncation.

    This piles on every box at once (20 listed children, 300 Schritte, 20
    Plan comments, 20 Beweis-Arten, 20 Entscheidungen, 10 Reibungs-Arten)
    to force real overflow past the per-box caps — not just exercise the
    caps themselves (that's the sibling cap test).

    Sabotage target: revert to the old blind end-truncation (`lines[:119]
    + ["… gekuerzt"]`, no shrink loop) — with this much load, the last
    box (Reibung) would be the one cut off or missing entirely, and this
    test must fail."""
    board = await make_board()
    base = datetime(2026, 9, 1, 8, 0, 0)
    task = await make_task(
        board.id, status="done",
        dispatched_at=base, ack_at=base, completed_at=base + timedelta(hours=1),
    )

    from app.models.task import Task, TaskEvent, TaskComment
    from app.models.activity import ActivityEvent
    from app.models.deliverable import TaskDeliverable
    from app.models.approval import Approval

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for i in range(20):
            # Deliberately unique per-child status strings (not real workflow
            # statuses) purely to inflate `by_status` — this is a rendering
            # stress test, not a workflow test.
            s.add(Task(
                board_id=board.id, parent_task_id=task.id,
                title=f"Kind {i} mit ordentlich langem Titel fuer Zeilenlast",
                status=f"laststatus_{i}",
            ))
        for i in range(300):
            s.add(TaskEvent(
                task_id=task.id, from_status="in_progress", to_status="in_progress",
                changed_by="agent", reason=f"step {i}",
                created_at=base + timedelta(minutes=i),
            ))
        for i in range(20):
            s.add(TaskComment(
                task_id=task.id, author_type="agent", comment_type="handoff",
                content=f"Handoff Nummer {i} mit etwas Fuelltext drumherum.",
                created_at=base + timedelta(minutes=i),
            ))
        for i in range(20):
            s.add(TaskDeliverable(
                task_id=task.id, deliverable_type=f"beweisart_{i}",
                title=f"Beweis {i}", created_at=base + timedelta(minutes=i),
            ))
        for i in range(20):
            s.add(Approval(
                board_id=board.id, task_id=task.id, action_type="deploy",
                description=f"Entscheidung {i}", status="approved",
            ))
        for i in range(10):
            s.add(ActivityEvent(
                task_id=task.id, board_id=board.id, event_type=f"task.friction_{i}",
                title=f"Friction type {i}", severity="warning",
            ))
        await s.commit()

    from app.services.run_record import build_run_record, render_run_record_markdown

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        record = await build_run_record(s, task.id)

    assert len(record["reibung"]) == 10  # full data survives in the JSON regardless

    markdown = render_run_record_markdown(record)
    assert len(markdown.splitlines()) <= 120

    reibung_section = markdown.split("## Reibung")[1]
    for i in range(10):
        assert f"task.friction_{i}" in reibung_section, (
            f"Reibungsart {i} fehlt — Reibung darf vom Notnagel nie geschrumpft werden"
        )
    assert "weitere Arten nicht angezeigt" not in reibung_section


# ── 8. Endpoint: JSON + Markdown, viewer can read / operator can export ─

@pytest.mark.asyncio
async def test_endpoint_json_and_markdown_operator_only(auth_client, client, make_board, make_task):
    board = await make_board()
    task = await make_task(board.id, status="done", title="Endpoint Task")

    resp = await auth_client.get(f"/api/v1/tasks/{task.id}/run-record")
    assert resp.status_code == 200
    body = resp.json()
    assert list(body.keys())[:8] == EIGHT_BOXES

    resp_md = await auth_client.get(f"/api/v1/tasks/{task.id}/run-record.md")
    assert resp_md.status_code == 200
    assert "text/markdown" in resp_md.headers["content-type"]
    assert "Auftrag" in resp_md.text

    # No auth at all -> rejected.
    # Erlaubte Teststaenderung (Lauf 5a): `auth_client` mutiert denselben
    # AsyncClient, den `client` referenziert (auth_client is client, per
    # tests/conftest.py:489-500) — ohne diesen Header-Reset traegt
    # `client` hier bereits den Admin-Token aus `auth_client` und der
    # "kein Auth"-Fall wird nie wirklich geprueft (empirisch verifiziert:
    # `client.headers` enthaelt "authorization" nach `auth_client`-Setup).
    client.headers.pop("Authorization", None)
    resp_noauth = await client.get(f"/api/v1/tasks/{task.id}/run-record")
    assert resp_noauth.status_code in (401, 403)


@pytest.mark.asyncio
async def test_endpoint_viewer_can_read_but_not_export(auth_client, client, make_board, make_task):
    """Viewer role (lowest) can GET the record, matching
    routers/workflows.py's require_role(Role.VIEWER) pattern — but the
    to-vault export requires operator (require_role(Role.OPERATOR))."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    token = await _viewer_token()
    client.headers["Authorization"] = f"Bearer {token}"

    resp = await client.get(f"/api/v1/tasks/{task.id}/run-record")
    assert resp.status_code == 200

    resp_export = await client.post(f"/api/v1/tasks/{task.id}/run-record/to-vault")
    assert resp_export.status_code == 403


@pytest.mark.asyncio
async def test_endpoint_404_for_missing_task(auth_client):
    resp = await auth_client.get(f"/api/v1/tasks/{uuid.uuid4()}/run-record")
    assert resp.status_code == 404
    # Must be the route's own "task not found" guard, not a coincidental
    # 404 from an unmatched path — the generic FastAPI "Not Found" body
    # would pass by accident before the route exists.
    assert resp.json()["detail"] == "Task not found"


# ── 9. Broken UTF-8 comment must not 500 ────────────────────────────────

@pytest.mark.asyncio
async def test_endpoint_survives_broken_utf8_comment(auth_client, make_board, make_task):
    """Regression guard for the SQL-side truncation landmine: cutting text
    with substr(content,1,10) can slice a multi-byte UTF-8 character in
    half and raise 'invalid byte sequence for encoding UTF8' on Postgres.
    Truncation must happen in Python, on a long unicode comment, never in
    SQL — and the endpoint must return 200, not 500."""
    board = await make_board()
    task = await make_task(board.id, status="done")

    from app.models.task import TaskComment

    # A long multi-byte-heavy comment whose naive byte-offset truncation
    # (e.g. a 10-byte cut) would land mid-character. Python string slicing
    # by character (not byte) must handle this cleanly.
    tricky_content = "Fortschritt: " + ("Übergabe bereit, Prüfung läuft. " * 50) + "🚀🚀🚀 Ende."
    await _add(
        TaskComment(
            task_id=task.id, author_type="agent", comment_type="handoff",
            content=tricky_content,
        ),
    )

    resp = await auth_client.get(f"/api/v1/tasks/{task.id}/run-record")
    assert resp.status_code == 200

    resp_md = await auth_client.get(f"/api/v1/tasks/{task.id}/run-record.md")
    assert resp_md.status_code == 200


# ── 10. Vault export ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_writes_vault_file(auth_client, make_board, make_task, tmp_path, monkeypatch):
    import app.config
    monkeypatch.setattr(app.config.settings, "vault_path", tmp_path)

    board = await make_board()
    task = await make_task(board.id, status="done", title="Export Me")

    resp = await auth_client.post(f"/api/v1/tasks/{task.id}/run-record/to-vault")
    assert resp.status_code == 200
    body = resp.json()
    assert "path" in body

    from pathlib import Path as _Path
    written_path = tmp_path / body["path"] if not str(body["path"]).startswith(str(tmp_path)) else _Path(body["path"])
    assert written_path.exists(), f"Expected exported file at {written_path}"
    content = written_path.read_text()
    assert "Auftrag" in content
    assert "Export Me" in content


@pytest.mark.asyncio
async def test_export_carries_valid_frontmatter_for_vault_watcher(
    auth_client, make_board, make_task, tmp_path, monkeypatch,
):
    """Live-Befund (nach Deploy main 855eab44): the exported file had no
    YAML frontmatter at all, so the real VaultWatcher immediately
    quarantined it to `_rejected/` with "missing required field: id" — the
    tmp-Vault export test above never saw this because it doesn't run the
    watcher. The exported file must carry REQUIRED_FIELDS (id, type,
    agent, date) and a valid `type` (app/helpers/vault_frontmatter.py)."""
    import app.config
    monkeypatch.setattr(app.config.settings, "vault_path", tmp_path)

    board = await make_board()
    task = await make_task(board.id, status="done", title="Export Me")

    resp = await auth_client.post(f"/api/v1/tasks/{task.id}/run-record/to-vault")
    assert resp.status_code == 200
    body = resp.json()

    from pathlib import Path as _Path
    written_path = (
        tmp_path / body["path"]
        if not str(body["path"]).startswith(str(tmp_path))
        else _Path(body["path"])
    )

    from app.helpers.vault_frontmatter import parse_frontmatter, validate_frontmatter

    post = parse_frontmatter(written_path)
    validate_frontmatter(post.metadata)  # raises FrontmatterError if this would be quarantined

    assert post.metadata["agent"] == "system"  # runs/ has no agents/<slug>/ folder to own it
    assert "Auftrag" in post.content
    assert "Export Me" in post.content


@pytest.mark.asyncio
async def test_export_survives_real_vault_watcher(auth_client, make_board, make_task, tmp_path, monkeypatch):
    """Same live bug as above, reproduced through the actual VaultWatcher
    handler (pattern from tests/test_vault_watcher.py) rather than just
    the frontmatter validator — proves the file is not quarantined end to
    end, index.upsert() is called, and _rejected/ stays empty."""
    import app.config
    monkeypatch.setattr(app.config.settings, "vault_path", tmp_path)

    board = await make_board()
    task = await make_task(board.id, status="done", title="Watcher Survives Me")

    resp = await auth_client.post(f"/api/v1/tasks/{task.id}/run-record/to-vault")
    assert resp.status_code == 200
    body = resp.json()

    from pathlib import Path as _Path
    written_path = (
        tmp_path / body["path"]
        if not str(body["path"]).startswith(str(tmp_path))
        else _Path(body["path"])
    )

    from unittest.mock import AsyncMock, MagicMock
    from app.services.vault_watcher import VaultWatcher

    services = {
        "index": MagicMock(upsert=MagicMock()),
        "activity": MagicMock(track_view=AsyncMock(), track_write=AsyncMock()),
        "embeddings": MagicMock(upsert=AsyncMock(return_value={"ok": True})),
        "git": MagicMock(stage=MagicMock()),
        "redis": MagicMock(publish=AsyncMock()),
    }
    watcher = VaultWatcher(
        vault_path=tmp_path,
        index=services["index"],
        activity=services["activity"],
        embeddings=services["embeddings"],
        git=services["git"],
        redis=services["redis"],
    )

    await watcher._handle_create_or_modify(written_path)

    services["index"].upsert.assert_called_once()  # NOT quarantined
    rejected = tmp_path / "_rejected"
    assert not rejected.exists() or not any(rejected.iterdir())
