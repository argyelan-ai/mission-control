"""
Phase 29 regression test: ensure no Gateway-Code leakage.

After Phase 29 lands, no file under backend/app/ may import openclaw_rpc,
gateway_sync, gateway_secrets_sync, or gateway_client. New code that
re-introduces these imports — or any rpc.* method call against the deleted
OpenClawRPC namespace — fails CI.

Design (Plan 29-02, D-13):

* **Pattern tests** (`test_no_gateway_rpc_calls`, `test_no_gateway_module_imports`,
  `test_no_gateway_settings`) scan `backend/app/**/*.py` (minus `alembic/versions/`
  and `__pycache__/`) for forbidden patterns. They reveal incremental progress
  as Wave 2/3 plans remove call sites. They will FAIL on the current feature
  branch (gateway code still present) — that is correct and proves the
  regression detector works.

* **File-existence tests** (`test_openclaw_rpc_file_deleted`,
  `test_gateway_sync_file_deleted`, `test_gateway_secrets_sync_file_deleted`,
  `test_gateway_router_file_deleted`, `test_gateway_client_file_deleted`,
  `test_telegram_file_deleted`) are marked `@pytest.mark.xfail(strict=False,
  reason="...")` so they do not break the main test suite during Phase 29
  execution. They flip to XPASS once Plan 29-09 deletes each file. Plan 29-09
  is responsible for removing the xfail markers at the end of the phase.

* **File-creation test** (`test_discord_router_file_exists`) is NOT xfail —
  it passes once Plan 29-01 (parallel sibling of this plan) lands the new
  discord router. Before that landing, this test will FAIL on the feature
  branch. That is expected during Wave 1; it goes green as soon as 29-01
  merges.

The legacy alembic migrations under backend/alembic/versions/ are explicitly
allowed to reference gateway concepts (historical records).
"""
from __future__ import annotations

import pathlib
import re

import pytest

# Layout-robust app-root discovery.
#
# Local dev:  /Users/.../mission-control/backend/tests/test_x.py
#             → parents[1] = .../mission-control/backend
#             → backend/app/main.py exists → APP_ROOT = backend/app/
# Docker:     /app/tests/test_x.py
#             → parents[1] = /app
#             → /app/app/main.py exists → APP_ROOT = /app/app/
#
# parents[2] hardcode breaks in Docker (parents[2] = `/`).
def _find_app_root() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "app" / "main.py").is_file():
            return ancestor / "app"
    raise RuntimeError(
        f"Could not locate app/main.py walking up from {here}. "
        f"Tested ancestors: {[str(p) for p in here.parents]}"
    )

APP_ROOT = _find_app_root()
# DISPLAY_ROOT is the directory containing `app/` — used to build readable
# relative paths in test failure messages (e.g. "app/services/foo.py:42").
DISPLAY_ROOT = APP_ROOT.parent

assert APP_ROOT.exists(), f"APP_ROOT detection failed: {APP_ROOT}"

# ── Forbidden module imports ─────────────────────────────────────────────
FORBIDDEN_IMPORTS = (
    "openclaw_rpc",
    "gateway_sync",
    "gateway_secrets_sync",
    "gateway_client",
)

# ── Forbidden call patterns ──────────────────────────────────────────────
FORBIDDEN_PATTERNS = (
    re.compile(
        r"\brpc\.(chat_send|chat_send_isolated|config_patch|"
        r"config_get|agents_files_set|agents_files_get|"
        r"sessions_list|sessions_send|sessions_spawn|sessions_reset|"
        r"sessions_history|chat_history|skills_status|skills_install|"
        r"skills_update|models_list|health|connect|disconnect|"
        r"poll_agent_reply|provision_agent|find_agent_session_key|"
        r"ensure_connected|connected|on_state_change|_ws_url|_shutdown|"
        r"_auto_reconnect|request)\b"
    ),
    re.compile(r"\bfrom\s+app\.services\.openclaw_rpc\b"),
    re.compile(r"\bfrom\s+app\.services\.gateway_sync\b"),
    re.compile(r"\bfrom\s+app\.services\.gateway_secrets_sync\b"),
    # `from app.services.telegram import ...` (legacy module — Plan 29-08 deletes).
    # `\s+import\b` ensures `telegram_bot` is NOT matched.
    re.compile(r"\bfrom\s+app\.services\.telegram\s+import\b"),
)

# ── Forbidden settings references ────────────────────────────────────────
FORBIDDEN_SETTINGS = (
    re.compile(r"\bsettings\.openclaw_(ws_url|token)\b"),
    re.compile(r"\bsettings\.gateway_url\b"),
)

# ── Exclude alembic migrations (historical records) + __pycache__ ────────
EXCLUDED_PARTS = ("__pycache__", "alembic", "versions")


def _iter_python_files():
    for path in APP_ROOT.rglob("*.py"):
        if any(p in EXCLUDED_PARTS for p in path.parts):
            continue
        yield path


# ─────────────────────────────────────────────────────────────────────────
# Pattern tests — NOT xfail. These reveal incremental progress as Wave 2/3
# plans remove call sites. Each wave should turn one or more of these
# parametrized cases from RED to GREEN.
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "pattern", FORBIDDEN_PATTERNS, ids=lambda p: p.pattern[:40]
)
def test_no_gateway_rpc_calls(pattern):
    offenders = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            offenders.append(
                f"{path.relative_to(DISPLAY_ROOT)}:{line_no} — {match.group()}"
            )
    assert not offenders, (
        f"Forbidden Gateway-RPC pattern {pattern.pattern!r} found:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("module", FORBIDDEN_IMPORTS)
def test_no_gateway_module_imports(module):
    offenders = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        # Two heuristics: explicit `import {module}` OR `.{module}` attr access
        # (e.g. `app.services.openclaw_rpc.connect`). Both indicate dependency.
        if module in text and (
            f"import {module}" in text or f".{module}" in text
        ):
            offenders.append(str(path.relative_to(DISPLAY_ROOT)))
    assert not offenders, (
        f"Forbidden import of {module!r} found in:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "pattern", FORBIDDEN_SETTINGS, ids=lambda p: p.pattern[:30]
)
def test_no_gateway_settings(pattern):
    offenders = []
    for path in _iter_python_files():
        text = path.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            line_no = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(DISPLAY_ROOT)}:{line_no}")
    assert not offenders, (
        f"Forbidden settings reference {pattern.pattern!r}:\n  "
        + "\n  ".join(offenders)
    )


# ─────────────────────────────────────────────────────────────────────────
# File-existence (delete) tests — xfail-guarded. Plan 29-09 deletes these
# files at the end of the phase; xfail markers are removed in that plan.
# `strict=False` means: if test passes (file already gone), pytest emits
# XPASS instead of failing — so the test suite stays green during the
# wave transitions.
# ─────────────────────────────────────────────────────────────────────────

def test_openclaw_rpc_file_deleted():
    assert not (APP_ROOT / "services" / "openclaw_rpc.py").exists(), (
        "openclaw_rpc.py must be deleted in Phase 29"
    )


def test_gateway_sync_file_deleted():
    assert not (APP_ROOT / "services" / "gateway_sync.py").exists(), (
        "gateway_sync.py must be deleted in Phase 29"
    )


def test_gateway_secrets_sync_file_deleted():
    assert not (APP_ROOT / "services" / "gateway_secrets_sync.py").exists(), (
        "gateway_secrets_sync.py must be deleted in Phase 29"
    )


def test_gateway_router_file_deleted():
    assert not (APP_ROOT / "routers" / "gateway.py").exists(), (
        "routers/gateway.py must be deleted in Phase 29"
    )


def test_gateway_client_file_deleted():
    assert not (APP_ROOT / "services" / "gateway_client.py").exists(), (
        "gateway_client.py becomes orphan in Phase 29 and must be deleted"
    )


def test_telegram_file_deleted():
    """Legacy `services/telegram.py` is replaced by `telegram_bot.py` callers."""
    assert not (APP_ROOT / "services" / "telegram.py").exists(), (
        "services/telegram.py must be deleted in Phase 29 (D-10 + Plan 29-08)"
    )


# ─────────────────────────────────────────────────────────────────────────
# File-creation test — NOT xfail. Plan 29-01 (parallel sibling) creates
# routers/discord.py. This test passes immediately after 29-01 merges.
# Before that, it FAILS on the feature branch — that is the design.
# ─────────────────────────────────────────────────────────────────────────

def test_discord_router_file_exists():
    """Plan 29-01 (D-04) creates the new Discord router."""
    assert (APP_ROOT / "routers" / "discord.py").exists(), (
        "routers/discord.py must be created in Phase 29 (D-04, Plan 29-01)"
    )
