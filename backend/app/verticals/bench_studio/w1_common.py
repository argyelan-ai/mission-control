"""Bench W1 — shared constants and small pure helpers.

Kept free of DB and HTTP so every other W1 module (plan, runner, receipt,
kit) can use them and the tests can check them directly. Public artefact
words are English by decision (24.10.2026); UI words are codes here and the
frontend words them through i18n.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .orchestrator import GENERATION_SYSTEM_PROMPT

PROTOCOL_V1 = "v1"

#: Class A request knobs (concept §5.1, operator default "temperature 0.7").
TEMPERATURE = 0.7
BASE_MAX_TOKENS = 16384
#: Video length of the one recorded run (preset, concept §4 "Video length
#: field" -> preset 20 s).
VIDEO_DURATION_S = 20
#: At most two contestants share one video (W1 kill list: no 3-6 grid).
MAX_VIDEO_CONTESTANTS = 2
#: The run that becomes the video — fixed before the run starts (§5.4).
VIDEO_RUN_INDEX = 1

#: Presets (concept W1.5): runs per class A contestant.
PRESET_RUNS = {"standard": 3, "quick": 1, "effort_ladder": 3, "rerun": 3}

#: Quiet box (concept §5.4): serving_since >= 15 min, engine idle, no fleet
#: usage on that runtime in the last 2 min; wait at most 10 min for "busy",
#: then run and mark the gate amber.
FRESH_MIN = 15
BUSY_WINDOW_S = 120
QUIET_WAIT_MAX_S = 600

#: Class B (agent builds): one run, a time limit printed on the receipt.
AGENT_TIME_LIMIT_MIN = 120

#: D4: above this a paid test asks a second time.
COST_CAP_USD = 1.0

#: Auto test after a recipe switch: give up when the switched model never
#: answers (aborted: switch_failed).
AUTO_TEST_SWITCH_TIMEOUT_MIN = 45

#: A "larger budget" re-run doubles the token budget (truncation warning).
LARGER_BUDGET_FACTOR = 2

SYSTEM_PROMPT = GENERATION_SYSTEM_PROMPT


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


SYSTEM_PROMPT_SHA = sha256_text(SYSTEM_PROMPT)


def canonical_json(value: Any) -> str:
    """Stable JSON for checksums: sorted keys, no spaces, ISO datetimes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_aware(value).isoformat()
    return str(value)


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive UTC datetimes; Postgres aware ones."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── Numbers as the public artefacts print them (English) ──────────────────


def fmt_clock(ms: float | int | None) -> str | None:
    """'1:06' for class A compute time (m:ss), '1:02:05' past an hour."""
    if ms is None:
        return None
    total = int(round(ms / 1000))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fmt_minutes(ms: float | int | None) -> str | None:
    """'92 min' for class B work time; '45 s' below a minute."""
    if ms is None:
        return None
    seconds = ms / 1000
    if seconds < 60:
        return f"{int(round(seconds))} s"
    return f"{int(round(seconds / 60))} min"


def fmt_tokens(n: float | int | None) -> str | None:
    """'6.6k' / '264k' / '1.1M' / '412'."""
    if n is None:
        return None
    n = float(n)
    if n < 1_000:
        return str(int(round(n)))
    if n < 100_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    if n < 1_000_000:
        return f"{int(round(n / 1_000))}k"
    return f"{n / 1_000_000:.1f}M".replace(".0M", "M")


def fmt_rate(v: float | None) -> str | None:
    """'101' / '99.5' tok/s (one decimal only below 100)."""
    if v is None:
        return None
    if v >= 100:
        return str(int(round(v)))
    return f"{v:.1f}".rstrip("0").rstrip(".")


def fmt_usd(v: float | None) -> str | None:
    if v is None:
        return None
    if v < 0.01:
        return "<$0.01"
    return f"${v:.2f}"


def fmt_seconds(ms: float | int | None) -> str | None:
    """'0.4 s' (TTFT)."""
    if ms is None:
        return None
    return f"{ms / 1000:.1f} s"


def median(values: Iterable[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2


# ── Privacy (gate rule R4) ────────────────────────────────────────────────

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")
_PATH = re.compile(r"(?:/Users/|/home/|/root/|/shared-deliverables|/var/|/tmp/|~/|[A-Za-z]:\\)")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_HOSTISH = re.compile(r"\b[a-z0-9-]+\.(?:local|lan|internal|ts\.net|home)\b", re.I)


def privacy_hits(texts: Iterable[str], private_words: Iterable[str] = ()) -> list[str]:
    """What in ``texts`` must never reach a public artefact (R4): IPs, file
    paths, UUIDs, LAN host names and the operator's private words (user
    names, host slugs, agent names). Returns the hit KINDS, never the
    private value itself (the gate reason is shown in the UI)."""
    words = [w.strip() for w in private_words if w and len(w.strip()) >= 3]
    hits: list[str] = []
    for text in texts:
        if not text:
            continue
        if _IPV4.search(text) or _IPV6.search(text):
            hits.append("ip")
        if _PATH.search(text):
            hits.append("path")
        if _UUID.search(text):
            hits.append("uuid")
        if _HOSTISH.search(text):
            hits.append("hostname")
        low = text.casefold()
        for word in words:
            if re.search(r"(?<![a-z0-9])" + re.escape(word.casefold()) + r"(?![a-z0-9])", low):
                hits.append("private_word")
                break
    seen: list[str] = []
    for h in hits:
        if h not in seen:
            seen.append(h)
    return seen


def blocked_network(record: dict[str, Any]) -> str | None:
    """What the recorder's network block cost the page (live finding
    25.10.2026): ``"script"`` = a script/module was blocked, so the page's
    own code (e.g. three.js from a CDN) never ran and the scene stayed
    empty; ``"assets"`` = only fonts/images/stylesheets were blocked;
    None = nothing blocked or no block recorded. A record that counts
    attempts but not their type (older sidecar) cannot prove the scene
    drew, so it counts as ``"script"``."""
    attempts = record.get("network_attempts") or 0
    if not record.get("network_blocked") or attempts <= 0:
        return None
    scripts = record.get("network_blocked_scripts")
    if scripts is None or scripts > 0:
        return "script"
    return "assets"
