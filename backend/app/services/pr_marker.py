"""The "PR erstellt:" note MC writes when it opens a card's PR — and the one
query every reader uses to find it again.

Writer (the only one): ``agent_git.handle_review_pr_creation`` — on the
project path the backend pushes the branch, opens the PR and records
``**PR erstellt:** <url>`` as a ``system`` / ``progress`` comment, plus
``task.pr_number`` / ``task.pr_url``. Wording is contract (Pitfall H) —
:func:`pr_created_note` builds it so writer and readers cannot drift.

Readers: the two merge helpers (``task_lifecycle._merge_pr_if_exists``,
``agent_git.handle_done_pr_merge``), the ADR gate (``adr_gate``), the reviewer
brief (``dispatch_message_builder``) and the proof gate (``proof_gate``).

Round 8 / L6: the readers used to take the NEWEST comment that merely
CONTAINED the text — from anyone. A chat reply, an agent's own progress note
or MC's task-done summary (``auto_memory``, which quotes the last two
comments) naming another PR of the same repo made MC squash-merge that PR.
Now only MC's own note counts: system author, ``progress`` type, content that
STARTS with the note. Anything else is text, never a merge target.
"""
from __future__ import annotations

import logging
import re

from sqlmodel import select

from app.models.task import TaskComment

logger = logging.getLogger("mc.pr_marker")

#: Pitfall H wording — DO NOT change (old cards carry it verbatim).
PR_CREATED_MARKER = "PR erstellt:"
_NOTE_PREFIX = f"**{PR_CREATED_MARKER}** "
_PR_NUMBER = re.compile(r"/pull/(\d+)")


def pr_created_note(pr_url: str) -> str:
    """The exact note MC writes when it opened ``pr_url`` for a card."""
    return f"{_NOTE_PREFIX}{pr_url}"


# rule: R-merge-only-mc-pr-note - a newer comment quoting another PR made MC merge that PR
def trusted_pr_note_filter(task_id) -> tuple:
    """WHERE clauses that match only MC's own "PR erstellt:" note."""
    return (
        TaskComment.task_id == task_id,
        TaskComment.author_type == "system",
        TaskComment.comment_type == "progress",
        TaskComment.content.like(f"{_NOTE_PREFIX}%"),  # type: ignore[union-attr]
    )


def pr_number_in(text: str | None) -> int | None:
    match = _PR_NUMBER.search(text or "")
    return int(match.group(1)) if match else None


async def trusted_pr_note(session, task_id) -> TaskComment | None:
    """MC's newest own "PR erstellt:" note on the card, or None."""
    result = await session.exec(
        select(TaskComment)
        .where(*trusted_pr_note_filter(task_id))
        .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
        .limit(1)
    )
    return result.first()


async def _has_foreign_note(session, task_id) -> bool:
    result = await session.exec(
        select(TaskComment.id)
        .where(
            TaskComment.task_id == task_id,
            TaskComment.content.like(f"%{PR_CREATED_MARKER}%"),  # type: ignore[union-attr]
        )
        .limit(1)
    )
    return result.first() is not None


async def pr_number_to_merge(session, task) -> int | None:
    """The PR MC may squash-merge for ``task``, or None.

    Only MC's own note counts. When it and ``task.pr_number`` (written in the
    same step) disagree, nothing is merged — a missed merge is a click on
    GitHub, a wrong merge is someone else's code on ``main``. Every skip that
    hides a possible PR is logged so the operator can see why.
    """
    note = await trusted_pr_note(session, task.id)
    note_number = pr_number_in(note.content) if note else None
    if note_number is None:
        if await _has_foreign_note(session, task.id):
            logger.warning(
                "PR merge skipped for task %s ('%s'): a 'PR erstellt:' note is on the "
                "card but it was not written by MC (chat reply, agent note or summary) "
                "— no merge on a guess",
                task.id, (task.title or "")[:60],
            )
        return None
    field = getattr(task, "pr_number", None)
    if field and int(field) != note_number:
        logger.warning(
            "PR merge skipped for task %s ('%s'): MC's note names PR #%d, the card "
            "field names PR #%d — they disagree, no merge on a guess",
            task.id, (task.title or "")[:60], note_number, int(field),
        )
        return None
    return note_number
