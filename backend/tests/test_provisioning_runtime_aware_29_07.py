"""Phase 29-07 Task 5: provisioning.py refactored to runtime-aware delegation.

- provision_agent_background dispatches by agent_runtime:
  - cli-bridge: write_compose_agents + sync_docker_agent_files + mark provisioned
  - host: mark provisioned (Boss runs on host, no-op)
  - other/None: warn, mark provision_status = 'local'
- sync_agent_skills_to_gateway / sync_agent_model_to_gateway DELETED
- cleanup_sync_ghosts DELETED in Plan 30-01 (DB-Cleanup follow-up; the
  only consumer was the startup Gateway-Sync, which Phase 29 removed)
- No openclaw_rpc / gateway_sync / gateway_secrets_sync imports
"""
from __future__ import annotations

import pathlib

import pytest


def test_provisioning_has_no_rpc_or_gateway_imports() -> None:
    """provisioning.py must not import any Gateway-side module."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "provisioning.py"
    ).read_text(encoding="utf-8")

    assert "openclaw_rpc" not in src
    assert "gateway_sync" not in src
    assert "gateway_secrets_sync" not in src
    bad_calls = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "rpc." in line and "openclaw_rpc" not in line:
            bad_calls.append(f"{line_no}: {line.strip()}")
    assert not bad_calls, "rpc.* calls remain:\n" + "\n".join(bad_calls)


def test_provisioning_deleted_gateway_sync_helpers() -> None:
    """sync_agent_skills_to_gateway and sync_agent_model_to_gateway must be deleted."""
    from app.services import provisioning

    assert not hasattr(provisioning, "sync_agent_skills_to_gateway"), (
        "sync_agent_skills_to_gateway must be deleted (D-11)"
    )
    assert not hasattr(provisioning, "sync_agent_model_to_gateway"), (
        "sync_agent_model_to_gateway must be deleted (D-11)"
    )


def test_provisioning_dropped_cleanup_sync_ghosts() -> None:
    """Plan 30-01: cleanup_sync_ghosts deleted — no live consumers post-Phase-29."""
    from app.services import provisioning

    assert not hasattr(provisioning, "cleanup_sync_ghosts"), (
        "cleanup_sync_ghosts must be removed (Plan 30-01 — was dead code)"
    )


def test_provision_agent_background_callable() -> None:
    """provision_agent_background must still be exported and callable."""
    from app.services.provisioning import provision_agent_background

    assert callable(provision_agent_background)


def test_no_external_orphan_imports() -> None:
    """No file under backend/app/ may still import the (deleted) gateway-sync
    helpers from app.services.provisioning. Local shims with the same NAME are
    OK as a Plan 29-07 stop-gap until Plan 29-05 drops the call sites.
    """
    app_root = pathlib.Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    targets = ("sync_agent_skills_to_gateway", "sync_agent_model_to_gateway")
    for path in app_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        # provisioning.py itself must not contain the names at all.
        if path.name == "provisioning.py":
            text = path.read_text(encoding="utf-8")
            for token in targets:
                if token in text:
                    offenders.append(
                        f"{path.relative_to(app_root.parent)}: still defines '{token}'"
                    )
            continue
        # Other files: ban the import-from-provisioning pattern; local shim defs allowed.
        text = path.read_text(encoding="utf-8")
        if "from app.services.provisioning import" in text:
            for token in targets:
                # Detect token in any import-from-provisioning block.
                if (
                    f"import {token}" in text
                    or f", {token}" in text
                    or f"{token},\n" in text
                    or f"{token}\n" in text.split("from app.services.provisioning import", 1)[1][:400]
                ):
                    offenders.append(
                        f"{path.relative_to(app_root.parent)}: imports '{token}' from provisioning"
                    )
    assert not offenders, "Orphan gateway-sync imports remain:\n" + "\n".join(offenders)
