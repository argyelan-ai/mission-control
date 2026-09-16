"""ADR-Gate — a PR touching a decision document is not mergeable without the
operator's explicit approval of that ADR *text*.

Incident (2026-09-16, PR #602): `docs/decisions/084-omp-acp-harness-property.md`
was squash-merged — superseding ADR-081 — on a green *code* verdict while
operator approval of the ADR text was still pending. The PR carried zero
reviews, so nothing in the system recorded that the *text* was ever approved.
The rule existed only as prose in the handbook.

This module makes it mechanical. Four properties matter, each chosen against
a specific way the rule can be dodged:

1. **The unit of approval is the ADR text, not the PR.** The operator approves
   a digest over the changed decision documents; the merge is refused unless an
   approved `adr_gate` record carries *that* digest. Editing the ADR after
   approval invalidates it — otherwise "approve then rewrite" would be a
   one-line bypass.
2. **The decision-document test is content-based** (`decision_docs.py`), so
   relocating an ADR does not slip past a path filter. That test runs on
   *every* changed file — there is no path pre-filter, because one is what let
   a decision document at a non-standard path through unexamined. A file that
   is *named* like an ADR but no longer carries the signature, or that was
   renamed out of `docs/decisions/`, counts as a change too: otherwise
   "rewrite the heading out of `# ADR-NNN` form" would hide it. For the same
   reason the **base** revision is asked about every file the head test
   rejects: a document that carried the signature *before* this PR and lost it
   — heading rewritten, file deleted, or renamed and stripped — is a decision
   change at *any* path. Judging the head alone would have made "strip the
   heading of a decision document outside `docs/decisions/`" a fresh bypass of
   exactly the path rule this gate just learned to see through.
3. **The file list is complete, not merely paginated.** `gh pr view --json
   files` returns exactly 100 entries and stops (measured on PR #537: 126
   changed files, 100 returned), so a decision document past that point was
   invisible — hiding an ADR in a large commit was a one-command bypass. The
   gate reads `/pulls/{n}/files` page by page instead and takes
   `previous_filename` from the same response, so a renamed ADR stays visible.
   The collected count is then **reconciled against `changed_files`** from the
   same PR metadata: GitHub truncates that endpoint for very large PRs, and a
   list that stops early is invisible by construction — the gate would judge a
   prefix and report "no decision document".
4. **The refusal is loud.** `AdrGateBlocked` is raised, never logged. All three
   merge call sites currently convert exceptions into
   `logger.warning("PR-Merge fehlgeschlagen")` — placing the gate *inside*
   those blocks would have created exactly the silent failure this task exists
   to remove. Callers must call this ahead of (or outside) that try-block.
   The same applies to the three facts the gate cannot re-derive: a missing
   head ref skips every content read, a missing base ref hides signature loss
   off-path, and a file list shorter than the PR's `changed_files` shows only a
   prefix. Each of them makes "no decision document" an *assumption* rather
   than a finding, so each one raises instead of passing quietly.

The GitHub access goes through the caller's `run_cmd` (`git_service._run_cmd`),
so the guard uses the same authenticated `gh` binding as the merge it guards,
and tests can drive it without the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from urllib.parse import quote

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

# GitHub's `/pulls/{n}/files` page size. Fixed, not tunable: the stop rule is
# "short page", so a page smaller than this is the end of the list.
PR_FILES_PAGE_SIZE = 100

# Content reads run in parallel. One `gh api` call is ~0.38 s (measured
# 2026-09-16 against this repo), and a PR check does up to two reads per file
# (head + base), so 126 files serial would add ~95 s to every done-transition.
# The cap keeps us far below GitHub's secondary rate limits while still
# collapsing the wait by an order of magnitude.
CONTENT_FETCH_CONCURRENCY = 8


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


async def fetch_pr_files(
    run_cmd, repo_full_name: str, pr_number: int,
) -> tuple[str, str, list[tuple[str, str | None]]]:
    """(head_sha, base_sha, [(path, previous_path)]) for a PR. Raises on doubt.

    Paginated on purpose: `gh pr view --json files` delivers exactly 100
    entries and stops. Measured 2026-09-16 on PR #537 — 126 changed files in
    the PR, 100 returned. An ADR behind that cap was invisible to the gate, so
    "hide the decision document in a large commit" was a one-command bypass.

    `previous_filename` arrives in the same response and costs nothing extra;
    it is what keeps a *renamed* ADR visible to `collect_decision_docs`. Both
    SHAs come from the same metadata call: the head ref reads the text as it
    stands, the base ref tells whether a document that lost its signature in
    this PR ever carried one.

    The collected list is reconciled against `changed_files` from that same
    response. Every part of the gate reasons about the *set* of changed files,
    so a list that stops early is a silent hole by construction — the gate
    would judge a prefix, find no signature in it, and report "no decision
    document". GitHub truncates this endpoint for very large PRs, so a
    mismatch raises instead of being rounded down to "nothing to see".
    Verified equal on live PRs #537/#602/#610/#600/#599/#590 (126/25/16/4/3/4)
    before making it fatal.
    """
    raw = await run_cmd("gh", "api", f"repos/{repo_full_name}/pulls/{pr_number}")
    metadata = json.loads(raw)
    head_sha = (metadata.get("head") or {}).get("sha") or ""
    if not head_sha:
        # Without a ref every content read is skipped and *every* file looks
        # like a non-ADR — a silent pass. Fail instead, so the caller's
        # fail-open path makes it loud.
        raise RuntimeError(
            f"gh lieferte keinen head-SHA fuer PR #{pr_number} — "
            "ADR-Pruefung nicht moeglich"
        )
    base_sha = (metadata.get("base") or {}).get("sha") or ""
    if not base_sha:
        # The base revision is what makes signature *loss* visible off-path;
        # without it a stripped heading reads as an ordinary edit. Loud, like
        # a missing head ref.
        raise RuntimeError(
            f"gh lieferte keinen base-SHA fuer PR #{pr_number} — "
            "ADR-Pruefung nicht moeglich"
        )

    files: list[tuple[str, str | None]] = []
    page = 1
    while True:
        raw = await run_cmd(
            "gh", "api",
            f"repos/{repo_full_name}/pulls/{pr_number}/files"
            f"?per_page={PR_FILES_PAGE_SIZE}&page={page}",
        )
        batch = json.loads(raw)
        for entry in batch:
            path = entry.get("filename")
            if path:
                files.append((path, entry.get("previous_filename")))
        # A short page is the last page; GitHub answers `[]` past the end.
        if len(batch) < PR_FILES_PAGE_SIZE:
            break
        page += 1

    changed_files = metadata.get("changed_files")
    if isinstance(changed_files, int) and changed_files != len(files):
        raise RuntimeError(
            f"gh lieferte {len(files)} von {changed_files} geaenderten Dateien "
            f"fuer PR #{pr_number} — ADR-Pruefung nicht moeglich"
        )

    return head_sha, base_sha, files


async def fetch_file_content(run_cmd, repo_full_name: str, path: str, ref: str) -> str | None:
    """File body at `ref`, or None when it does not exist there.

    None is meaningful (deleted/unreadable), not an error: `is_decision_doc`
    treats a vanished candidate as still decision-relevant.
    """
    try:
        return await run_cmd(
            "gh", "api",
            f"repos/{repo_full_name}/contents/{quote(path, safe='/')}?ref={ref}",
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

    Every changed file is judged by its content — there is deliberately NO
    path pre-filter. `is_decision_doc` never got to run on a decision document
    outside `docs/decisions/` (measured bypass: a PR adding only
    `notes/decision.md` with `# ADR-085` passed the gate unexamined), and a
    suffix whitelist is an extension blacklist, not a definition.

    A path is *also* a candidate when it is ADR-*named* — at the head or, for
    a rename, at `previous_path`. Signature loss at a known ADR path counts
    like a deletion: otherwise "rewrite the first heading out of `# ADR-NNN`
    form" makes the document vanish from the gate. Same for renaming an ADR
    out of the folder without rewriting its heading. The digest covers the
    text as it stands now, so an approval of the pre-strip text cannot unlock
    the stripped version.

    The *base* revision is asked about every file the head test rejects. The
    path rule above only covers `docs/decisions/`; a decision document living
    anywhere else (the whole point of judging by body, not by path) whose
    heading is rewritten or which is deleted used to slip through both tests
    at once — `is_decision_doc` says "no signature", `_is_adr_named` says "not
    an ADR path". A document that carried `# ADR-NNN` before this PR is a
    decision change at *any* path, so it is judged on either revision.

    Cost: the base read runs only for files the head test rejected, at most
    one extra `gh` content call per changed file (~0.38 s measured). Reads run
    concurrently, bounded by `CONTENT_FETCH_CONCURRENCY`. A base-side read
    failure is indistinguishable from "did not exist at base" — both yield no
    signature, so the base probe can only *add* documents, never hide one.
    """
    head_sha, base_sha, files = await fetch_pr_files(run_cmd, repo_full_name, pr_number)
    semaphore = asyncio.Semaphore(CONTENT_FETCH_CONCURRENCY)

    async def _read(path: str, ref: str) -> str | None:
        async with semaphore:
            return await fetch_file_content(run_cmd, repo_full_name, path, ref)

    head_contents = await asyncio.gather(
        *(_read(path, head_sha) for path, _previous in files)
    )

    matched = [
        is_decision_doc(head_contents[index], files[index][0])
        or _is_adr_named(files[index][0], files[index][1])
        for index in range(len(files))
    ]

    unanswered = [index for index, hit in enumerate(matched) if not hit]
    if unanswered:
        # At the base revision a renamed file still lives at its old path —
        # reading the head path would 404 and hide exactly the rename+strip
        # combination this probe exists for.
        base_paths = [files[index][1] or files[index][0] for index in unanswered]
        base_contents = await asyncio.gather(
            *(_read(base_path, base_sha) for base_path in base_paths)
        )
        for index, base_path, base_content in zip(unanswered, base_paths, base_contents):
            # Judged against the *base* path: the filename has to agree with
            # the signature there, same rule as at the head.
            if is_decision_doc(base_content, base_path):
                matched[index] = True

    entries: list[tuple[str, str | None]] = [
        (files[index][0], head_contents[index])
        for index in range(len(files))
        if matched[index]
    ]
    return entries, head_sha


def _is_adr_named(path: str, previous_path: str | None = None) -> bool:
    """True when the file is named like an ADR — now or before a rename."""
    return decision_doc_number(path) is not None or (
        previous_path is not None and decision_doc_number(previous_path) is not None
    )


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


async def _project_repo_full_name(session, project) -> str | None:
    """The GitHub repo this project merges into, or None if it has none.

    Not `project.github_repo_name` alone: `repo_binding.project_binds_repo`
    accepts a project bound through the ADR-050 registry (`repo_id`), and a
    card on such a project would reach the merge with the gate never running.
    `resolve_repo_for_project` already prefers `repo_id` and falls back to the
    legacy name, so both spellings end in the same answer; the extra fallback
    covers a name whose registry row is missing.
    """
    try:
        from app.services.repo_registry import resolve_repo_for_project

        repo = await resolve_repo_for_project(session, project)
    except Exception:  # noqa: BLE001 — a broken lookup must not freeze merges
        logger.warning("ADR-Gate: Repo-Lookup zum Projekt fehlgeschlagen", exc_info=True)
        repo = None
    if repo is not None:
        return repo.full_name
    return project.github_repo_name or None


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
    if project is None:
        return
    repo_full_name = await _project_repo_full_name(session, project)
    if repo_full_name is None:
        return

    from app.services.git_service import git_service

    try:
        entries, _head_sha = await collect_decision_docs(
            git_service._run_cmd, repo_full_name, pr_number,
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
            repo_full_name=repo_full_name,
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
