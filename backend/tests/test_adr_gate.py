"""ADR-Gate (2026-09-16): ein PR, der ein Entscheidungsdokument aendert, darf
nicht ohne explizite Operator-Freigabe des ADR-*Textes* gemergt werden.

Vorfall PR #602: `docs/decisions/084-omp-acp-harness-property.md` (loest
ADR-081 ab) wurde per Squash-Merge auf ein gruenes *Code*-Urteil hin
geschlossen, waehrend die Freigabe des *Textes* noch ausstand. Der PR trug null
Reviews — nichts im System hielt fest, dass der Text je freigegeben wurde. Die
Regel stand nur als Prosa im Handbook.

Sabotage in BEIDE Richtungen:
  - verbotener Fall (ADR geaendert, keine Freigabe)  → 409, Status bleibt review
  - erlaubter Fall (nur Code im PR)                  → Status wird done
  - erlaubter Fall (ADR-Text freigegeben)            → Status wird done
  - Freigabe wird durch eine Textaenderung entwertet  → wieder 409

Der Mutation-Guard am Ende pinnt die Kernbedingung fest: die Freigabe haengt am
Digest des Textes, nicht am blossen Vorhandensein irgendeines Approvals.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.auth import generate_agent_token
from app.models.agent import Agent
from app.models.approval import Approval
from app.models.board import Board, Project
from app.models.task import Task, TaskComment
from app.services.adr_gate import (
    ADR_GATE_ACTION_TYPE,
    AdrGateBlocked,
    adr_digest,
    evaluate_adr_gate,
    guard_adr_merge,
    pr_number_for_task,
)

from tests.conftest import test_engine

# ── Fixtures / Helfer ────────────────────────────────────────────────────

ADR_CANDIDATE = "docs/decisions/084-omp-acp-harness-property.md"
ADR_BODY_V1 = "# ADR-084 — ACP als Eigenschaft des omp-Harness\n\nFassung 1.\n"
ADR_BODY_V2 = "# ADR-084 — ACP als Eigenschaft des omp-Harness\n\nFassung 2.\n"
CODE_PATH = "backend/app/services/git_service.py"


async def _mk(objs: list):
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        for o in objs:
            s.add(o)
        await s.commit()
        for o in objs:
            await s.refresh(o)


async def _board_project(board_id) -> Project:
    project = Project(
        id=uuid.uuid4(),
        board_id=board_id,
        name=f"Proj-{uuid.uuid4().hex[:5]}",
        github_repo_name="argyelan-ai/mission-control",
        github_repo_url="https://github.com/argyelan-ai/mission-control.git",
    )
    await _mk([project])
    return project


async def _card(board_id, project_id, *, pr_number: int = 602) -> Task:
    task = Task(
        id=uuid.uuid4(),
        board_id=board_id,
        project_id=project_id,
        title="ADR-084 einfuehren",
        status="review",
        pr_number=pr_number,
    )
    await _mk([task])
    return task


def _gh(files: dict[str, str | None], head: str = "headsha123"):
    """Fake `_run_cmd`: bedient `gh pr view` und `gh api .../contents/...`.

    `files` bildet changed path → Inhalt am PR-Head ab (None = geloescht).
    """

    async def run_cmd(*args, **kwargs):
        if args[:3] == ("gh", "pr", "view"):
            import json

            return json.dumps(
                {
                    "headRefOid": head,
                    "files": [{"path": p} for p in files],
                }
            )
        if args[:2] == ("gh", "api"):
            target = args[2]
            for path, content in files.items():
                if f"contents/{path}?" in target:
                    if content is None:
                        raise RuntimeError(f"gh: Not Found ({path})")
                    return content
            raise RuntimeError(f"gh: Not Found ({target})")
        raise AssertionError(f"unerwarteter gh-Aufruf: {args}")

    return run_cmd


async def _guard(task, run_cmd):
    """Ruft den Waechter mit injiziertem `gh`-Ersatz auf (kein Netz)."""
    with patch(
        "app.services.git_service.git_service._run_cmd", new=run_cmd,
    ):
        async with AsyncSession(test_engine, expire_on_commit=False) as s:
            fresh = await s.get(Task, task.id)
            await guard_adr_merge(s, fresh, board_id=task.board_id)


async def _approve(task, digest: str):
    await _mk([
        Approval(
            id=uuid.uuid4(),
            board_id=task.board_id,
            task_id=task.id,
            action_type=ADR_GATE_ACTION_TYPE,
            description="ADR-Freigabe",
            payload={"digest": digest},
            status="approved",
        )
    ])


async def _seed_pair():
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project = await _board_project(board.id)
    task = await _card(board.id, project.id)
    return board, task


# ── Verbotener Fall ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_adr_change_without_approval_is_rejected(fake_redis):
    """Der Vorfall selbst: das PR aendert ein Entscheidungsdokument, es gibt
    keine Freigabe → 409 statt Merge, und ein freigebbarer Datensatz entsteht."""
    _board, task = await _seed_pair()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _guard(task, _gh({ADR_CANDIDATE: ADR_BODY_V1, CODE_PATH: "x = 1\n"}))

    assert exc.value.status_code == 409
    assert "ADR-084" in exc.value.detail

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        pending = (
            await s.exec(
                select(Approval).where(
                    Approval.task_id == task.id,
                    Approval.action_type == ADR_GATE_ACTION_TYPE,
                )
            )
        ).all()
        fresh = await s.get(Task, task.id)

    assert len(pending) == 1, "der Block muss eine aufloesbare Freigabe hinterlassen"
    assert pending[0].status == "pending"
    assert pending[0].payload["digest"] == adr_digest([(ADR_CANDIDATE, ADR_BODY_V1)])
    assert fresh.status == "review", "der Status darf nicht auf done springen"
    # Die Freigabe muss benennen, was freigegeben wird — nicht nur "ein PR".
    assert pending[0].payload["documents"] == [ADR_CANDIDATE]


# ── Erlaubte Faelle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_only_pr_passes(fake_redis):
    """Reiner Code-PR: kein Entscheidungsdokument beruehrt → kein Block.

    Ohne diese Richtung waere der Waechter ein Generalsperrer fuer jeden Merge.
    """
    _board, task = await _seed_pair()

    await _guard(task, _gh({CODE_PATH: "x = 1\n", "backend/README.md": "# hi\n"}))

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        approvals = (
            await s.exec(select(Approval).where(Approval.task_id == task.id))
        ).all()
    assert approvals == [], "Code-PR darf keine ADR-Freigabe anfordern"


@pytest.mark.asyncio
async def test_adr_change_with_approved_text_passes(fake_redis):
    """Freigegebener Text → Merge erlaubt; der Waechter kehrt ohne Exception zurueck."""
    _board, task = await _seed_pair()
    await _approve(task, adr_digest([(ADR_CANDIDATE, ADR_BODY_V1)]))

    await _guard(task, _gh({ADR_CANDIDATE: ADR_BODY_V1}))


@pytest.mark.asyncio
async def test_adr_deletion_needs_approval_too(fake_redis):
    """Ein geloeschtes Entscheidungsdokument ist genauso entscheidungsrelevant
    wie ein neues — der Waechter darf daran nicht vorbeilaufen."""
    _board, task = await _seed_pair()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _guard(task, _gh({ADR_CANDIDATE: None}))
    assert exc.value.status_code == 409


# ── Entwertung: Freigabe gilt fuer den TEXT, nicht fuer den PR ───────────


@pytest.mark.asyncio
async def test_editing_the_adr_after_approval_invalidates_it(fake_redis):
    """Kernbedingung: nach der Freigabe den Text aendern → wieder gesperrt.

    Sonst waere "freigeben lassen, dann umschreiben" ein Ein-Zeilen-Bypass.
    """
    _board, task = await _seed_pair()
    await _approve(task, adr_digest([(ADR_CANDIDATE, ADR_BODY_V1)]))
    await _guard(task, _gh({ADR_CANDIDATE: ADR_BODY_V1}))  # deckt Fassung 1 ab

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _guard(task, _gh({ADR_CANDIDATE: ADR_BODY_V2}))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_approval_for_a_different_task_does_not_unlock_this_one(fake_redis):
    """Eine Freigabe ist an die Karte gebunden: ein fremdes Approval mit
    gleichem Digest darf diesen Merge nicht oeffnen."""
    _board, task = await _seed_pair()

    other = await _seed_pair()
    await _approve(other[1], adr_digest([(ADR_CANDIDATE, ADR_BODY_V1)]))

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _guard(task, _gh({ADR_CANDIDATE: ADR_BODY_V1}))
    assert exc.value.status_code == 409


# ── Mutation-Guards: faellt eine Bedingung weg, wird der Waechter zahnlos ─


@pytest.mark.asyncio
async def test_mutation_guard_digest_is_content_bound(fake_redis):
    """Mutiert man den Digest zu "irgendein Digest", ist der Waechter zahnlos.

    Der Test pinnt fest, dass zwei verschiedene Texte verschiedene Digests
    ergeben — die Bedingung, an der `test_editing_the_adr_...` haengt.
    """
    assert adr_digest([(ADR_CANDIDATE, ADR_BODY_V1)]) != adr_digest(
        [(ADR_CANDIDATE, ADR_BODY_V2)]
    )
    # Reihenfolge der Dateien darf den Digest nicht veraendern (sonst haengt
    # die Freigabe an der Dateiliste der GitHub-API).
    a = adr_digest([(ADR_CANDIDATE, ADR_BODY_V1), (CODE_PATH, "y\n")])
    b = adr_digest([(CODE_PATH, "y\n"), (ADR_CANDIDATE, ADR_BODY_V1)])
    assert a == b


@pytest.mark.asyncio
async def test_mutation_guard_verdict_never_silently_passes(fake_redis):
    """Der Verdikt-Pfad darf nicht schlucken: eine erkannte ADR-Aenderung ohne
    Freigabe wirft — auch wenn die Freigabe-Anlage selbst fehlschlaegt."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    task = await _card(board.id, None)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        with patch(
            "app.services.autonomy.enforce_autonomy",
            new=AsyncMock(side_effect=RuntimeError("DB weg")),
        ):
            with pytest.raises(AdrGateBlocked):
                await evaluate_adr_gate(
                    s,
                    [(ADR_CANDIDATE, ADR_BODY_V1)],
                    repo_full_name="argyelan-ai/mission-control",
                    pr_number=602,
                    task_id=task.id,
                )


@pytest.mark.asyncio
async def test_unreachable_gh_passes_loudly_not_silently(fake_redis):
    """Ist `gh` nicht erreichbar, darf der Merge nicht die ganze Board-
    Fertigstellung sperren — aber der Durchlauf muss als Event sichtbar sein,
    damit "kein ADR" von "nie geprueft" unterscheidbar bleibt."""
    _board, task = await _seed_pair()

    async def broken(*args, **kwargs):
        raise RuntimeError("gh: not authenticated")

    with patch("app.services.activity.emit_event", new_callable=AsyncMock) as emit:
        await _guard(task, broken)

    assert emit.await_count == 1
    assert emit.await_args.args[1] == "adr_gate_check_unavailable"


# ── PR-Ermittlung ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_number_prefers_field_then_falls_back_to_comment(fake_redis):
    """Die Karte traegt die PR-Nummer im Feld; fehlt sie, greift derselbe
    Kommentar-Marker, den die Merge-Helfer nutzen (Pitfall H)."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    from_field = await _card(board.id, None, pr_number=148)

    legacy = await _card(board.id, None, pr_number=None)
    await _mk([
        TaskComment(
            id=uuid.uuid4(),
            task_id=legacy.id,
            author_type="system",
            comment_type="progress",
            content="**PR erstellt:** https://github.com/argyelan-ai/mission-control/pull/537",
        )
    ])
    no_pr = await _card(board.id, None, pr_number=None)

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        assert await pr_number_for_task(s, await s.get(Task, from_field.id)) == 148
        assert await pr_number_for_task(s, await s.get(Task, legacy.id)) == 537
        assert await pr_number_for_task(s, await s.get(Task, no_pr.id)) is None


@pytest.mark.asyncio
async def test_card_without_project_is_out_of_scope(fake_redis):
    """Karte ohne Projekt → kein Merge-Pfad → kein Block, kein gh-Aufruf."""
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    task = await _card(board.id, None, pr_number=602)

    async def forbidden(*args, **kwargs):
        raise AssertionError("ohne Projekt darf kein gh-Aufruf erfolgen")

    await _guard(task, forbidden)


# ── Der Waechter sitzt vor dem Commit, nicht im schluckenden Merge-Block ─


@pytest.mark.asyncio
async def test_agent_patch_done_is_blocked_before_the_status_commits(client, fake_redis):
    """Ende-zu-Ende am PATCH-Pfad: der Agent setzt done auf einer Karte mit
    ADR-PR → 409 UND der Status bleibt review.

    Das ist die eigentliche Anforderung: eine Ablehnung, die den Zustand nicht
    heimlich doch aendert, ist keine Ablehnung.
    """
    board = Board(id=uuid.uuid4(), name="B", slug=f"b-{uuid.uuid4().hex[:6]}")
    await _mk([board])
    project = await _board_project(board.id)
    task = await _card(board.id, project.id)

    token, token_hash = generate_agent_token()
    agent = Agent(
        id=uuid.uuid4(),
        name=f"Reviewer-{uuid.uuid4().hex[:5]}",
        board_id=board.id,
        agent_token_hash=token_hash,
        scopes=["tasks:read", "tasks:write"],
        role="reviewer",
        provision_status="provisioned",
    )
    await _mk([agent])

    with patch(
        "app.services.git_service.git_service._run_cmd",
        new=_gh({ADR_CANDIDATE: ADR_BODY_V1}),
    ):
        response = await client.patch(
            f"/api/v1/agent/boards/{board.id}/tasks/{task.id}",
            json={"status": "done"},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 409, response.text
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        fresh = await s.get(Task, task.id)
    assert fresh.status == "review", "409 darf den Status nicht bereits gesetzt haben"
