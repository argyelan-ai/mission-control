"""Regression guard: no code may add heartbeat_md back.

After migration 0125 removed `agents.heartbeat_md` and deleted
`backend/templates/HEARTBEAT.md.j2`, no production code should reference
either. Stale comments are tolerated; live code (assignments, imports,
field declarations, template loads) is not.

Pattern follows test_no_gateway_imports.py (Phase 29 layout-robust check).

Plan: docs/superpowers/plans/2026-05-23-dispatch-message-refactor.md
Phase: 4 / Task 4.8
"""
from __future__ import annotations

import re
from pathlib import Path


BACKEND_APP = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


# Allow: lines that are pure comments mentioning the breadcrumb
_COMMENT_BREADCRUMB = re.compile(
    r"^\s*#.*heartbeat_md.*(removed|never read|migration 0125)",
    re.IGNORECASE,
)


def _is_breadcrumb_comment(line: str) -> bool:
    return bool(_COMMENT_BREADCRUMB.search(line))


def test_no_heartbeat_md_in_backend_app():
    """Backend app code must not reference heartbeat_md as live code.

    Allowed exceptions:
    - Comment lines explaining the removal (breadcrumbs).
    - Migration file (Alembic versions/) — that's where the drop lives.
    """
    hits = []
    for path in BACKEND_APP.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), 1):
            if "heartbeat_md" not in line:
                continue
            if _is_breadcrumb_comment(line):
                continue
            hits.append(f"{path.relative_to(BACKEND_APP.parent)}:{line_no}: {line.strip()}")
    assert not hits, (
        "Found live references to 'heartbeat_md' that should be removed:\n"
        + "\n".join(hits)
    )


def test_no_heartbeat_md_template_file():
    """HEARTBEAT.md.j2 must not exist in templates/."""
    assert not (TEMPLATES / "HEARTBEAT.md.j2").exists(), (
        f"HEARTBEAT.md.j2 should have been deleted in Phase 4 / Task 4.5. "
        f"Found at {TEMPLATES / 'HEARTBEAT.md.j2'}"
    )


def test_no_heartbeat_md_in_template_renderer():
    """template_renderer.render_all_agent_files() must not list HEARTBEAT.md.j2."""
    renderer = BACKEND_APP / "services" / "template_renderer.py"
    text = renderer.read_text()
    # Only allow string mentions inside docstrings or comments
    for line_no, line in enumerate(text.splitlines(), 1):
        if "HEARTBEAT.md" in line:
            stripped = line.strip()
            is_comment = stripped.startswith("#")
            is_docstring_content = "Returns:" in stripped or "removed" in stripped.lower() or "never read" in stripped.lower()
            assert is_comment or is_docstring_content, (
                f"template_renderer.py:{line_no} has live HEARTBEAT.md reference: {stripped}"
            )
