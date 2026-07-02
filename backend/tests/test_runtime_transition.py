"""Runtime-transition tests (TST-03).

4 boundary cases for workspace_path_for_runtime.

Production code references:
  - backend/app/services/runtime_context.py:60 — workspace_path_for_runtime
  - backend/app/services/dispatch.py:142 — traversal guard (ADR-023)
  - backend/app/services/dispatch.py:151 — legacy openclaw regex
"""
import pytest

from app.services.runtime_context import workspace_path_for_runtime


class _FakeCliBridgeAgent:
    agent_runtime = "cli-bridge"
    name = "cody"


class _FakeOpenclawAgent:
    agent_runtime = "openclaw"
    name = "henry"


def test_workspace_path_traversal_returns_mount_root():
    """ADR-023 guard: `..` segment in cli-bridge path → /workspace fallback.

    Production code: backend/app/services/dispatch.py:142
    """
    # Traversal attempt — must NOT escape /workspace
    result = workspace_path_for_runtime(
        _FakeCliBridgeAgent(),
        "/Users/testuser/.mc/workspaces/evil/../../etc/passwd",
    )
    # The guard at dispatch.py:142 logs warning + returns "/workspace"
    # (or a normalized path that does NOT contain `..`).
    assert ".." not in result, f"Traversal escaped: {result}"
    assert result == "/workspace" or result.startswith("/workspace"), (
        f"Expected /workspace fallback, got: {result}"
    )


def test_workspace_path_legacy_openclaw_pattern():
    """Legacy ~/.openclaw/workspace-<slug>/ → /workspace/...

    Production code: backend/app/services/dispatch.py:151 (elif branch)
    """
    out = workspace_path_for_runtime(
        _FakeCliBridgeAgent(),
        "/Users/testuser/.openclaw/workspace-cody/proj/file.py",
    )
    assert out is not None
    assert out.startswith("/workspace"), f"Legacy openclaw path not translated: {out}"
    assert "proj/file.py" in out, f"Path tail dropped: {out}"


def test_workspace_path_unmatched_returns_passthrough():
    """Host path NOT under .mc/workspaces/ AND NOT under .openclaw/workspace- → passthrough.

    Production behavior: returns input as-is (or near-as-is) for unmapped paths.
    """
    unmapped = "/Users/testuser/random-folder/file.txt"
    out = workspace_path_for_runtime(_FakeCliBridgeAgent(), unmapped)
    # Production may return input verbatim OR a /workspace stub — either is acceptable
    # as long as it doesn't crash and doesn't lose the path tail.
    assert out is not None
    assert out == unmapped or "file.txt" in out, f"Unmapped path mishandled: {out}"


def test_openclaw_runtime_no_translation():
    """openclaw runtime → workspace_path_for_runtime returns input as-is (gateway translates)."""
    host_path = "/Users/testuser/.mc/workspaces/henry/proj/file.py"
    out = workspace_path_for_runtime(_FakeOpenclawAgent(), host_path)
    # Openclaw runtime: gateway handles path translation, MC passes through
    assert out == host_path, f"openclaw should passthrough, got: {out}"


