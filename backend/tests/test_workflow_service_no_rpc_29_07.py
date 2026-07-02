"""Phase 29-07 Task 4: workflow_service.py removes rpc.chat_send_isolated +
rpc.chat_history. LLM steps stubbed for Phase 31 cli-bridge async pattern;
deterministic step types (internal_api, webhook, script_ref) unchanged.
"""
from __future__ import annotations

import pathlib


def test_workflow_service_has_no_rpc_imports() -> None:
    """workflow_service.py must not import openclaw_rpc after refactor."""
    src = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "workflow_service.py"
    ).read_text(encoding="utf-8")

    assert "openclaw_rpc" not in src
    bad_calls = []
    for line_no, line in enumerate(src.splitlines(), start=1):
        if "rpc." in line and "openclaw_rpc" not in line:
            bad_calls.append(f"{line_no}: {line.strip()}")
    assert not bad_calls, "rpc.* calls remain:\n" + "\n".join(bad_calls)


def test_workflow_service_imports_cleanly() -> None:
    """Module must import after refactor."""
    import importlib

    import app.services.workflow_service as ws
    importlib.reload(ws)

    assert hasattr(ws, "workflow_service")
    # WorkflowService public API
    assert hasattr(ws.workflow_service, "list_workflows")
    assert hasattr(ws.workflow_service, "start_run")
    assert hasattr(ws.workflow_service, "execute_run")
