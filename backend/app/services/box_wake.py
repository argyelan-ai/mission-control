"""What both wake routes do around ``runtime_manager.wake_*`` — in one place.

``POST /hosts/{id}/wake`` (the box card) and ``POST /runtimes/{id}/wake`` (the
runtime detail) wake the same box through the same trigger file. They drifted
apart once (round-7 L5, r1 N1: the runtime route kept the box "asleep"), so
the answer to a failed wake and the clean-up after a sent one live here.
"""

from __future__ import annotations

import uuid
from typing import Iterable

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.services import host_liveness, runtime_readiness


def raise_unless_sent(result: dict) -> None:
    """Turn a failed wake into its HTTP answer; a sent wake passes.

    503 when the wake helper on the Mac is down — the request is fine, nothing
    reads it. The detail then carries ``reason`` (``no_heartbeat``,
    ``heartbeat_stale``, ``request_waiting``) next to the German ``message``.
    Anything else that failed (no MAC, not power-managed) is a 400.
    """
    if result.get("ok"):
        return
    if result.get("helper_down"):
        raise HTTPException(
            status_code=503,
            detail={"message": result["message"], "reason": result.get("reason")},
        )
    raise HTTPException(status_code=400, detail=result["message"])


async def forget_box_after_wake(
    session: AsyncSession,
    host_id: uuid.UUID | str | None,
    *,
    slugs: Iterable[str | None] = (),
) -> None:
    """A wake was sent: the box's remembered "asleep" and the readiness of
    every runtime on it are stale — the next poll asks again.

    ``slugs`` adds runtimes that are not bound to the host row (a legacy
    runtime that carries its own WoL fields).
    """
    stale = {s for s in slugs if s}
    if host_id is not None:
        await host_liveness.forget(host_id)
        rows = (await session.exec(select(Runtime.slug).where(Runtime.host_id == host_id))).all()
        stale.update(s for s in rows if s)
    for slug in sorted(stale):
        await runtime_readiness.invalidate_readiness(slug)
