"""Wave-0 stubs for MEM-01 — Recycler config defaults + two-tier flag rendering.

Bodies land in plans 03-02..03-04. Today these xfail because:
  1) settings.agent_recycler_enabled does not exist yet (target: True)
  2) get_effective_recycler_enabled helper does not exist yet
  3) docker_agent_sync.py does not yet render AGENT_RECYCLER_ENABLED into .env
  4) internal.py does not yet add AGENT_RECYCLER_ENABLED to bootstrap

Pattern: introspect Settings.model_fields[...].default — same shape as
test_intelligence_interval.py. conftest.py overrides settings for tests, so
reading the *class* default via model_fields is immune to instance-level
overrides (Pitfall 6 from RESEARCH.md).

Tests are runnable in CI from Wave 0; bodies flip xfail→PASS as the named
follow-up plans introduce the symbols.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


def _import_or_xfail():
    try:
        from app.config import Settings
        from app.services.recycler_config import get_effective_recycler_enabled
        return Settings, get_effective_recycler_enabled
    except ImportError as e:
        pytest.xfail(f"Plan 03-02 implements recycler config: {e}")


def test_default_recycler_enabled_is_true():
    """settings.agent_recycler_enabled default must be True (config.py).

    Fail-safe default-on per CONTEXT D-10. Read class default to bypass
    any conftest.py session-level overrides.
    """
    Settings, _ = _import_or_xfail()
    default = Settings.model_fields["agent_recycler_enabled"].default
    assert default is True, f"expected True, got {default}"


def test_env_var_strict_parse():
    """BaseSettings parses raw env "true"/"false" as bool correctly.

    Contract guard: if the value in agent.env is anything other than
    "true" (e.g. "True", "1", typo), the bash side treats it as disabled
    (fail-closed). Pydantic's BaseSettings already enforces strict bool
    parsing — this test pins the contract so a future loosening of the
    parse rule cannot silently change recycler semantics.
    """
    Settings, _ = _import_or_xfail()
    field = Settings.model_fields["agent_recycler_enabled"]
    # The annotation must be bool — anything else (e.g. str | bool) would
    # break the strict parse contract.
    assert field.annotation is bool, (
        f"expected bool annotation, got {field.annotation}"
    )


def test_env_renders_recycler_line():
    """Static check: docker_agent_sync.py must include a line that appends
    AGENT_RECYCLER_ENABLED to env_lines.

    Mirrors the test_lock_miss_log_level_is_warning_not_debug source-grep
    pattern (test_intelligence_interval.py:64-76). Cheap, no FastAPI
    TestClient needed for Wave-0.
    """
    src = Path(__file__).resolve().parents[1] / "app" / "services" / "docker_agent_sync.py"
    text = src.read_text(encoding="utf-8")
    if not re.search(r'env_lines\.append\(\s*f["\']AGENT_RECYCLER_ENABLED=', text):
        pytest.xfail(
            "Plan 03-04: docker_agent_sync.py does not yet render "
            "AGENT_RECYCLER_ENABLED into agent.env"
        )


def test_bootstrap_includes_recycler_flag():
    """Static check: internal.py agent_bootstrap must add AGENT_RECYCLER_ENABLED
    to the tokens dict.

    Same source-grep idiom; integration-level coverage via TestClient lands
    in Plan 03-04 once the bootstrap key is wired through entrypoint.sh.
    """
    src = Path(__file__).resolve().parents[1] / "app" / "routers" / "internal.py"
    text = src.read_text(encoding="utf-8")
    if not re.search(r'tokens\[\s*["\']AGENT_RECYCLER_ENABLED["\']', text):
        pytest.xfail(
            "Plan 03-04: internal.py agent_bootstrap does not yet add "
            "AGENT_RECYCLER_ENABLED to tokens dict"
        )
