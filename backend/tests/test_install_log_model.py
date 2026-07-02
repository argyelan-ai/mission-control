import uuid
from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.install_log import InstallLog


@pytest.mark.asyncio
async def test_install_log_insert_and_query(async_session: AsyncSession):
    target = uuid.uuid4()
    entry = InstallLog(
        target_agent_id=target,
        action_type="install_skill",
        resource_name="web-performance",
        source="github:anthropic/skill-web-performance",
        result="success",
        installed_version="1.0.0",
        previous_state={"cli_skills": None},
    )
    async_session.add(entry)
    await async_session.commit()

    rows = (await async_session.exec(select(InstallLog))).all()
    assert len(rows) == 1
    assert rows[0].resource_name == "web-performance"
    assert rows[0].previous_state == {"cli_skills": None}
    assert isinstance(rows[0].created_at, datetime)
