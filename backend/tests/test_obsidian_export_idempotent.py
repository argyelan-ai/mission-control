"""Phase 7 — OBS-02 Idempotency Invariant: Plan 07-02 lands the bodies.

Wave-0 stubs (Plan 07-00) flipped here in Plan 07-02. Tests exercise:
- ``_write_if_changed`` SHA-256 short-circuit (zero file diff on identical
  content; mtime preserved on skip).
- "MC always wins" rule: when content changes (BoardMemory.updated_at
  advances), next ``_write_if_changed`` overwrites the vault file.

Per RESEARCH.md A3 default: when MC's BoardMemory.updated_at and the
vault file mtime disagree, MC always overwrites — vault is a one-way sink.
"""
import asyncio
import os
import time

import pytest


@pytest.mark.asyncio
async def test_second_run_zero_diff(tmp_path, monkeypatch):
    """OBS-02: writing the SAME content twice via ``_write_if_changed`` MUST
    skip the second write (SHA-256 match) and leave mtime unchanged. This is
    the idempotency contract from ROADMAP Success Criterion 2.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.services.obsidian_export import _write_if_changed

    target = str(tmp_path / "out.md")
    content = "---\ntitle: A\n---\n\n# A\n\nbody\n"

    # First write: creates file, returns True.
    assert _write_if_changed(target, content) is True
    assert os.path.isfile(target)
    mtime1 = os.path.getmtime(target)

    # Sleep so the OS clock could advance — proves mtime preservation isn't
    # a coincidence.
    await asyncio.sleep(0.05)

    # Second write with identical content: returns False, no FS touch.
    assert _write_if_changed(target, content) is False
    mtime2 = os.path.getmtime(target)
    assert mtime1 == mtime2, (
        f"mtime changed on idempotent skip: {mtime1} vs {mtime2}"
    )


@pytest.mark.asyncio
async def test_newer_wins_overwrites(tmp_path, monkeypatch):
    """OBS-02: when content changes (e.g. BoardMemory.updated_at advanced
    upstream), ``_write_if_changed`` MUST overwrite the vault file. "MC
    always wins" per RESEARCH.md A3 default.
    """
    monkeypatch.setenv("HOME_HOST", str(tmp_path))

    from app.services.obsidian_export import _write_if_changed

    target = str(tmp_path / "out.md")
    v1 = "---\ntitle: A\n---\n\n# A\n\noriginal body\n"
    v2 = "---\ntitle: A\n---\n\n# A\n\nupdated body\n"

    assert _write_if_changed(target, v1) is True
    assert "original body" in open(target).read()

    # Advance content — write_if_changed must replace.
    assert _write_if_changed(target, v2) is True
    contents_after = open(target).read()
    assert "updated body" in contents_after
    assert "original body" not in contents_after
