"""Bench tasks own their follow-up — the board lead is never called back.

A finished bench agent task used to trigger the generic completion callback
to the board lead ("TASK ERLEDIGT — classify the result"). The lead runs on
a cloud model, so every "local" bench run carried a hidden cloud cost
(measured 21.10.2026: 3 lead turns, 0.71 USD, answer "Task only"). The
operator judges bench results in the Bench Studio itself; the lead has
nothing to do. Registered on hooks.completion_callback_skip_hooks.
"""
from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchEntry

# Written by orchestrator.dispatch_agent_entry on every bench agent task.
# Kept as the second marker so a task whose entry was re-pointed by a retry
# (entry.task_id -> the NEW task) is still recognized when it ends late.
BENCH_AUTO_REASON_PREFIX = "bench_studio challenge "


async def owns_completion_followup(session: AsyncSession, task) -> bool:
    """True for bench agent tasks (linked BenchEntry or bench auto_reason).

    Never matches on the title: an operator card named "[Bench] …" is not
    a bench run and still reaches the lead.
    """
    if (task.auto_reason or "").startswith(BENCH_AUTO_REASON_PREFIX):
        return True
    result = await session.exec(select(BenchEntry.id).where(BenchEntry.task_id == task.id))
    return result.first() is not None
