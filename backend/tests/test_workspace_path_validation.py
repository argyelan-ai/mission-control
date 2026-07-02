"""Tests: is_backend_writable_path Helper + Dispatch Pre-Check.

Incident-Context 2026-04-23 (DNA-Task, Boss): Boss hatte workspace_path=
<home>/Workspace — nicht im Backend-Container gemounted. Git-Clone
crashte mit PermissionError, der Operator bekam kryptische Blocker-Meldung.
"""

from pathlib import Path

import pytest

from app.services.dispatch import is_backend_writable_path

# is_backend_writable_path prefixes against settings.home_host, which
# defaults to the real Path.home() — match that dynamically instead of a
# hardcoded machine-specific path.
HOME = str(Path.home())


class TestIsBackendWritablePath:
    """Purely-stateless path classification."""

    def test_none_path_is_not_writable(self):
        assert is_backend_writable_path(None) is False

    def test_empty_path_is_not_writable(self):
        assert is_backend_writable_path("") is False

    def test_mc_workspaces_path_is_writable(self):
        """Standard-Pattern fuer alle cli-bridge Agents."""
        assert is_backend_writable_path(f"{HOME}/.mc/workspaces/rex") is True
        assert is_backend_writable_path(f"{HOME}/.mc/workspaces/boss/projects/xy") is True

    def test_openclaw_agent_path_no_longer_writable(self):
        """Stage-2-Entkopplung: ~/.openclaw Mount entfernt → nicht mehr beschreibbar.
        Aktuelle Agent-Workspaces liegen unter ~/.mc/ (siehe test_mc_workspaces_path_is_writable)."""
        assert is_backend_writable_path(f"{HOME}/.openclaw/agents/boss") is False

    def test_freecode_path_is_writable(self):
        """FreeCode legacy project-root."""
        assert is_backend_writable_path(f"{HOME}/FreeCode/projects/foo") is True

    def test_tmp_is_writable(self):
        """/tmp ist immer beschreibbar im Container."""
        assert is_backend_writable_path("/tmp/foo") is True

    def test_workspace_root_is_not_writable(self):
        """**Der Bug-Case:** <home>/Workspace ist NICHT gemounted."""
        assert is_backend_writable_path(f"{HOME}/Workspace") is False
        assert is_backend_writable_path(f"{HOME}/Workspace/Projects/mc") is False

    def test_other_host_paths_not_writable(self):
        """Alles ausserhalb der bekannten Mounts → False."""
        assert is_backend_writable_path("/etc/passwd") is False
        assert is_backend_writable_path("/opt/custom") is False
        assert is_backend_writable_path("/home/other-user") is False

    def test_traversal_attempts_rejected(self):
        """Path-Traversal via `..` wird normalisiert und dann gecheckt."""
        # normpath strips the `..` segments back past HOME entirely — not writable.
        assert is_backend_writable_path(
            f"{HOME}/.mc/workspaces/../../../../etc/passwd"
        ) is False

    def test_prefix_collision_not_matched(self):
        """<home>/.mcfoo soll NICHT als <home>/.mc matchen."""
        assert is_backend_writable_path(f"{HOME}/.mcfoo/bar") is False
        assert is_backend_writable_path(f"{HOME}/.openclawx/") is False

    def test_trailing_slash_handled(self):
        """Mit und ohne trailing slash identisches Verhalten."""
        assert is_backend_writable_path(f"{HOME}/.mc/workspaces/rex") is True
        assert is_backend_writable_path(f"{HOME}/.mc/workspaces/rex/") is True


# Note: Integration-Test (dispatch.py pre-check triggert RuntimeError mit
# richtiger Error-Message) erfordert volles Dispatch-Setup (Agent, Project,
# github_repo_url, Gateway-RPC Mocks) — das ist bereits via
# test_dispatch_* abgedeckt (die existierenden Dispatch-Tests laufen durch
# den Git-Setup-Block). Hier fokussieren wir auf den Helper-Contract, weil
# der eindeutig isoliert testbar ist.
