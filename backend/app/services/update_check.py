"""Update-Check gegen die GitHub-Releases des Public-Repos.

Ein Self-Hosted-Produkt braucht eine Update-Story: MC prueft (taeglich
gecacht, nie im Request-Hotpath ohne Cache-TTL) das neueste Release und
zeigt in der UI einen Hinweis. Kein Auto-Update — der Operator
entscheidet (install.sh --update / docs/setup/updating.md).
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
CACHE_TTL = 86400  # 1x taeglich reicht; schont das GitHub-Rate-Limit (60/h anon)

_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _parse(tag: Optional[str]) -> Optional[tuple[int, int, int]]:
    if not tag:
        return None
    m = _VER.match(tag.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(candidate: Optional[str], current: Optional[str]) -> bool:
    """True wenn candidate eine hoehere SemVer als current ist.

    Unparsebare/fehlende Versionen sind nie "neuer" — ein kaputter
    GitHub-Response darf keinen falschen Update-Banner ausloesen.
    """
    c, cur = _parse(candidate), _parse(current)
    if c is None or cur is None:
        return False
    return c > cur


async def get_latest_release(
    _fetch: Optional[Callable[[], Awaitable[dict]]] = None,
) -> dict:
    """Neueste Release-Info, 24h-gecacht. Fehler → {tag: None} (still)."""
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
    except Exception as e:  # noqa: BLE001 — offline/ratelimited ist normal
        logger.debug("update check failed (harmless): %s", e)

    await redis.set(CACHE_KEY, json.dumps(info), ex=CACHE_TTL)
    return info
