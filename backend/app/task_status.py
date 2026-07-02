"""TaskStatus — Single Source of Truth fuer Task-Status und erlaubte Uebergaenge.

Importiert von tasks.py, agent_scoped.py, watchdog, task_lifecycle.py.
Kein anderer Ort darf eigene Status-Maps definieren.
"""

from enum import StrEnum


class TaskStatus(StrEnum):
    INBOX = "inbox"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    USER_TEST = "user_test"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABORTED = "aborted"


# Gueltige Status-Uebergaenge (von → erlaubte Ziele)
VALID_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.INBOX:       {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED},
    TaskStatus.IN_PROGRESS: {TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.BLOCKED, TaskStatus.INBOX, TaskStatus.FAILED},
    TaskStatus.REVIEW:      {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.INBOX, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.USER_TEST},
    TaskStatus.USER_TEST:   {TaskStatus.DONE, TaskStatus.IN_PROGRESS, TaskStatus.REVIEW},
    TaskStatus.BLOCKED:     {TaskStatus.INBOX, TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.FAILED:      {TaskStatus.INBOX},
    TaskStatus.DONE:        {TaskStatus.IN_PROGRESS},
    TaskStatus.ABORTED:     {TaskStatus.IN_PROGRESS, TaskStatus.INBOX},
}

# Status-Labels fuer Fehlermeldungen
STATUS_LABELS: dict[str, str] = {
    TaskStatus.INBOX: "Inbox",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.REVIEW: "Review",
    TaskStatus.USER_TEST: "User Test",
    TaskStatus.DONE: "Done",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.FAILED: "Failed",
    TaskStatus.ABORTED: "Aborted",
}

ALL_STATUSES = set(TaskStatus)


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """Pruefen ob ein Status-Uebergang erlaubt ist."""
    allowed = VALID_TRANSITIONS.get(from_status, set())
    return to_status in allowed


# Terminal-Status: Wenn ein Task hierhin wechselt, ist er "fertig"
TERMINAL_STATUSES = {TaskStatus.DONE}


async def check_children_complete(task_id, session) -> tuple[bool, str]:
    """Pruefen ob alle Children eines Tasks abgeschlossen sind.

    Returns:
        (True, "") wenn keine Children existieren oder alle done sind.
        (False, detail_message) wenn offene Children existieren.
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
