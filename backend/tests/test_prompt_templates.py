"""Prompt Library (Benchmark Studio Baustein 3, core): model + CRUD API tests.

Fixture pattern mirrors tests/test_reference_files.py: `session` / `auth_client`
from conftest (SQLite in-memory + JWT admin user).
"""

import uuid

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.prompt_template import PromptTemplate


# ── Model ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_template_model_defaults(session: AsyncSession):
    tpl = PromptTemplate(
        title="Spinning cube",
        body="Build a spinning 3D cube in a single self-contained index.html.",
    )
    session.add(tpl)
    await session.commit()
    await session.refresh(tpl)

    assert isinstance(tpl.id, uuid.UUID)
    assert tpl.tags == []          # JSON default list, never None
    assert tpl.created_at is not None
    assert tpl.updated_at is not None
