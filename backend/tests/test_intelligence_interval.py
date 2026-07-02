"""Wave-0 stubs for MEM-05 — Intelligence interval default + lock fail-fast.

Bodies land in plan 02-04. Today these xfail because:
  1) settings.intelligence_interval default is still 300 (target: 600)
  2) IntelligenceConfig.interval_seconds default is still 300 (target: 600)
  3) intelligence.py:113 still uses logger.debug for lock-miss (target: warning)

Pattern: introspect Settings.model_fields[...].default — conftest.py:35
overrides intelligence_interval=99999 for tests so we cannot use
`settings.intelligence_interval` directly (Pitfall 6 from RESEARCH.md).
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest


def _import_or_xfail():
    try:
        from app.config import Settings
        from app.routers.system import IntelligenceConfig
        from app.services.intelligence import IntelligenceService
        return Settings, IntelligenceConfig, IntelligenceService
    except ImportError as e:
        pytest.xfail(f"Plan 02-04 implements MEM-05 changes: {e}")


def test_default_interval_is_600():
    """settings.intelligence_interval default must be 600 (config.py:115)."""
    Settings, _, _ = _import_or_xfail()
    default = Settings.model_fields["intelligence_interval"].default
    if default == 300:
        pytest.xfail("Plan 02-04: config.py:115 not yet flipped 300→600")
    assert default == 600, f"expected 600, got {default}"


def test_intelligenceconfig_default_is_600():
    """IntelligenceConfig.interval_seconds default must be 600 (system.py:210)."""
    _, IntelligenceConfig, _ = _import_or_xfail()
    default = IntelligenceConfig.model_fields["interval_seconds"].default
    if default == 300:
        pytest.xfail("Plan 02-04: system.py:210 not yet flipped 300→600")
    assert default == 600, f"expected 600, got {default}"


@pytest.mark.asyncio
async def test_two_singletons_one_acks_one_skips(fake_redis):
    """Two singletons race for the Redis lock: exactly one wins, the other returns False."""
    _, _, IntelligenceService = _import_or_xfail()
    svc1 = IntelligenceService(interval=99999)
    svc2 = IntelligenceService(interval=99999)
    with patch("app.services.intelligence.get_redis", return_value=fake_redis):
        results = await asyncio.gather(svc1._acquire_lock(), svc2._acquire_lock())
    assert sum(1 for r in results if r) == 1, (
        f"exactly one singleton must hold the lock, got {results}"
    )


def test_lock_miss_log_level_is_warning_not_debug():
    """Static check on the source: the lock-miss path must emit at WARNING level,
    not DEBUG. Read intelligence.py and grep for the bad pattern.
    """
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "intelligence.py"
    text = src.read_text(encoding="utf-8")
    bad = re.search(r"logger\.debug\([^)]*another worker holds the lock", text)
    if bad:
        pytest.xfail("Plan 02-04: intelligence.py log level not yet flipped to warning")
    # Also enforce that the new WARN message exists once we're past the flip.
    assert re.search(r'logger\.warning\(\s*["\']intelligence: lock contention, skipping cycle', text), (
        "expected WARN log 'intelligence: lock contention, skipping cycle' in intelligence.py"
    )
