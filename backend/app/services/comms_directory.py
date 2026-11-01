"""The one presence source for talking across harnesses (ADR-100 §6 rule 2).

This module alone decides whether a principal is **alive** (a sign of life
within the last 90 s) and whether it is **listening** (takes messages now:
not paused, not switched off). Every reader — `GET /agent/comms/who`, the
send path, later the inbox tab on the phone — asks `state_of` / `directory`;
nothing else computes either word.

The two words stay apart on purpose. A paused agent keeps sending signs of
life (ADR-089 Nachtrag 2026-10-20); one green dot would claim it listens.

Inputs:
- an agent principal: its row (`last_seen_at`, refreshed by every
  agent-token call; `operational_mode`, `comm_v2`, `archived_at`,
  `run_state`) plus an optional presence report it posts itself;
- a terminal session principal (guest card, ADR-100 §6 rule 1): only its
  presence reports.

Presence reports live in Redis with a 90 s expiry. After a backend start, or
after Redis lost its keys (the epoch key is gone), the directory says
"unknown" for 60 s instead of "not alive" — a sender must not conclude a
receiver is gone just because nobody has reported yet.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.redis_client import get_redis
from app.scopes import Scope
from app.utils import ensure_aware

ALIVE_WINDOW_S = 90
PRESENCE_TTL_S = 90
WARMUP_S = 60
EPOCH_KEY = "mc:comms:directory-epoch"
ACCEPT_MODES = ("accept", "hold", "refuse")

# Module import time = backend start. Tests move it into the past.
_PROCESS_STARTED = time.time()

Alive = Literal["yes", "no", "unknown"]


@dataclass(frozen=True)
class Principal:
    """Who talks: an agent (agent token) or a terminal session (guest card).

    ``address`` is what a sender types; it is unique among principals."""

    kind: Literal["agent", "session"]
    id: uuid.UUID
    address: str
    label: str
    harness: str | None = None
    machine: str | None = None
    agent: Agent | None = field(default=None, compare=False, hash=False, repr=False)

    @property
    def ref(self) -> str:
        return f"{self.kind}:{self.id}"


def presence_key(kind: str, principal_id: uuid.UUID | str) -> str:
    return f"mc:comms:presence:{kind}:{principal_id}"


def agent_may_talk(agent: Agent) -> bool:
    """Explicit opt-in: comms:talk must be in the agent's stored scope list.
    An empty list means "all scopes" elsewhere (backward compat) — not here:
    a message is a new way in for prompt injection, so the operator grants
    it per agent (security review, ADR-100 §6). Archived agents never talk."""
    return agent.archived_at is None and Scope.COMMS_TALK.value in (agent.scopes or [])


def agent_address(agent: Agent) -> str:
    return (agent.slug or agent.name or str(agent.id)).strip().lower()


def principal_for_agent(agent: Agent) -> Principal:
    return Principal(
        kind="agent",
        id=agent.id,
        address=agent_address(agent),
        label=agent.name,
        harness=agent.harness or agent.agent_runtime,
        machine=None,
        agent=agent,
    )


def principal_for_session(row) -> Principal:
    """A terminal session enrolled with a guest card (ADR-100 §6 rule 1)."""
    return Principal(
        kind="session",
        id=row.id,
        address=row.address,
        label=row.folder,
        harness=row.harness,
        machine=row.machine,
    )


async def _session_principals(session: AsyncSession) -> list[Principal]:
    """Terminal sessions whose session and guest card are both live."""
    from app.services.comms_guest import live_sessions

    return [principal_for_session(r) for r in await live_sessions(session)]


async def session_address(session: AsyncSession, session_id: uuid.UUID) -> str | None:
    from app.models.comms import CommsSession

    row = await session.get(CommsSession, session_id)
    return row.address if row else None


async def list_principals(session: AsyncSession) -> list[Principal]:
    agents = (await session.exec(select(Agent).where(Agent.archived_at.is_(None)))).all()  # type: ignore[union-attr]
    out = [principal_for_agent(a) for a in agents if agent_may_talk(a)]
    out.extend(await _session_principals(session))
    return out


async def _read_presence(redis, principal: Principal) -> dict | None:
    raw = await redis.get(presence_key(principal.kind, principal.id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def record_presence(
    principal: Principal,
    *,
    listening: bool = True,
    busy: bool = False,
    task: str | None = None,
    accept: str = "accept",
) -> None:
    if accept not in ACCEPT_MODES:
        raise ValueError(f"accept must be one of {ACCEPT_MODES}")
    redis = await get_redis()
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "listening": bool(listening),
        "busy": bool(busy),
        "task": (task or None) and task[:200],
        "accept": accept,
    }
    await redis.set(presence_key(principal.kind, principal.id), json.dumps(payload), ex=PRESENCE_TTL_S)


async def warming_up(redis=None, now: float | None = None) -> bool:
    """True during the first 60 s after a backend start or a Redis restart."""
    redis = redis or await get_redis()
    now = time.time() if now is None else now
    raw = await redis.get(EPOCH_KEY)
    if raw is None:
        await redis.set(EPOCH_KEY, str(now), nx=True)
        raw = await redis.get(EPOCH_KEY) or str(now)
    try:
        epoch = float(raw)
    except ValueError:
        epoch = now
    return now - max(_PROCESS_STARTED, epoch) < WARMUP_S


def _agent_willing(agent: Agent) -> bool:
    """An agent listens only when it is active and has an inbox (comm_v2) —
    without comm_v2 its inbox answers empty, so a message would never land."""
    return (
        agent.archived_at is None
        and (agent.operational_mode or "active") == "active"
        and bool(getattr(agent, "comm_v2", False))
    )


# rule: R-one-presence-source - alive and listening are decided here only
async def state_of(principal: Principal, *, redis=None, warming: bool | None = None) -> dict[str, Any]:
    redis = redis or await get_redis()
    if warming is None:
        warming = await warming_up(redis)
    presence = await _read_presence(redis, principal)
    now = datetime.now(timezone.utc)

    last_seen: datetime | None = None
    if presence and presence.get("at"):
        try:
            last_seen = datetime.fromisoformat(presence["at"])
        except ValueError:
            last_seen = None
    agent = principal.agent
    if agent is not None and agent.last_seen_at is not None:
        seen = ensure_aware(agent.last_seen_at)
        last_seen = max(last_seen, seen) if last_seen else seen

    if last_seen is not None and (now - last_seen).total_seconds() <= ALIVE_WINDOW_S:
        alive: Alive = "yes"
    elif warming:
        alive = "unknown"
    else:
        alive = "no"

    reported_listening = presence.get("listening", True) if presence else True
    if agent is not None:
        listening: bool | None = _agent_willing(agent) and bool(reported_listening)
    else:
        listening = bool(reported_listening) if presence else None

    busy = bool(presence.get("busy")) if presence else False
    if agent is not None and agent.run_state == "running":
        busy = True

    return {
        "alive": alive,
        "listening": listening,
        "busy": busy,
        "accept": (presence or {}).get("accept", "accept"),
        "task": (presence or {}).get("task"),
        "last_seen": last_seen.isoformat() if last_seen else None,
    }


async def directory(session: AsyncSession) -> dict[str, Any]:
    redis = await get_redis()
    warming = await warming_up(redis)
    rows = []
    for p in await list_principals(session):
        st = await state_of(p, redis=redis, warming=warming)
        rows.append({
            "address": p.address,
            "kind": p.kind,
            "label": p.label,
            "harness": p.harness,
            "machine": p.machine,
            **st,
        })
    rows.sort(key=lambda r: r["address"])
    return {"sessions": rows, "warming_up": warming}
