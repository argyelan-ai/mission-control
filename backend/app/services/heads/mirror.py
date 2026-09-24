"""Mirror a head state onto its task card — existing status values only
(docs/specs/head-launcher.md §6.2).

Never through ``task_lifecycle`` transition helpers (they would trigger the
review hand-off). Every hop of a mirror write is one edge of
``VALID_TRANSITIONS`` — the Python mirror of the Postgres trigger
``validate_task_transition`` (migration 0159), which is the real guard in
production; the SQLite test engine does not run it, so ``mirror_path``
checks every hop itself.
"""
from __future__ import annotations

import logging
from collections import deque

from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task, TaskComment
from app.services.task_lifecycle import record_task_event
from app.task_status import VALID_TRANSITIONS, TaskStatus, is_valid_transition
from app.utils import utcnow

logger = logging.getLogger(__name__)

HEAD_TO_TASK: dict[str, str] = {
    "starting": TaskStatus.IN_PROGRESS,
    "running": TaskStatus.IN_PROGRESS,
    "needs_you": TaskStatus.WAITING,
    # Not done: the PR is open, merging is the operator's acceptance.
    "passed": TaskStatus.REVIEW,
    "failed": TaskStatus.FAILED,
    # There is no transition into `aborted`; same target as the fleet's stop.
    "stopped": TaskStatus.BLOCKED,
}

# Neighbour order for the shortest-path search — ties resolve the way the
# spec's table reads (waiting → blocked → failed, failed → inbox → in_progress).
_PRIORITY = [
    TaskStatus.BLOCKED,
    TaskStatus.INBOX,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING,
    TaskStatus.REVIEW,
    TaskStatus.FAILED,
    TaskStatus.USER_TEST,
    TaskStatus.DONE,
    TaskStatus.ABORTED,
]


def mirror_path(from_status: str, to_status: str) -> list[str] | None:
    """Shortest list of hops (excluding ``from_status``) from → to.

    ``[]`` when already there, ``None`` when unreachable.
    """
    if from_status == to_status:
        return []
    prev: dict[str, str | None] = {from_status: None}
    queue = deque([from_status])
    while queue:
        cur = queue.popleft()
        targets = VALID_TRANSITIONS.get(cur, set())
        for nxt in sorted(targets, key=lambda s: _PRIORITY.index(s) if s in _PRIORITY else 99):
            if nxt in prev:
                continue
            prev[nxt] = cur
            if nxt == to_status:
                hops = [nxt]
                back = cur
                while back is not None and back != from_status:
                    hops.append(back)
                    back = prev[back]
                return [str(h) for h in reversed(hops)]
            queue.append(nxt)
    return None


def _comment(session: AsyncSession, task: Task, kind: str, text: str) -> None:
    session.add(TaskComment(task_id=task.id, author_type="system", comment_type=kind, content=text))


async def move_task(session: AsyncSession, task: Task, target: str, reason: str) -> bool:
    """Walk ``task`` to ``target`` hop by hop. Returns True when it moved."""
    hops = mirror_path(str(task.status), target)
    if hops is None:
        logger.warning("heads: no path %s → %s for task %s", task.status, target, task.id)
        return False
    for hop in hops:
        if not is_valid_transition(str(task.status), hop):  # belt and braces
            logger.error("heads: refusing invalid hop %s → %s", task.status, hop)
            return False
        await record_task_event(
            session, task.id, str(task.status), hop,
            changed_by="head", reason=reason, actor_label="head",
        )
        task.status = hop
        # One UPDATE per hop: the Postgres trigger validate_task_transition
        # sees OLD → NEW of each statement. Without the flush a multi-hop walk
        # (failed → inbox → in_progress) reaches the DB as ONE invalid
        # failed → in_progress UPDATE at commit (live: restart → HTTP 500).
        session.add(task)
        await session.flush()
    if hops:
        task.updated_at = utcnow()
    return bool(hops)


async def apply_head_state(session: AsyncSession, task: Task, run, derived: dict) -> bool:
    """Mirror one derived head state onto ``task`` (caller commits)."""
    state = derived["state"]
    target = HEAD_TO_TASK[state]
    # The hold keeps dispatch + healers away for the whole life of the card.
    task.run_control = "manual_hold"
    moved = await move_task(session, task, target, reason=f"head_{state}")
    pair = f"{run.spec.get('harness')} × {run.spec.get('runtime_slug')}"
    if state == "needs_you" and moved:
        question = (run.question or "").strip() or "(question.md is empty)"
        _comment(session, task, "blocker", f"Head ({pair}) needs you:\n\n{question}")
    elif state == "passed":
        if run.pr_url:
            task.pr_url = run.pr_url
        if moved and run.pr_url:
            _comment(session, task, "resolution", f"Head ({pair}) passed — PR open: {run.pr_url}")
        elif moved:
            _comment(
                session, task, "resolution",
                f"Head ({pair}) passed — branch pushed: {run.spec.get('branch')} "
                "(scratch repo with a local origin — no PR possible).",
            )
    elif state == "failed" and moved:
        _comment(session, task, "message", f"Head ({pair}) failed: {derived.get('reason') or 'unknown'}")
    session.add(task)
    return moved
