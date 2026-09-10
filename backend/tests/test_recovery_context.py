"""
Tests for build_recovery_context() — rich recovery context from task comments.

TDD: tests first, implementation after.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import Board
from app.models.task import Task, TaskComment


# ── Helper function: create comment ─────────────────────────────────


async def _create_comment(
    session: AsyncSession,
    task_id: uuid.UUID,
    comment_type: str = "progress",
    content: str = "Test comment",
    created_at: datetime | None = None,
    author_type: str = "agent",
) -> TaskComment:
    """Create a TaskComment in the DB and return it."""
    comment = TaskComment(
        id=uuid.uuid4(),
        task_id=task_id,
        author_type=author_type,
        comment_type=comment_type,
        content=content,
        created_at=created_at or datetime.utcnow(),
    )
    session.add(comment)
    await session.commit()
    await session.refresh(comment)
    return comment


# ── Helper function: board + task setup ──────────────────────────────────


async def _setup_board_and_task(
    session: AsyncSession,
    assigned_agent_id: uuid.UUID | None = None,
) -> Task:
    """Create board + task and return the task."""
    board = Board(id=uuid.uuid4(), name="Test Board", slug=f"test-{uuid.uuid4().hex[:8]}")
    session.add(board)
    await session.commit()

    task = Task(
        id=uuid.uuid4(),
        board_id=board.id,
        title="Recovery Test Task",
        assigned_agent_id=assigned_agent_id,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_context_returns_none_without_comments(session: AsyncSession):
    """Without comments → return None."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    result = await build_recovery_context(session, task)
    assert result is None


@pytest.mark.asyncio
async def test_recovery_context_includes_progress_comments(session: AsyncSession):
    """Progress comments appear under 'Latest Progress'.

    Workstream A4: `checkpoint` comments no longer exist — migration 0082
    moved them into `progress`, and new code posts `progress` via
    `mc comment progress`.
    """
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    await _create_comment(session, task.id, "progress", "Schritt 1 erledigt", now - timedelta(minutes=30))
    await _create_comment(session, task.id, "progress", "Models erstellt", now - timedelta(minutes=20))
    await _create_comment(session, task.id, "progress", "Tests geschrieben", now - timedelta(minutes=10))

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Recovery" in result
    assert "Schritt 1 erledigt" in result
    assert "Models erstellt" in result
    assert "Tests geschrieben" in result
    assert "progress" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_blocker(session: AsyncSession):
    """Blocker comment is shown with the BLOCKER label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "blocker", "Warte auf API-Key")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "BLOCKER" in result
    assert "Warte auf API-Key" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_feedback(session: AsyncSession):
    """Reviewer feedback is shown with the REVIEWER-FEEDBACK label."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "feedback", "Tests fehlen fuer Edge-Cases")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "REVIEWER-FEEDBACK" in result
    assert "Tests fehlen fuer Edge-Cases" in result


@pytest.mark.asyncio
async def test_recovery_context_limits_to_3_comments(session: AsyncSession):
    """10 comments → only the newest 3 appear (Nacharbeit-2 PR #489: 5 -> 3,
    um Platz fuer den hoeheren Anweisungs-Cap freizumachen)."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    for i in range(10):
        prefix = "OLD" if i < 7 else "NEW"
        await _create_comment(
            session,
            task.id,
            "progress",
            f"Fortschritt {prefix}-{i:02d}",
            now - timedelta(minutes=10 - i),
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    # The oldest 7 (OLD-00 through OLD-06) should NOT be included
    for i in range(7):
        assert f"Fortschritt OLD-{i:02d}" not in result
    # The newest 3 (NEW-07 through NEW-09) should be included
    for i in range(7, 10):
        assert f"Fortschritt NEW-{i:02d}" in result


@pytest.mark.asyncio
async def test_recovery_context_includes_workspace_info(session: AsyncSession):
    """Agent with workspace_path → path in the result."""
    from app.services.dispatch import build_recovery_context

    agent = Agent(
        id=uuid.uuid4(),
        name="Cody",
        workspace_path="/home/henry/.openclaw/workspace-cody",
    )
    session.add(agent)
    await session.commit()

    task = await _setup_board_and_task(session, assigned_agent_id=agent.id)
    await _create_comment(session, task.id, "progress", "Arbeite am Feature")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "/home/henry/.openclaw/workspace-cody" in result
    assert "Workspace" in result


@pytest.mark.asyncio
async def test_recovery_context_ignores_message_type(session: AsyncSession):
    """Comments of type 'message' are NOT included."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "message", "Hallo, wie geht's?")

    result = await build_recovery_context(session, task)

    # Only message comments → no recovery context
    assert result is None


@pytest.mark.asyncio
async def test_recovery_context_includes_sixth_operator_comment(session: AsyncSession):
    """W0.3: 6 Operator-Kommentare vor einem Requeue -> der 6. (neueste) steht im Prompt.

    Vorher rot gegen die alte Funktion verifiziert (relevant_types kannte weder
    `message` noch `handoff` -> alle 6 fielen komplett raus, `result is None`).
    Prueft nebenbei auch die Postfach-Hinweiszeile (DoD-Punkt 1): von 6
    Operator-Kommentaren werden nur die letzten 3 ungekuerzt gezeigt, die
    Hinweiszeile muss die echte Zahl der uebrigen 3 nennen.
    """
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    for i in range(1, 7):
        await _create_comment(
            session,
            task.id,
            "message",
            f"Anweisung {i}: mach X statt Y",
            now - timedelta(minutes=60 - i),
            author_type="user",
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Anweisung 6: mach X statt Y" in result
    # W4 (Nacharbeit PR #489): `assert "3" in result` war zahnlos — die "3"
    # steckt auch in Zeitstempeln und der Task-UUID, ein falscher Zaehlwert
    # (z.B. wegen M2) waere hier nicht aufgefallen. Voller erwarteter Satz.
    assert (
        f"**Postfach:** 3 weitere Anweisungen/Fortschrittseintraege nicht in "
        f"diesem Kontext -> `mc task-get {task.id}`"
    ) in result


@pytest.mark.asyncio
async def test_recovery_context_operator_comment_full_multiline_content(session: AsyncSession):
    """Ein mehrzeiliger Operator-Kommentar kommt vollstaendig an, nicht nur Zeile 1."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    multiline = (
        "Bitte zuerst die Migration pruefen.\n"
        "Danach den Endpunkt gegen den neuen Vertrag testen.\n"
        "Erst wenn beides gruen ist: PR aufmachen."
    )
    await _create_comment(session, task.id, "message", multiline, author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert multiline in result


@pytest.mark.asyncio
async def test_recovery_context_single_overlong_operator_comment_gets_truncated_not_dropped(
    session: AsyncSession,
):
    """B1 (Nacharbeit PR #489): der alte Gesamt-Cap griff bei genau einem
    uebrig gebliebenen Kommentar gar nicht — die Schleife brach ab statt zu
    kuerzen (`len(remaining) <= 1`). Rex hat gemessen: ein einzelner
    20000-Zeichen-Operator-Kommentar ergab 20423 Zeichen Recovery-Kontext.
    Fix: Per-Kommentar-Cap mit sichtbarem `[...gekuerzt]`-Marker (Idiom aus
    `_load_feedback()`). Der Kommentar darf nicht mehr vollstaendig
    durchschlagen, aber auch nicht spurlos verschwinden.

    Nacharbeit-3 PR #489: bei genau einem Kommentar ist dieser automatisch
    der juengste -> Cap ist jetzt OPERATOR_LEAD_LATEST_MAX_CHARS (1200), nicht
    mehr der alte einheitliche Per-Item-Cap (800).
    """
    from app.services.dispatch import build_recovery_context
    from app.services.task_context_builder import OPERATOR_LEAD_LATEST_MAX_CHARS

    task = await _setup_board_and_task(session)

    huge = "X" * 20_000
    await _create_comment(session, task.id, "message", huge, author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    # Nicht der volle 20000-Zeichen-Kommentar landet im Kontext ...
    assert huge not in result
    assert len(result) < 2000
    # ... aber der Anfang schon (Kommentar verschwindet nicht komplett) plus
    # sichtbarer Kuerzungsmarker und Verweis, wo der Rest steht.
    assert "X" * OPERATOR_LEAD_LATEST_MAX_CHARS in result
    assert "[...gekuerzt]" in result
    assert f"mc task-get {task.id}" in result


@pytest.mark.asyncio
async def test_recovery_context_operator_block_caps_each_comment_not_whole_block(
    session: AsyncSession,
):
    """M2 (Nacharbeit PR #489): mehrere ueberlange Kommentare -> jeder muss im
    Prompt auftauchen (gekuerzt), keiner darf komplett fallen. Das alte
    Alles-oder-nichts liess bei Ueberschreitung des Gesamt-Caps die aelteste
    Anweisung komplett verschwinden; `shown_count` zaehlte trotzdem die
    ungekuerzte Rohliste, wodurch die Postfach-Zeile und die tatsaechlich
    gezeigten Kommentare auseinanderliefen.

    Nacharbeit-2 PR #489: bei COMMENT_LIMIT=3 und PER_ITEM_MAX_CHARS=800
    sprengten 3 gleichzeitig ueberlange Kommentare rechnerisch immer den
    Gesamt-Cap (3 * 814 Zeichen (Cap + Kuerzungsmarker) + Header > 1800) —
    das war der zweite Befund aus der Rueckfrage an den Operator.

    Nacharbeit-3 PR #489: der Operator hat entschieden, dass die Verteilung
    falsch war, nicht die Obergrenze — juengster Kommentar bis 1200, aeltere
    je 250. Dieser Test verifiziert jetzt genau das: die beiden aelteren
    Kommentare (oldest, middle) werden auf OPERATOR_LEAD_OLDER_MAX_CHARS
    gekuerzt, der juengste bleibt unangetastet (er ist ohnehin kurz).
    """
    from app.services.dispatch import build_recovery_context
    from app.services.task_context_builder import OPERATOR_LEAD_OLDER_MAX_CHARS

    task = await _setup_board_and_task(session)

    now = datetime.utcnow()
    oldest = "A" * 900
    middle = "B" * 900
    newest = "C" * 30
    await _create_comment(session, task.id, "message", oldest, now - timedelta(minutes=30), author_type="user")
    await _create_comment(session, task.id, "handoff", middle, now - timedelta(minutes=20), author_type="agent")
    await _create_comment(session, task.id, "message", newest, now - timedelta(minutes=10), author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    # Alle 3 sind vertreten (per-Kommentar-Anfang), keiner ist spurlos weg —
    # das war genau der Bug, den B1/M2 beheben.
    assert "A" * OPERATOR_LEAD_OLDER_MAX_CHARS in result
    assert "B" * OPERATOR_LEAD_OLDER_MAX_CHARS in result
    assert newest in result
    # Aber die ueberlangen nicht ungekuerzt (jeder ist laenger als der aeltere Cap).
    assert oldest not in result
    assert middle not in result
    assert result.count("[...gekuerzt]") == 2
    assert f"mc task-get {task.id}" in result
    # Es gibt keinen Postfach-Hinweis mehr — alle 3 Operator-Kommentare
    # (== Gesamtzahl passend zum Filter) sind tatsaechlich im Prompt, nur
    # gekuerzt, nicht weggelassen.
    assert "**Postfach:**" not in result


@pytest.mark.asyncio
async def test_recovery_context_excludes_system_generated_message_and_handoff(session: AsyncSession):
    """Automatisch erzeugte system-Kommentare (author_type='system') sind keine
    Anweisungen und duerfen nicht im Operator-/Lead-Block auftauchen — selbst
    wenn ihr comment_type zufaellig 'message' oder 'handoff' ist (z.B. der
    System-Handoff beim Human-Review-Uebergang, oder der System-Message-
    Callback bei Subtask-Abschluss)."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    await _create_comment(
        session, task.id, "handoff",
        "Human-Review angefordert fuer 'X' — wartet auf Mark (kein Agent-Reviewer dispatcht).",
        author_type="system",
    )
    await _create_comment(
        session, task.id, "message",
        "Callback: Root-Task abgeschlossen (done).",
        author_type="system",
    )
    # Ein echter Operator-Kommentar muss trotzdem durchkommen.
    await _create_comment(session, task.id, "message", "Echte Anweisung vom Operator", author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Human-Review angefordert" not in result
    assert "Callback: Root-Task abgeschlossen" not in result
    assert "Echte Anweisung vom Operator" in result


# ── Nacharbeit-2 PR #489: Checkliste (nur offen, max 10) + Reihenfolge ─────


async def _create_checklist_item(
    session: AsyncSession,
    task_id: uuid.UUID,
    title: str,
    status: str,
    sort_order: int,
):
    from app.models.checklist import TaskChecklistItem

    item = TaskChecklistItem(
        id=uuid.uuid4(), task_id=task_id, title=title, status=status, sort_order=sort_order,
    )
    session.add(item)
    await session.commit()
    return item


@pytest.mark.asyncio
async def test_recovery_context_checklist_hides_done_items(session: AsyncSession):
    """Erledigte Checklist-Items gehoeren nicht in den Recovery-Prompt — nur
    offene (pending/in_progress) werden gezeigt."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_checklist_item(session, task.id, "Erledigt A", "done", 0)
    await _create_checklist_item(session, task.id, "Erledigt B", "done", 1)
    await _create_checklist_item(session, task.id, "Offen C", "pending", 2)

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Erledigt A" not in result
    assert "Erledigt B" not in result
    assert "Offen C" in result
    assert "← **HIER WEITERMACHEN**" in result


@pytest.mark.asyncio
async def test_recovery_context_checklist_caps_open_items_at_10(session: AsyncSession):
    """40 Checklist-Items, 28 davon done verstreut, 12 offen -> nur die ersten
    10 offenen werden gezeigt, danach eine Hinweiszeile mit der echten Zahl
    der uebrigen (2). Done-Items zaehlen nirgends mit."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    # Jedes 3. Item bleibt offen (13 Positionen von 40 sind offen -> 12 offen
    # + 1 done je Dreiergruppe passt nicht exakt; wir bauen es explizit:
    # 28 done + 12 pending, done vor jedem 3. pending gemischt).
    order = 0
    open_count = 0
    for i in range(40):
        if i % 10 < 7:  # 7 von 10 done, 3 von 10 offen -> 28 done / 12 offen
            status = "done"
        else:
            status = "pending"
            open_count += 1
        await _create_checklist_item(session, task.id, f"Item {i:02d} ({status})", status, order)
        order += 1
    assert open_count == 12

    result = await build_recovery_context(session, task)

    assert result is not None
    assert result.count("[ ]") == 10
    assert "... und 2 weitere" in result
    assert f"mc task-get {task.id}" in result
    # DoD (Nacharbeit-2 PR #489): "40 Checklist-Eintraege -> Kontext bleibt
    # unter 2500 Zeichen" — dieses Szenario (Checkliste ohne Kommentare) ist
    # der exakte Wortlaut dieses DoD-Punkts.
    assert len(result) < 2500, f"Checklist-only context zu gross: {len(result)} Zeichen"


@pytest.mark.asyncio
async def test_recovery_context_checklist_hint_names_done_count_too(session: AsyncSession):
    """B6 (Nacharbeit-4 PR #489): 28 erledigte / 12 offene -> die Hinweiszeile
    nennt beide Zahlen, nicht nur die verdeckten offenen. Vorher stand ueber
    die 28 erledigten kein Wort, obwohl sie komplett aus `open_items`
    herausgefiltert wurden, bevor irgendeine Zaehlung ansetzt."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    order = 0
    for i in range(40):
        status = "done" if i % 10 < 7 else "pending"
        await _create_checklist_item(session, task.id, f"Item {i:02d}", status, order)
        order += 1

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "- ... und 2 weitere offene, 28 erledigte" in result
    assert f"mc task-get {task.id}" in result


@pytest.mark.asyncio
async def test_recovery_context_checklist_hint_shows_done_even_without_hidden_open(
    session: AsyncSession,
):
    """B6: alle offenen Items passen ins Limit (keine verdeckten offenen),
    aber es gibt erledigte Items — die Hinweiszeile muss trotzdem erscheinen,
    sonst verschwinden die Erledigten weiterhin spurlos."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_checklist_item(session, task.id, "Erledigt A", "done", 0)
    await _create_checklist_item(session, task.id, "Erledigt B", "done", 1)
    await _create_checklist_item(session, task.id, "Offen C", "pending", 2)

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Offen C" in result
    assert "- ... und 2 erledigte" in result
    assert "weitere offene" not in result  # keine verdeckten offenen Items


@pytest.mark.asyncio
async def test_recovery_context_progress_snippet_marks_truncation_over_180_chars(
    session: AsyncSession,
):
    """B7 (Nacharbeit-4 PR #489): der Fortschritts-Block kuerzte bisher auf
    Zeile 1 + 180 Zeichen, ohne Marker — wortwoertlich derselbe Bug wie im
    Anweisungs-Block, nur eine Zeile tiefer. Jetzt bekommt ein gekuerzter
    Eintrag denselben sichtbaren Marker plus Verweis."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    long_line = "Y" * 250
    await _create_comment(session, task.id, "progress", long_line)

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Y" * 180 in result
    assert long_line not in result
    assert "[...gekuerzt]" in result
    assert f"mc task-get {task.id}" in result


@pytest.mark.asyncio
async def test_recovery_context_progress_snippet_marks_truncation_on_multiline(
    session: AsyncSession,
):
    """B7: auch das stille Abschneiden nachfolgender Zeilen (Zeile 1 ist kurz,
    aber es gibt eine Zeile 2+) ist Kuerzung und muss sichtbar sein."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    multiline = "Kurze erste Zeile.\nZweite Zeile mit weiterem Kontext."
    await _create_comment(session, task.id, "progress", multiline)

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Kurze erste Zeile." in result
    assert "Zweite Zeile mit weiterem Kontext." not in result
    assert "[...gekuerzt]" in result
    assert f"mc task-get {task.id}" in result


@pytest.mark.asyncio
async def test_recovery_context_progress_snippet_no_marker_when_short(session: AsyncSession):
    """B7 Gegenprobe: ein kurzer einzeiliger Fortschritts-Kommentar bleibt wie
    er ist — kein Marker, wenn nichts gekuerzt wurde."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_comment(session, task.id, "progress", "Kurzer Status.")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert "Kurzer Status." in result
    assert "[...gekuerzt]" not in result


@pytest.mark.asyncio
async def test_recovery_context_block_order_instructions_checklist_progress(session: AsyncSession):
    """Nacharbeit-2 PR #489, Scope-Punkt 4: Reihenfolge im Recovery-Kontext
    ist Anweisungen -> Checkliste -> Fortschritt. Was der Agent tun soll,
    steht oben."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)
    await _create_checklist_item(session, task.id, "Offener Schritt", "pending", 0)
    await _create_comment(session, task.id, "progress", "Fortschritt geleistet")
    await _create_comment(session, task.id, "message", "Mach X statt Y", author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    idx_instructions = result.index("### Operator-/Lead-Anweisungen")
    idx_checklist = result.index("### Deine Checkliste")
    idx_progress = result.index("### Letzter Fortschritt")
    assert idx_instructions < idx_checklist < idx_progress


@pytest.mark.asyncio
async def test_recovery_context_typical_multipart_instruction_survives_complete(
    session: AsyncSession,
):
    """Nacharbeit-2 PR #489: eine typische mehrteilige Anweisung (6 kurze
    nummerierte Schritte, hier ~740 Zeichen) kommt vollstaendig an, nicht nur
    Schritt 1 + Haelfte von Schritt 2 wie beim alten 180/250-Zeichen-Cap.

    Nacharbeit-3 PR #489: als einziger (= juengster) Kommentar liegt sie
    unter OPERATOR_LEAD_LATEST_MAX_CHARS (1200) statt dem frueheren
    einheitlichen Per-Item-Cap (800) — bleibt also weiterhin unangetastet."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    steps = "\n".join(
        f"{i}. " + ("Detail zu Schritt " + str(i) + " ") * 6 for i in range(1, 7)
    ).rstrip()  # build_recovery_context strips comment content before rendering it
    assert len(steps) < 800, f"Fixture zu lang fuer den Test: {len(steps)}"
    await _create_comment(session, task.id, "message", steps, author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert steps in result
    assert "[...gekuerzt]" not in result


@pytest.mark.asyncio
async def test_recovery_context_overlong_2000_char_instruction_capped_at_1200_with_pointer(
    session: AsyncSession,
):
    """DoD (Nacharbeit-3 PR #489, korrigiert gegenueber der urspruenglichen
    Karte): der Punkt 'eine 2000-Zeichen-Anweisung kommt vollstaendig an' war
    mathematisch unmoeglich, sobald ein Per-Item-Cap existiert. Der korrigierte
    Wortlaut: eine 2000-Zeichen-Anweisung mit sechs nummerierten Schritten
    kommt mit ihren ersten 1200 Zeichen an (sie ist als einziger Kommentar
    automatisch die juengste -> OPERATOR_LEAD_LATEST_MAX_CHARS), gefolgt vom
    `[...gekuerzt]`-Marker UND einem ausdruecklichen Verweis, wo der Rest
    steht (`mc task-get <id>`). Gekuerzt ist in Ordnung, stillschweigend
    gekuerzt nicht."""
    from app.services.dispatch import build_recovery_context
    from app.services.task_context_builder import OPERATOR_LEAD_LATEST_MAX_CHARS

    task = await _setup_board_and_task(session)

    six_steps = "\n".join(f"{i}. " + ("Schritt-Detail " * 30) for i in range(1, 7))
    assert len(six_steps) >= 2000, f"Fixture zu kurz: {len(six_steps)}"
    await _create_comment(session, task.id, "message", six_steps, author_type="user")

    result = await build_recovery_context(session, task)

    assert result is not None
    assert six_steps not in result
    assert six_steps[:OPERATOR_LEAD_LATEST_MAX_CHARS] in result
    assert "[...gekuerzt]" in result
    assert f"mc task-get {task.id}" in result


@pytest.mark.asyncio
async def test_recovery_context_heavy_scenario_measured_for_pr_text(session: AsyncSession):
    """Scope-Punkt 5 / DoD 'Messwert im PR-Text' (Nacharbeit-2 PR #489):
    schweres Szenario — 40 Checklist-Eintraege (davon 12 offen), 5
    Fortschritts-Kommentare (nur die neuesten 3 werden gerendert), 3
    Operator-/Lead-Anweisungen am Per-Item-Cap.

    Nacharbeit-3 PR #489 (Aufloesung der Rueckfrage aus Thread 8a913a45):
    der gleiche 800er-Cap fuer alle drei Kommentare war der Fehler — 3 x 800
    = 2400 Rohzeichen gegen den 1800er Gesamt-Cap konnte nie aufgehen, der
    Gesamt-Cap griff dadurch strukturell immer statt als Notnetz. Der
    Operator hat entschieden: nicht die Obergrenze war falsch, sondern die
    Verteilung — juengster Kommentar bis OPERATOR_LEAD_LATEST_MAX_CHARS
    (1200), die beiden aelteren je OPERATOR_LEAD_OLDER_MAX_CHARS (250). In
    diesem Fixture sind die beiden aelteren Anweisungen (~840 Zeichen roh)
    deutlich laenger als ihr 250er-Cap und werden gekuerzt, die juengste
    (~840 Zeichen) bleibt unter ihrem 1200er-Cap vollstaendig erhalten.
    Gemessener Wert nach dem Fix: 2678 Zeichen (vorher, mit dem gleichen
    800er-Cap fuer alle drei: ~2900 Zeichen) — deutlich unter dem alten,
    ungedeckelten Verhalten, das bei einem einzelnen ueberlangen Kommentar
    allein >20000 Zeichen erreichen konnte."""
    from app.services.dispatch import build_recovery_context

    task = await _setup_board_and_task(session)

    order = 0
    for i in range(40):
        status = "done" if i % 10 < 7 else "pending"
        await _create_checklist_item(session, task.id, f"Item {i:02d}", status, order)
        order += 1

    now = datetime.utcnow()
    for i in range(5):
        await _create_comment(
            session, task.id, "progress",
            f"Fortschritt {i:02d}: einiges an Arbeit erledigt, Details siehe Commit-Historie.",
            now - timedelta(minutes=50 - i * 10),
        )

    for i in range(3):
        await _create_comment(
            session, task.id, "message",
            ("Schritt " + str(i + 1) + "-Anweisung: ") * 40,
            now - timedelta(minutes=20 - i * 5),
            author_type="user",
        )

    result = await build_recovery_context(session, task)

    assert result is not None
    # Regressions-Absicherung: deutlich unter dem alten ungedeckelten
    # Verhalten (ein einzelner ueberlanger Kommentar allein konnte vorher
    # >20000 Zeichen ergeben). Gemessener Wert nach der ungleichen Verteilung
    # (Nacharbeit-3 PR #489, siehe PR-Text): 2678 Zeichen — Grenze mit etwas
    # Puffer statt exakt am Messwert, damit der Test nicht bei jeder
    # Nachkomma-Abweichung flackert.
    assert len(result) < 2900, f"Heavy-scenario context zu gross: {len(result)} Zeichen"
    assert "[...gekuerzt]" in result
    # Messwert fuer den PR-Text protokollieren.
    print(f"\n[Nacharbeit-3 PR #489] Heavy-scenario Recovery-Kontext: {len(result)} Zeichen")


@pytest.mark.asyncio
async def test_agent_dispatch_config_defaults(session: AsyncSession):
    """dispatch_config defaults to empty dict."""
    agent = Agent(name="TestAgent")
    session.add(agent)
    await session.flush()

    loaded = await session.get(Agent, agent.id)
    assert loaded.dispatch_config == {}


# ── Tests for _get_agent_timeout ─────────────────────────────────────


def test_get_agent_timeout_returns_default():
    """Without dispatch_config, the global default is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="TestAgent")
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 30


def test_get_agent_timeout_returns_agent_value():
    """With dispatch_config, the agent value is returned."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "stale_progress_minutes", 30) == 45


def test_get_agent_timeout_falls_back_on_missing_key():
    """Missing key in dispatch_config falls back to the default."""
    from app.services.task_runner import _get_agent_timeout

    agent = Agent(name="Cody", dispatch_config={"stale_progress_minutes": 45})
    assert _get_agent_timeout(agent, "ack_timeout_minutes", 10) == 10
