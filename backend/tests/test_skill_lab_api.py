"""Skill-lab candidate endpoints (frozen feature F-skill-lab).

Moved from the removed playbooks test module in E5: the router stays, only
the playbook layer that wrote candidates is gone.
"""
import pytest
from httpx import AsyncClient
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine


@pytest.mark.asyncio
async def test_list_and_update_skill_candidate(auth_client: AsyncClient):
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
