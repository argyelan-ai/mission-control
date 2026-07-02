"""Phase 7 — OBS-01 Vault Render: Plan 07-02 lands the bodies.

Wave-0 stubs (Plan 07-00) flipped here in Plan 07-02. Tests exercise:
- ``_render_frontmatter`` deterministic key order + YAML semantics
- Vault layout creation on ``ObsidianExportService.start()``
- ``None`` agent_id / project_slug emit YAML ``null`` (not empty string,
  not key omitted) so Obsidian dataview parses cleanly.

Pattern derived from backend/tests/test_embedding_retry_queue.py and
backend/tests/test_memory_attachments.py (Phase 5 Wave-0).
"""
from datetime import datetime
import os
import uuid

import pytest


def _make_entry(**overrides):
    """Build a BoardMemory test instance — defaults match a pinned global
    knowledge entry.
    """
    from app.models.memory import BoardMemory

    defaults = dict(
        id=uuid.uuid4(),
        board_id=None,
        agent_id=None,
        title="Test Title",
        content="hello world",
        memory_type="knowledge",
        tags=["alpha", "beta"],
        source="user",
        is_pinned=False,
        auto_generated=False,
        updated_at=datetime(2026, 4, 27, 12, 0, 0),
    )
    defaults.update(overrides)
    return BoardMemory(**defaults)


@pytest.mark.asyncio
async def test_frontmatter_keys_and_order():
    """OBS-01: rendered frontmatter MUST contain title, type, tags, date,
    agent, project, status keys in the documented schema order.
    """
    from app.services.obsidian_export import _render_frontmatter

    entry = _make_entry(
        title="Hello",
        memory_type="knowledge",
        tags=["a", "b"],
    )
    fm = _render_frontmatter(entry, agent_slug="cody", project_slug="auth")

    assert fm.startswith("---\n"), "frontmatter must start with ---\\n"
    assert fm.endswith("---\n"), "frontmatter must end with ---\\n"

    # Extract top-level YAML keys in order (skip list entries `- ...`).
    keys: list[str] = []
    for line in fm.split("\n"):
        if not line or line.startswith("---") or line.startswith(" ") or line.startswith("-"):
            continue
        if ":" in line:
            keys.append(line.split(":", 1)[0].strip())

    expected = ["title", "type", "tags", "date", "agent", "project", "status"]
    assert keys == expected, f"key order broken: got {keys}, expected {expected}"


@pytest.mark.asyncio
async def test_vault_layout_created(tmp_path, monkeypatch):
    """OBS-01: ObsidianExportService MUST create vault/memory/{agents,projects,
    global}/ + vault/attachments/{tasks,deliverables}/ on first run.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.services.obsidian_export import ObsidianExportService, _vault_root

    svc = ObsidianExportService(interval=99999)  # never auto-fire
    await svc.start()
    try:
        root = _vault_root()
        assert root == f"{tmp_path}/.mc/vault"
        for sub in (
            "memory/agents",
            "memory/projects",
            "memory/global",
            "attachments/tasks",
            "attachments/deliverables",
        ):
            assert os.path.isdir(os.path.join(root, sub)), f"missing vault subdir: {sub}"
    finally:
        await svc.stop()


@pytest.mark.asyncio
async def test_null_agent_and_project_emit_yaml_null():
    """OBS-01: rows with null agent_id/project_id MUST emit YAML `null`
    (not empty string, not key omitted) so Obsidian dataview parses cleanly.
    """
    from app.services.obsidian_export import _render_frontmatter

    entry = _make_entry(agent_id=None, board_id=None)
    fm = _render_frontmatter(entry, agent_slug=None, project_slug=None)

    assert "agent: null" in fm, f"None agent did not become YAML null:\n{fm}"
    assert "project: null" in fm, f"None project did not become YAML null:\n{fm}"


@pytest.mark.asyncio
async def test_tags_coerce_drop_none_and_stringify():
    """OBS-01 (Pitfall 7): tags list with None / non-string entries MUST be
    coerced to ``[str(t) for t in tags if t is not None]``. None entries dropped.
    """
    import yaml

    from app.services.obsidian_export import _render_frontmatter

    entry = _make_entry(tags=["alpha", None, 42, "beta"])
    fm = _render_frontmatter(entry, agent_slug=None, project_slug=None)
    parsed = yaml.safe_load(fm.replace("---\n", "", 1).rsplit("---\n", 1)[0])

    assert parsed["tags"] == ["alpha", "42", "beta"], (
        f"tag coercion broken (Pitfall 7): got {parsed['tags']}"
    )


@pytest.mark.asyncio
async def test_yaml_safe_dump_handles_special_chars():
    """OBS-01: yaml.safe_dump must escape/quote markers so titles with
    special YAML chars round-trip cleanly (YAML injection guard).
    """
    import yaml

    from app.services.obsidian_export import _render_frontmatter

    entry = _make_entry(title="Title with --- markers and: colons")
    fm = _render_frontmatter(entry, agent_slug="cody", project_slug=None)
    parsed = yaml.safe_load(fm.replace("---\n", "", 1).rsplit("---\n", 1)[0])

    assert parsed["title"] == "Title with --- markers and: colons"
