"""PR merge monitor — merged PRs close their review/user_test cards.

Background: head cards land in ``review`` with ``pr_url`` when the head
passes, and sat there forever after their PR was merged on GitHub (daily
metrics: "stale cards"). This loop polls GitHub for exactly those cards and
walks merged ones to ``done`` through the normal status path (TaskEvent,
actor = system, plus one ``resolution`` comment). Open or closed-but-unmerged
PRs are left alone — the review decision stays with the reviewer.

GitHub access follows the house pattern (ADR-055): config comes from
``github_config`` (vault > env), the call itself is a ``gh pr view``
subprocess like git_service / github_visibility_monitor. Without a
configured token nothing is called at all. Rate limiting: at most
``MAX_PR_CHECKS_PER_CYCLE`` PR checks per cycle, one cycle every
``CHECK_INTERVAL_SECONDS``.

The loop itself is a plain background service (kz rejected idea
X-loop-as-workflow-or-schedule: no workflow child, no schedule variant),
started/stopped in app.background like the other loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.task import Task, TaskComment
from app.services.github_config import resolve_github_config
from app.services.task_lifecycle import record_task_event
from app.task_status import is_valid_transition
from app.utils import utcnow

logger = logging.getLogger("mc.pr_merge_monitor")

CHECK_INTERVAL_SECONDS = 600  # 10 min — well within "one check interval"
MAX_PR_CHECKS_PER_CYCLE = 20  # rate limit: GitHub calls per cycle

_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")

# Cards that wait on a human/agent review decision and whose PR, once merged,
# means the work is done.
MONITORED_STATUSES = ("review", "user_test")


def parse_pr_url(url: str | None) -> tuple[str, str, int] | None:
    """``https://github.com/<owner>/<repo>/pull/<n>`` → (owner, repo, n).

    ``None`` for anything else (repo root URLs, GitLab, missing pr_url).
    """
    if not url:
        return None
    m = _PR_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


async def _run_gh(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def fetch_pr_merged(owner: str, repo: str, number: int) -> bool | None:
    """True = merged, False = open or closed without merge, None = unknown.

    ``None`` (gh failed, unparseable answer) never closes a card — a missing
    answer must not look like a decision.
    """
    rc, out, err = await _run_gh(
        "gh", "pr", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json", "state,mergedAt",
    )
    if rc != 0:
        logger.warning("gh pr view %s/%s#%d failed: %s", owner, repo, number, err)
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        logger.warning("gh pr view %s/%s#%d: JSON parse failed", owner, repo, number)
        return None
    return data.get("state") == "MERGED"


async def _close_task(session: AsyncSession, task: Task, owner: str, repo: str,
                      number: int) -> None:
    """Walk one card to done through the normal status path (actor = system)."""
    target = "done"
    if str(task.status) == target:
        return
    if not is_valid_transition(str(task.status), target):
        logger.warning(
            "pr_merge_monitor: no direct transition %s → done for task %s",
            task.status, task.id,
        )
        return
    await record_task_event(
        session, task.id, str(task.status), target,
        changed_by="system", reason="pr_merged", actor_label="system",
    )
    task.status = target
    task.updated_at = utcnow()
    session.add(task)
    session.add(TaskComment(
        task_id=task.id,
        author_type="system",
        comment_type="resolution",
        content=f"PR #{number} merged ({owner}/{repo}) — card closed automatically.",
    ))
    await session.flush()
    await session.commit()
    logger.info(
        "pr_merge_monitor: task '%s' → done (PR %s/%s#%d merged)",
        task.title, owner, repo, number,
    )


async def check_once(session: AsyncSession | None = None) -> int:
    """One pass over review/user_test cards with a pr_url.

    Returns the number of cards closed. With no session the caller owns the
    transaction model: the monitor closes each card in its own commit, so one
    failing card cannot block the others.
    """
    config = await resolve_github_config(session)
    if not config.configured:
        logger.debug("pr_merge_monitor: GitHub not configured — idle")
        return 0

    own_session = session is None
    if own_session:
        from app.database import engine

        session = AsyncSession(engine, expire_on_commit=False)

    closed = 0
    checked = 0
    try:
        rows = await session.execute(
            select(Task).where(
                Task.status.in_(MONITORED_STATUSES),
                Task.pr_url.is_not(None),  # type: ignore[union-attr]
            )
        )
        tasks = list(rows.scalars().all())
        for task in tasks:
            if checked >= MAX_PR_CHECKS_PER_CYCLE:
                break
            parsed = parse_pr_url(task.pr_url)
            if parsed is None:
                continue
            owner, repo, number = parsed
            checked += 1
            merged = await fetch_pr_merged(owner, repo, number)
            if merged:
                try:
                    await _close_task(session, task, owner, repo, number)
                    closed += 1
                except Exception:
                    logger.error(
                        "pr_merge_monitor: closing task %s failed", task.id,
                        exc_info=True,
                    )
                    await session.rollback()
    finally:
        if own_session:
            await session.close()
    return closed


async def run_forever() -> None:
    """Background loop (app.background). Idles while no token is configured —
    since ADR-055 the token can be set live via Settings → GitHub."""
    logger.info(
        "pr_merge_monitor: Starte Loop (Intervall %ds, max %d PR-Checks/Cycle)",
        CHECK_INTERVAL_SECONDS, MAX_PR_CHECKS_PER_CYCLE,
    )
    while True:
        try:
            n = await check_once()
            if n:
                logger.info("pr_merge_monitor: %d Karte(n) geschlossen", n)
        except asyncio.CancelledError:
            logger.info("pr_merge_monitor loop cancelled")
            raise
        except Exception as e:
            logger.error("pr_merge_monitor cycle failed: %s", e)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
