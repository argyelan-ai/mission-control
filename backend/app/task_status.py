"""TaskStatus — single source of truth for task status and allowed transitions.

Imported by tasks.py, agent_scoped.py, watchdog, task_lifecycle.py.
No other location may define its own status maps.
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    INBOX = "inbox"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    USER_TEST = "user_test"
    WAITING = "waiting"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABORTED = "aborted"


# Valid status transitions (from → allowed targets)
#
# `waiting` (Task 6, answer-wait for `ask --blocking`): the task's session
# stays alive, it's paused on an operator/agent answer — distinct from
# `blocked` (external impediment) and `user_test` (Mark's manual test gate).
# Deliberately NOT inbox -> waiting: a task must be actively worked before it
# can wait on an answer. Mirrored 1:1 in the Postgres trigger
# `validate_task_transition` (migration 0159) — that DB-level guard is the
# only one enforced in production; this dict is what tests exercise since the
# SQLite test engine doesn't run Postgres triggers.
VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.INBOX:       {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
    TaskStatus.IN_PROGRESS: {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.INBOX, TaskStatus.FAILED, TaskStatus.WAITING},
    TaskStatus.REVIEW:      {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.INBOX, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.USER_TEST},
    TaskStatus.USER_TEST:   {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW},
    TaskStatus.WAITING:     {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
    TaskStatus.BLOCKED:     {TaskStatus.INBOX, TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.FAILED:      {TaskStatus.INBOX},
    TaskStatus.DONE:        {TaskStatus.IN_PROGRESS},
    TaskStatus.ABORTED:     {TaskStatus.IN_PROGRESS, TaskStatus.INBOX},
}

# Status labels for error messages
STATUS_LABELS: dict[str, str] = {
    TaskStatus.INBOX: "Inbox",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.REVIEW: "Review",
    TaskStatus.USER_TEST: "User Test",
    TaskStatus.WAITING: "Waiting",
    TaskStatus.DONE: "Done",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.FAILED: "Failed",
    TaskStatus.ABORTED: "Aborted",
}

ALL_STATUSES = set(TaskStatus)


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """Check whether a status transition is allowed."""
    allowed = VALID_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# Terminal status: when a task moves here, it is "done"
TERMINAL_STATUSES = {TaskStatus.DONE}


async def check_children_complete(task_id, session) -> tuple[bool, str]:
    """Check whether all children of a task are complete.

    Returns:
        (True, "") if no children exist or all are done.
        (False, detail_message) if open children exist.
    """
    from app.models.task import Task  # Lazy import to avoid circular
    from sqlmodel import select

    result = await session.exec(
        select(Task).where(Task.parent_task_id == task_id)
    )
    children = result.all()

    if not children:
        return True, ""

    open_children = [c for c in children if c.status != TaskStatus.DONE]
    if not open_children:
        return True, ""

    status_summary = ", ".join(
        f'"{c.title}" ({c.status})' for c in open_children[:5]
    )
    remaining = len(open_children) - 5
    if remaining > 0:
        status_summary += f" ... und {remaining} weitere"

    return False, (
        f"Parent kann nicht abgeschlossen werden: "
        f"{len(open_children)} Subtask(s) noch offen: {status_summary}"
    )
