"""Update check against the public repo's GitHub releases.

A self-hosted product needs an update story: MC checks (cached daily,
never in the request hot path without a cache TTL) the latest release and
shows a hint in the UI. No auto-update — the operator
decides (install.sh --update / docs/setup/updating.md).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable, Optional

import httpx

from app.redis_client import get_redis

logger = logging.getLogger("mc.update_check")

RELEASES_URL = (
    "https://api.github.com/repos/argyelan-ai/mission-control/releases/latest"
)
CACHE_KEY = "mc:update-check:latest"
CACHE_TTL = 86400  # once daily is enough; protects the GitHub rate limit (60/h anon)

_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse(tag: Optional[str]) -> Optional[tuple[int, int, int]]:
    if not tag:
        return None
    m = _VER.match(tag.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(candidate: Optional[str], current: Optional[str]) -> bool:
    """True if candidate has a higher SemVer than current.

    Unparseable/missing versions are never "newer" — a broken
    GitHub response must never trigger a false update banner.
    """
    c, cur = _parse(candidate), _parse(current)
    if c is None or cur is None:
        return False
    return c > cur


async def get_latest_release(
    _fetch: Optional[Callable[[], Awaitable[dict]]] = None,
) -> dict:
    """Latest release info, cached for 24h. Error → {tag: None} (silent)."""
    redis = await get_redis()
    cached = await redis.get(CACHE_KEY)
    if cached:
        try:
            return json.loads(cached)
        except (TypeError, ValueError):
            pass

    info: dict = {"tag": None, "url": None}
    try:
        if _fetch is not None:
            data = await _fetch()
        else:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(
                    RELEASES_URL,
                    headers={"Accept": "application/vnd.github+json"},
                )
                r.raise_for_status()
                data = r.json()
        info = {"tag": data.get("tag_name"), "url": data.get("html_url")}
    except Exception as e:  # noqa: BLE001 — offline/ratelimited is normal
        logger.debug("update check failed (harmless): %s", e)

    await redis.set(CACHE_KEY, json.dumps(info), ex=CACHE_TTL)
    return info
