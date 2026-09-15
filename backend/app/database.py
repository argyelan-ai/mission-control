"""Async SQLAlchemy engine + session management.

Pool sizing (incident 2026-09-14, ~20:00 UTC):
  pool_size=10 + max_overflow=20 → 30 connections. Expected concurrent
  checkouts: every agent polls every ~5s with ~0.1–0.2s of DB work
  → 30 pollers contribute ~30 × (0.2 / 5) ≈ 1.2 connections on average;
  UI + frontend traffic peaks around 10. Even at 3× that average the pool
  keeps ≥15 spare connections. A checkout wait of db_pool_timeout=5s
  therefore means NO connection was freed for 5s across all 30 — under
  healthy load one frees every few milliseconds — i.e. the pool is genuinely
  stuck (transactions held across non-DB awaits, CPU starvation). In that
  state the request must FAIL FAST (this timeout) instead of piling up
  behind the leak; 5s also stays under the 8s client timeout observed
  during the incident, so callers retry sooner.

Transaction-over-await rule (same incident):
  A request session must NOT hold an open transaction across an `await`
  that is not itself database work (Redis, embedding/Qdrant HTTP, git).
  Young transactions are congestion; old ones are a leak. Poll/cursor
  paths commit (releasing the connection's transaction) before such
  awaits; `managed_session` logs any session returned with an open
  transaction after db_session_hold_warn_seconds so the next occurrence
  is visible in the log without psql.
"""
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy.exc import TimeoutError as SQLAlchemyPoolTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings

logger = logging.getLogger("mc.database")

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_timeout=settings.db_pool_timeout,
)

@asynccontextmanager
async def managed_session(
    session: AsyncSession, *, route: str = "?"
) -> AsyncGenerator[AsyncSession, None]:
    """Shared session lifecycle: rollback on error, observability on return.

    Extracted from get_session() so the conftest override (and therefore
    every endpoint test) exercises the same release/logging behavior as
    production instead of a lookalike.

    Logged cases (both with endpoint + held duration):
      - pool checkout TIMEOUT: the request waited db_pool_timeout for a
        connection — the pool is exhausted; the request fails fast so it
        does not bind a connection forever (DoD: hanging request ends in
        an error, not an eternal connection hold).
      - session returned with an OPEN transaction after
        db_session_hold_warn_seconds: the leak signature from the
        2026-09-14 incident (transactions aged 279–1789s).
      - session held longer than the warn threshold without an open
        transaction (slow queries / CPU starvation congestion).
    """
    t0 = time.monotonic()
    try:
        yield session
    except SQLAlchemyPoolTimeoutError:
        held = time.monotonic() - t0
        logger.error(
            "DB pool checkout TIMEOUT after %.1fs — failing fast instead of "
            "binding a connection (endpoint=%s, pool_timeout=%.1fs)",
            held, route, settings.db_pool_timeout,
        )
        await session.rollback()
        raise
    except Exception:
        await session.rollback()
        raise
    finally:
        held = time.monotonic() - t0
        open_txn = session.in_transaction()
        await session.close()
        if held >= settings.db_session_hold_warn_seconds:
            if open_txn:
                logger.warning(
                    "DB connection returned with OPEN transaction after %.1fs "
                    "(endpoint=%s) — transaction held across a non-DB await?",
                    held, route,
                )
            else:
                logger.info(
                    "DB connection held %.1fs (endpoint=%s)", held, route
                )


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    route = request.url.path
    session = AsyncSession(engine, expire_on_commit=False)
    async with managed_session(session, route=route):
        yield session


def async_session_maker() -> AsyncSession:
    """Session factory for code that manages its own session lifecycle
    (e.g. the RefactorLoop in routers/powerbi.py). Mirrors get_session()'s
    construction; the caller is responsible for `async with`."""
    return AsyncSession(engine, expire_on_commit=False)


async def create_db_and_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
