"""ADR-Gate — a PR touching a decision document is not mergeable without the
operator's explicit approval of that ADR *text*.

Incident (2026-09-16, PR #602): `docs/decisions/084-omp-acp-harness-property.md`
was squash-merged — superseding ADR-081 — on a green *code* verdict while
operator approval of the ADR text was still pending. The PR carried zero
reviews, so nothing in the system recorded that the *text* was ever approved.
The rule existed only as prose in the handbook.

This module makes it mechanical. Three properties matter, each chosen against
a specific way the rule can be dodged:

1. **The unit of approval is the ADR text, not the PR.** The operator approves
   a digest over the changed decision documents; the merge is refused unless an
   approved `adr_gate` record carries *that* digest. Editing the ADR after
   approval invalidates it — otherwise "approve then rewrite" would be a
   one-line bypass.
2. **The decision-document test is content-based** (`decision_docs.py`), so
   relocating an ADR does not slip past a path filter.
3. **The refusal is loud.** `AdrGateBlocked` is raised, never logged. All three
   merge call sites currently convert exceptions into
   `logger.warning("PR-Merge fehlgeschlagen")` — placing the gate *inside*
   those blocks would have created exactly the silent failure this task exists
   to remove. Callers must call this ahead of (or outside) that try-block.

The GitHub access goes through the caller's `run_cmd` (`git_service._run_cmd`),
so the guard uses the same authenticated `gh` binding as the merge it guards,
and tests can drive it without the network.
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.services.decision_docs import (
    adr_signature_number,
    decision_doc_number,
    is_decision_doc,
)

logger = logging.getLogger("mc.adr_gate")

# action_type for the approval that unblocks a merge. Not in AUTONOMY_DEFAULTS
# on purpose: `resolve_autonomy` already defaults unknown action types to L3
# (safest), so no migration and no autonomy config entry are needed.
ADR_GATE_ACTION_TYPE = "adr_gate"

DELETED_CONTENT_MARKER = "<deleted-or-unreadable>"


class AdrGateBlocked(RuntimeError):
    """Raised when a merge is refused because an ADR needs operator approval.

    Deliberately NOT a silent-log path: it must reach the caller's user-visible
    error surface. Carries the approval id so the operator can resolve it.
    """

    def __init__(self, message: str, *, approval_id=None, digest: str = ""):
        super().__init__(message)
        self.approval_id = approval_id
        self.digest = digest


def adr_digest(entries: list[tuple[str, str | None]]) -> str:
    """Stable digest of the decision-document payload a merge would land.

    `entries` is [(repo_path, content)] with `content=None` for a file whose
    body could not be read at the PR head (deleted or unreadable). Sorting
    makes the digest independent of the API's file ordering. A deletion is
    folded in by its marker, so deleting an ADR also invalidates a prior
    approval — removing a decision document is a decision too.
    """
    normalized = [
        [path, hashlib.sha256((content if content is not None else DELETED_CONTENT_MARKER).encode("utf-8")).hexdigest()]
        for path, content in entries
    ]
    normalized.sort(key=lambda item: item[0])
    blob = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


async def fetch_pr_files(run_cmd, repo_full_name: str, pr_number: int) -> tuple[str, list[str]]:
    """(head_sha, changed paths) for a PR. Raises if `gh` itself fails."""
    raw = await run_cmd(
        "gh", "pr", "view", str(pr_number),
        "--repo", repo_full_name,
        "--json", "files,headRefOid",
    )
    data = json.loads(raw)
    head_sha = data.get("headRefOid") or ""
    paths = [entry.get("path", "") for entry in data.get("files", []) if entry.get("path")]
    return head_sha, paths


async def fetch_file_content(run_cmd, repo_full_name: str, path: str, ref: str) -> str | None:
    """File body at `ref`, or None when it does not exist there.

    None is meaningful (deleted/unreadable), not an error: `is_decision_doc`
    treats a vanished candidate as still decision-relevant.
    """
    try:
        return await run_cmd(
            "gh", "api",
            f"repos/{repo_full_name}/contents/{path}?ref={ref}",
            "-H", "Accept: application/vnd.github.raw",
        )
    except Exception as exc:  # noqa: BLE001 — a 404 on a deleted path is expected
        logger.info("ADR-Gate: Inhalt von %s@%s nicht lesbar (%s)", path, ref, exc)
        return None


async def collect_decision_docs(
    run_cmd, repo_full_name: str, pr_number: int,
) -> tuple[list[tuple[str, str | None]], str]:
    """The decision documents this PR would change, and the PR head SHA.

    Empty list ⇒ the merge is not an ADR change and must pass untouched.
    """
    head_sha, paths = await fetch_pr_files(run_cmd, repo_full_name, pr_number)
    entries: list[tuple[str, str | None]] = []
    for path in paths:
        # Cheap path pre-filter only; the verdict below is content-based.
        if decision_doc_number(path) is None:
            continue
        content = await fetch_file_content(run_cmd, repo_full_name, path, head_sha) if head_sha else None
        if is_decision_doc(content, path):
            entries.append((path, content))
    return entries, head_sha


def describe_entries(entries: list[tuple[str, str | None]]) -> str:
    """Human-readable list for the approval description."""
    parts = []
    for path, content in entries:
        number = decision_doc_number(path) or (adr_signature_number(content) if content else None) or "?"
        parts.append(f"ADR-{number} ({path})" if number != "?" else path)
    return ", ".join(parts)


async def find_approved_adr_gate(session, *, digest: str, task_id=None, board_id=None):
    """An approved `adr_gate` record whose digest matches the current ADR text."""
    from sqlmodel import select

    from app.models.approval import Approval

    statement = select(Approval).where(
        Approval.action_type == ADR_GATE_ACTION_TYPE,
        Approval.status == "approved",
    )
    if task_id is not None:
        statement = statement.where(Approval.task_id == task_id)
    elif board_id is not None:
        statement = statement.where(Approval.board_id == board_id)
    else:
        return None

    result = await session.exec(statement)
    for approval in result.all():
        payload = approval.payload or {}
        if payload.get("digest") == digest:
            return approval
    return None


async def find_pending_adr_gate(session, *, digest: str, task_id=None):
    """An already-open request for exactly this text — avoids duplicates when
    several done-paths race for the same task."""
    from sqlmodel import select

    from app.models.approval import Approval

    if task_id is None:
        return None
    result = await session.exec(
        select(Approval).where(
            Approval.action_type == ADR_GATE_ACTION_TYPE,
            Approval.status == "pending",
            Approval.task_id == task_id,
        )
    )
    for approval in result.all():
        if (approval.payload or {}).get("digest") == digest:
            return approval
    return None


async def ensure_adr_gate(
    session,
    run_cmd,
    *,
    repo_full_name: str,
    pr_number: int,
    board_id=None,
    task_id=None,
    agent_id=None,
) -> None:
    """Refuse the merge unless this PR's decision documents are operator-approved.

    Returns `None` when the PR touches no decision document, or when the exact
    ADR text is already approved. Otherwise records the outstanding approval
    (which lands in the operator's Inbox via `approval.created`) and raises
    `AdrGateBlocked`.
    """
    entries, _head_sha = await collect_decision_docs(run_cmd, repo_full_name, pr_number)
    await evaluate_adr_gate(
        session,
        entries,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        board_id=board_id,
        task_id=task_id,
        agent_id=agent_id,
    )


async def evaluate_adr_gate(
    session,
    entries: list[tuple[str, str | None]],
    *,
    repo_full_name: str,
    pr_number: int,
    board_id=None,
    task_id=None,
    agent_id=None,
) -> None:
    """The verdict itself, on already-collected entries.

    Split from the collection so callers can treat the two failure classes
    differently: an inconclusive *lookup* (`gh` down) may pass loudly, but a
    *detected* ADR change must never pass — including when recording the
    approval request fails. Nothing here is wrapped.
    """
    if not entries:
        return

    digest = adr_digest(entries)
    approved = await find_approved_adr_gate(session, digest=digest, task_id=task_id, board_id=board_id)
    if approved is not None:
        logger.info("ADR-Gate: Freigabe %s deckt PR #%d ab", approved.id, pr_number)
        return

    described = describe_entries(entries)
    detail = (
        f"PR #{pr_number} aendert Entscheidungsdokumente: {described}. "
        "Die ADR-Texte sind nicht freigegeben — der Merge ist gesperrt, bis der "
        "Operator genau diese Fassung freigibt."
    )

    pending = await find_pending_adr_gate(session, digest=digest, task_id=task_id)
    if pending is not None:
        approval_id = pending.id
    else:
        from app.services.autonomy import enforce_autonomy

        # Unknown action_type ⇒ L3 ⇒ an Approval is created and `approval.created`
        # is emitted. No schema change: approvals.action_type is free-form Text.
        #
        # MUST be called before the caller mutates `task.status`: this path
        # commits (enforce_autonomy and emit_event both do), which would persist
        # the very transition the gate refuses — the 409 would be a lie and the
        # card would sit at `done` with an unresolvable review.
        try:
            await enforce_autonomy(
                ADR_GATE_ACTION_TYPE,
                session,
                agent_id=agent_id,
                board_id=board_id,
                description=detail,
                task_id=task_id,
                payload={
                    "digest": digest,
                    "pr_number": pr_number,
                    "repo": repo_full_name,
                    "documents": [path for path, _ in entries],
                },
            )
        except Exception:  # noqa: BLE001
            # A detected ADR change must not pass just because the approval
            # record could not be written. Fail closed anyway — the operator
            # gets the same 409, minus the convenience link.
            logger.exception(
                "ADR-Gate: Freigabe fuer PR #%d konnte nicht angelegt werden — "
                "Merge bleibt gesperrt", pr_number,
            )
            raise AdrGateBlocked(detail, approval_id=None, digest=digest)
        resolved = await find_pending_adr_gate(session, digest=digest, task_id=task_id)
        approval_id = resolved.id if resolved is not None else None

    raise AdrGateBlocked(detail, approval_id=approval_id, digest=digest)


async def pr_number_for_task(session, task) -> int | None:
    """The PR this card produced, or None when it never created one.

    Prefers the `task.pr_number` field (written by `handle_review_pr_creation`
    since 2026-09-13) and falls back to the `PR erstellt:` comment marker —
    the same greppable contract the merge helpers use (Pitfall H). Cards
    without either never reach a merge, so they are out of scope by
    construction.
    """
    pr_number = getattr(task, "pr_number", None)
    if pr_number:
        return int(pr_number)

    import re

    from sqlmodel import select

    from app.models.task import TaskComment

    result = await session.exec(
        select(TaskComment)
        .where(
            TaskComment.task_id == task.id,
            TaskComment.content.like("%PR erstellt:%"),
        )
        .order_by(TaskComment.created_at.desc())
        .limit(1)
    )
    comment = result.first()
    if comment is None:
        return None
    match = re.search(r"/pull/(\d+)", comment.content)
    return int(match.group(1)) if match else None


async def guard_adr_merge(
    session,
    task,
    *,
    agent_id=None,
    board_id=None,
) -> None:
    """Refuse to let `task` reach its merge while its ADR text is unapproved.

    Call this BEFORE the status transition commits, not inside the merge
    helper's `try` block: every merge helper in this codebase ends in
    `except Exception: logger.warning("PR-Merge fehlgeschlagen")`, so a gate
    placed there is swallowed — a new silent failure, which is the bug class
    this task removes. It therefore raises `HTTPException(409)` (the codebase's
    enforcement-guard idiom, matching `repo_binding.enforce_repo_binding`) and
    lets the operator resolve the recorded approval to unblock the retry.
    """
    if getattr(task, "project_id", None) is None:
        return

    pr_number = await pr_number_for_task(session, task)
    if pr_number is None:
        # No PR on this card ⇒ no merge ⇒ nothing for the ADR-Gate to guard.
        return

    from app.models.board import Project

    project = await session.get(Project, task.project_id)
    if project is None or not project.github_repo_name:
        return

    from app.services.git_service import git_service

    try:
        entries, _head_sha = await collect_decision_docs(
            git_service._run_cmd, project.github_repo_name, pr_number,
        )
    except Exception as exc:  # noqa: BLE001 — `gh` unreachable / PR already gone
        # An *inconclusive lookup* must not freeze every done-card that has a
        # PR: a transient GitHub outage would otherwise block the whole board —
        # precisely the failure mode that gets a guard deleted again. The pass
        # is loud, not silent: logged AND emitted as an event, so an operator
        # can tell "not an ADR change" apart from "never checked". Note the
        # *verdict* itself never fails open — see `evaluate_adr_gate`.
        logger.warning(
            "ADR-Gate: Pruefung fuer PR #%s (Task %s) nicht moeglich, Merge "
            "OHNE ADR-Pruefung zugelassen: %s", pr_number, task.id, exc,
        )
        try:
            from app.services.activity import emit_event

            await emit_event(
                session,
                "adr_gate_check_unavailable",
                f"ADR-Gate nicht pruefbar fuer PR #{pr_number} — Merge ungeprueft zugelassen",
                severity="warning",
                board_id=board_id or task.board_id,
                task_id=task.id,
                agent_id=agent_id,
                detail={"pr_number": pr_number, "error": str(exc)[:300]},
            )
        except Exception:  # noqa: BLE001 — never let the audit trail block the board
            logger.warning("ADR-Gate: Nicht-pruefbar-Event fehlgeschlagen", exc_info=True)
        return

    try:
        await evaluate_adr_gate(
            session,
            entries,
            repo_full_name=project.github_repo_name,
            pr_number=pr_number,
            board_id=board_id or task.board_id,
            task_id=task.id,
            agent_id=agent_id,
        )
    except AdrGateBlocked as blocked:
        from fastapi import HTTPException

        detail = str(blocked)
        if blocked.approval_id is not None:
            detail += (
                f" Freigabe anfordern/freigeben: PATCH /api/v1/approvals/{blocked.approval_id}"
                " (status=approved)."
            )
        logger.warning("ADR-Gate: Merge fuer Task %s blockiert — %s", task.id, detail)
        raise HTTPException(status_code=409, detail=detail) from blocked
