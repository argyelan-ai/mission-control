"""Skill-lab candidate endpoints (frozen feature F-skill-lab).

Moved from the removed playbooks test module in E5: the router stays, only
the playbook layer that wrote candidates is gone.
"""
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


async def _mk_candidate():
    from app.models.skill_lab import SkillCandidate

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        candidate = SkillCandidate(
            title="Research synthesis skill",
            summary="Suggested from repeated discovery runs.",
            candidate_type="new_skill",
            proposed_by="system",
            source_run_ids=["run-1", "run-2"],
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        return candidate.id


@pytest.mark.asyncio
async def test_skill_lab_is_frozen_patch_refused_and_row_unchanged(auth_client: AsyncClient):
    """Etappe 2 (ADR-102 §3, list row 10): Skill-Lab is dead (0 candidates,
    0 runs), its tables are frozen. Reading stays, writing answers 410."""
    candidate_id = await _mk_candidate()

    r = await auth_client.patch(
        f"/api/v1/skill-lab/candidates/{candidate_id}", json={"status": "approved"},
    )
    assert r.status_code == 410, r.text
    assert r.json()["detail"]["code"] == "frozen"

    listed = await auth_client.get("/api/v1/skill-lab/candidates")
    assert listed.status_code == 200
    [row] = listed.json()
    assert row["status"] == "open"
    assert row["reviewed_at"] is None


@pytest.mark.asyncio
async def test_list_and_update_skill_candidate(auth_client: AsyncClient, monkeypatch):
    # Frozen (Etappe 2); the update logic stays for the way back.
    from app.routers import skill_lab as _skill_lab_router
    monkeypatch.setattr(_skill_lab_router, "FROZEN", False)
    from app.models.skill_lab import SkillCandidate

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        candidate = SkillCandidate(
            title="Research synthesis skill",
            summary="Suggested from repeated discovery runs.",
            candidate_type="new_skill",
            proposed_by="system",
            source_run_ids=["run-1", "run-2"],
        )
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        candidate_id = candidate.id

    list_resp = await auth_client.get("/api/v1/skill-lab/candidates")
    assert list_resp.status_code == 200, list_resp.text
    assert len(list_resp.json()) == 1

    update_resp = await auth_client.patch(
        f"/api/v1/skill-lab/candidates/{candidate_id}",
        json={"status": "approved", "target_skill_key": "research-synthesis"},
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()
    assert body["status"] == "approved"
    assert body["target_skill_key"] == "research-synthesis"
    assert body["reviewed_at"] is not None
