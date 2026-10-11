"""Box token ledger — books what a local inference box itself counted.

Insights sees local tokens through MC's own transcripts only. A request sent
straight to a box's OpenAI-compatible API from another machine never lands in
a transcript, but the engine's Prometheus counters count it. The runtime pulse
(services/runtime_pulse.py) already scrapes ``/metrics`` every few seconds for
the tok/s strip; it hands the same text to ``token_ledger.observe`` and this
module books the counter DELTAS into hourly UTC buckets
(``runtime_token_hours``). Insights subtracts what MC attributed to local
models to get the "external" share (services/usage_baseline.py).

Counters (all cumulative; ``prompt`` includes the cached part):

  engine      prompt                          cached                             generated
  tensorfold  tensorfold:prompt_tokens_total  tensorfold_health:cached_tokens_total  tensorfold_health:completion_tokens_total
                                                                                 (streaming-aware; falls back to
                                                                                 tensorfold:generation_tokens_total)
  vllm        vllm:prompt_tokens_total        vllm:prefix_cache_hits_total       vllm:generation_tokens_total
  sglang      sglang:prompt_tokens_total      sglang:cached_tokens_total         sglang:generation_tokens_total

Only the TensorFold names were checked against a running box; the vLLM cache
and all SGLang names follow the engines' docs. A missing counter books 0 for
that column, never a guess.

Delta rules (``counter_delta``):
  - first sight of a host × engine books nothing — counting starts when the
    box is first seen, there is no history before;
  - a counter that went DOWN means the engine restarted: the new value is the
    count since the restart and is booked in full (not dropped, not clamped);
  - the last booked values live in ``runtime_token_counters``, so a backend
    restart continues from the DB instead of booking the whole counter again.

Scheduling (``TokenLedger.observe``, every pulse tick): book at first sight
in this process, then every ``runtime_token_flush_interval`` seconds. Two
cases book earlier so nothing slips: the counter dropped (the last scrape
before the restart is booked first, then the new value) and a new UTC hour
began (the last scrape of the old hour is booked into the old hour).
Booking is idempotent — the same counters twice add nothing — so a second
worker or a retried flush cannot double count.

Not covered: if the engine restarts while the backend is down, the tokens
between the last booking and the restart are lost; if the backend is down
for long, the catch-up delta lands in the hour it is booked.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.runtime_token import RuntimeTokenCounter, RuntimeTokenHour

logger = logging.getLogger(__name__)

COMPONENTS = ("prompt", "cached", "generated")

# engine → component → metric names in priority order (first present wins,
# all label series of that name are summed).
_BOX_COUNTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "tensorfold": {
        "prompt": ("tensorfold:prompt_tokens_total",),
        "cached": ("tensorfold_health:cached_tokens_total",),
        "generated": ("tensorfold_health:completion_tokens_total", "tensorfold:generation_tokens_total"),
    },
    "vllm": {
        "prompt": ("vllm:prompt_tokens_total",),
        "cached": ("vllm:prefix_cache_hits_total",),
        "generated": ("vllm:generation_tokens_total",),
    },
    "sglang": {
        "prompt": ("sglang:prompt_tokens_total",),
        "cached": ("sglang:cached_tokens_total",),
        "generated": ("sglang:generation_tokens_total",),
    },
}


def _sum_metric(text: str, name: str) -> float | None:
    pattern = re.compile(rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9eE+\-.]+)(?:\s+\d+)?\s*$")
    total, found = 0.0, False
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        try:
            total += float(match.group(1))
        except ValueError:
            continue
        found = True
    return total if found else None


def parse_box_counters(text: str, engine: str) -> dict[str, float | None]:
    """Prompt/cached/generated counter totals for ``engine``; None = not exposed."""
    names = _BOX_COUNTERS.get(engine, {})
    out: dict[str, float | None] = {}
    for comp in COMPONENTS:
        out[comp] = None
        for name in names.get(comp, ()):
            value = _sum_metric(text, name)
            if value is not None:
                out[comp] = value
                break
    return out


def counter_delta(prev: float | None, cur: float | None) -> int:
    """Tokens since ``prev``. No previous value or no current value → 0.
    A drop means the engine restarted, so ``cur`` is the count since then."""
    if prev is None or cur is None:
        return 0
    if cur < prev:
        return int(round(cur))
    return int(round(cur - prev))


def _hour(at: datetime) -> datetime:
    return at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


async def book_counters(
    session: AsyncSession,
    *,
    host_id: uuid.UUID,
    engine: str,
    counters: dict[str, float | None],
    at: datetime,
) -> dict[str, int]:
    """Book the delta between ``counters`` and the last booked values into
    the UTC hour of ``at`` and remember ``counters``. Idempotent."""
    state = (
        await session.exec(
            select(RuntimeTokenCounter)
            .where(RuntimeTokenCounter.host_id == host_id, RuntimeTokenCounter.engine == engine)
            .with_for_update()
        )
    ).first()
    deltas = {
        comp: counter_delta(getattr(state, f"last_{comp}") if state else None, counters.get(comp))
        for comp in COMPONENTS
    }
    if state is None:
        state = RuntimeTokenCounter(host_id=host_id, engine=engine, first_seen_at=at, updated_at=at)
        session.add(state)
    for comp in COMPONENTS:
        if counters.get(comp) is not None:
            setattr(state, f"last_{comp}", float(counters[comp]))
    state.updated_at = at

    if any(deltas.values()):
        hour = _hour(at)
        row = (
            await session.exec(
                select(RuntimeTokenHour).where(
                    RuntimeTokenHour.host_id == host_id,
                    RuntimeTokenHour.engine == engine,
                    RuntimeTokenHour.hour == hour,
                )
            )
        ).first()
        if row is None:
            row = RuntimeTokenHour(host_id=host_id, engine=engine, hour=hour,
                                   prompt_tokens=0, cached_tokens=0, generated_tokens=0)
            session.add(row)
        row.prompt_tokens += deltas["prompt"]
        row.cached_tokens += deltas["cached"]
        row.generated_tokens += deltas["generated"]
        row.updated_at = at
    await session.commit()
    return deltas


@dataclass
class _Seen:
    counters: dict[str, float | None]
    at: datetime
    booked: bool


def _default_session_factory() -> AbstractAsyncContextManager[AsyncSession]:
    from app.services.runtime_model_resolver import session_scope

    return session_scope()


class TokenLedger:
    """Decides on each pulse tick whether to book (see module docstring)."""

    def __init__(
        self,
        flush_interval: int | None = None,
        session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None,
    ) -> None:
        self._interval = (
            flush_interval if flush_interval is not None else settings.runtime_token_flush_interval
        )
        self._session_factory = session_factory or _default_session_factory
        self._seen: dict[tuple[uuid.UUID, str], _Seen] = {}
        self._last_booked: dict[tuple[uuid.UUID, str], datetime] = {}

    async def observe(
        self, host_id: uuid.UUID, engine: str, counters: dict[str, float | None], now: datetime
    ) -> None:
        if not self._interval or self._interval <= 0:
            return
        if all(counters.get(c) is None for c in COMPONENTS):
            return
        key = (host_id, engine)
        prev = self._seen.get(key)
        reset = prev is not None and any(
            counters.get(c) is not None
            and prev.counters.get(c) is not None
            and counters[c] < prev.counters[c]
            for c in COMPONENTS
        )
        new_hour = prev is not None and _hour(prev.at) != _hour(now)
        if prev is not None and not prev.booked and (reset or new_hour):
            await self._book(key, prev.counters, prev.at)

        last = self._last_booked.get(key)
        due = (
            prev is None
            or reset
            or new_hour
            or last is None
            or (now - last).total_seconds() >= self._interval
        )
        self._seen[key] = _Seen(counters=dict(counters), at=now, booked=False)
        if due:
            await self._book(key, counters, now)
            self._seen[key].booked = True

    async def _book(self, key: tuple[uuid.UUID, str], counters: dict, at: datetime) -> None:
        async with self._session_factory() as session:
            await book_counters(session, host_id=key[0], engine=key[1], counters=counters, at=at)
        self._last_booked[key] = at


token_ledger = TokenLedger()
