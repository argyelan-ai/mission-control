"""Tests fuer deliverable_paths.validate_deliverable_path."""
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.deliverable_paths import accepted_path_prefixes, validate_deliverable_path


TASK_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")

# accepted_path_prefixes() derives the host-form prefix from settings.home_host,
# which defaults to the real Path.home() — match that dynamically.
HOME = str(Path.home())


# ── accepted paths ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    f"/deliverables/{TASK_ID}/report.pdf",
    f"/deliverables/{TASK_ID}/sub/dir/file.docx",
    f"/shared-deliverables/{TASK_ID}/generated.pdf",
    f"/shared-deliverables/{TASK_ID}/screenshot.png",
    f"/shared-mcp/{TASK_ID}/capture.png",
    f"~/.mc/deliverables/{TASK_ID}/output.md",
    f"{HOME}/.mc/deliverables/{TASK_ID}/output.md",
])
def test_accepted_local_paths(path):
    validate_deliverable_path(path, None, TASK_ID)  # must not raise


@pytest.mark.parametrize("url", [
    "https://github.com/test-owner/repo/pull/42",
    "http://localhost:3000/preview",
])
def test_accepted_urls(url):
    validate_deliverable_path(url, None, TASK_ID)


def test_accepted_content_only():
    validate_deliverable_path(None, "# Report\n\nContent here.", TASK_ID)


# ── rejected paths ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/home/agent/report.pdf",
    "/workspace/output.md",
    "~/FreeCode/projects/foo.pdf",
    f"/deliverables/other-task-id/file.pdf",  # wrong task_id
    f"/shared-deliverables/other-task-id/file.pdf",
    "/tmp/file.pdf",
])
def test_rejected_wrong_prefix(path):
    with pytest.raises(HTTPException) as exc_info:
        validate_deliverable_path(path, None, TASK_ID)
    assert exc_info.value.status_code == 422


def test_rejected_empty_path_no_content():
    with pytest.raises(HTTPException) as exc_info:
        validate_deliverable_path(None, None, TASK_ID)
    assert exc_info.value.status_code == 422

    with pytest.raises(HTTPException):
        validate_deliverable_path(None, "   ", TASK_ID)


def test_rejected_nul_byte():
    path = f"/deliverables/{TASK_ID}/fi\x00le.pdf"
    with pytest.raises(HTTPException) as exc_info:
        validate_deliverable_path(path, None, TASK_ID)
    assert exc_info.value.status_code == 422
    assert "NUL" in exc_info.value.detail


def test_rejected_path_traversal():
    path = f"/deliverables/{TASK_ID}/../../etc/passwd"
    with pytest.raises(HTTPException) as exc_info:
        validate_deliverable_path(path, None, TASK_ID)
    assert exc_info.value.status_code == 422
    assert "Normalisierung" in exc_info.value.detail


def test_rejected_shared_deliverables_traversal():
    path = f"/shared-deliverables/{TASK_ID}/../../etc/shadow"
    with pytest.raises(HTTPException) as exc_info:
        validate_deliverable_path(path, None, TASK_ID)
    assert exc_info.value.status_code == 422


# ── accepted_path_prefixes helper ────────────────────────────────────────────

def test_accepted_prefixes_contains_shared_deliverables():
    prefixes = accepted_path_prefixes(TASK_ID)
    assert any("shared-deliverables" in p for p in prefixes)
    assert any(p == f"/deliverables/{TASK_ID}/" for p in prefixes)
    assert any(p == f"/shared-deliverables/{TASK_ID}/" for p in prefixes)
    assert any(p == f"/shared-mcp/{TASK_ID}/" for p in prefixes)
