"""Redis-backed live log + status for long-running remote jobs.

Extracted from ``host_bootstrap`` (PR 4) when the recipe installer (PR 6)
needed the same three things: an append-only log the UI can poll with a
cursor, a status document that survives the request that started the job, and
a TTL so a crashed backend cannot pin a job in "running" forever.

The alternative was a second copy of the same 60 lines. Two copies of a
progress protocol drift the moment one side learns a new status, and the UI
poller is the thing that breaks.

Key layout: ``mc:<namespace>:<entity_id>:log`` / ``…:status``. The namespace
carries the colons of the old hand-written keys (``host:bootstrap``) so this
refactor did not rename a single Redis key.

Everything that outlives a run is in Redis on purpose — a poll served by
another backend worker must see the same picture as the one that started it.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from app.redis_client import get_redis

# Log + status expire together — a run whose backend died must not leave its
# subject looking permanently busy.
DEFAULT_LOG_TTL = 3600
DEFAULT_STATUS_TTL = 3600

# The one status every job shares. Terminal values are per-job (a bootstrap can
# end in ``needs_sudo``), so only the running state lives here.
STATUS_RUNNING = "running"
STATUS_IDLE = "idle"

# Reserved keys in the status document — everything else a caller passes in
# ``extra`` is merged into the polled response verbatim.
_RESERVED = ("status", "phase", "message", "updated_at")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobLog:
    """The Redis side of one background job.

    ``namespace`` groups a job type (``host:bootstrap``, ``recipe:install``),
    ``entity_id`` identifies the subject (a host id, a host+slug pair).
    """

    def __init__(
        self,
        namespace: str,
        entity_id: str,
        *,
        log_ttl: int = DEFAULT_LOG_TTL,
        status_ttl: int = DEFAULT_STATUS_TTL,
        logger: logging.Logger | None = None,
    ) -> None:
        self.namespace = namespace
        self.entity_id = str(entity_id)
        self.log_ttl = log_ttl
        self.status_ttl = status_ttl
        self._logger = logger or logging.getLogger(f"mc.{namespace.replace(':', '.')}")

    # ── keys ────────────────────────────────────────────────────────────────
    @property
    def log_key(self) -> str:
        return f"mc:{self.namespace}:{self.entity_id}:log"

    @property
    def status_key(self) -> str:
        return f"mc:{self.namespace}:{self.entity_id}:status"

    # ── writing ─────────────────────────────────────────────────────────────
    async def append(self, text: str, level: str = "info") -> None:
        redis = await get_redis()
        entry = json.dumps({"ts": time.time(), "level": level, "text": text})
        await redis.rpush(self.log_key, entry)
        await redis.expire(self.log_key, self.log_ttl)
        self._logger.info("%s[%s] %s: %s", self.namespace, self.entity_id, level, text)

    async def set_status(
        self,
        status: str,
        *,
        phase: str,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        redis = await get_redis()
        doc: dict[str, Any] = {
            "status": status,
            "phase": phase,
            "message": message,
            "updated_at": _now_iso(),
        }
        doc.update(extra or {})
        await redis.set(self.status_key, json.dumps(doc), ex=self.status_ttl)

    async def reset(self) -> None:
        """Drop the previous run's lines. Status is overwritten by the caller."""
        redis = await get_redis()
        await redis.delete(self.log_key)

    # ── reading ─────────────────────────────────────────────────────────────
    async def get_status(self) -> dict | None:
        """The status document, or None when no run was ever started (or the
        TTL expired)."""
        redis = await get_redis()
        raw = await redis.get(self.status_key)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None

    async def is_running(self) -> bool:
        doc = await self.get_status()
        return bool(doc and doc.get("status") == STATUS_RUNNING)

    async def read(self, cursor: int = 0) -> dict:
        """Log lines from ``cursor`` on, plus the current status.

        One response for the poller: status and lines come from the same read,
        so the UI can never show "done" while still missing the last lines (or
        the reverse). ``cursor`` is a plain index into the append-only list —
        the caller sends back what it got and gets exactly the new lines.
        """
        redis = await get_redis()
        cursor = max(0, int(cursor))
        raw_lines = await redis.lrange(self.log_key, cursor, -1)
        lines = []
        for raw in raw_lines:
            try:
                lines.append(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                lines.append({"ts": None, "level": "info", "text": str(raw)})

        doc = await self.get_status() or {}
        status = doc.get("status") or STATUS_IDLE
        passthrough = {k: v for k, v in doc.items() if k not in _RESERVED}
        return {
            "status": status,
            "phase": doc.get("phase"),
            "message": doc.get("message"),
            "running": status == STATUS_RUNNING,
            "lines": lines,
            "cursor": cursor + len(lines),
            **passthrough,
        }
