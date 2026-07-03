"""Tests: target_url is shown in dispatch messages."""
import uuid
import pytest
from app.models.task import Task
from app.models.agent import Agent


def _make_task(**kwargs) -> Task:
    defaults = dict(
        id=uuid.uuid4(),
        board_id=uuid.uuid4(),
        title="Test Task",
        status="inbox",
        priority="medium",
        task_type="story",
        dispatch_intent="subtask",
        sort_order=0,
        is_auto_created=False,
        report_back_required=False,
        requires_auth=False,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _make_agent(**kwargs) -> Agent:
    defaults = dict(
        id=uuid.uuid4(),
        name="TestAgent",
        status="online",
        is_board_lead=False,
        tools_md="Bearer test-token-123",
        provision_status="provisioned",
    )
    defaults.update(kwargs)
    return Agent(**defaults)


class TestTargetUrlInDispatchMessage:

    def test_target_url_in_worker_dispatch(self):
        from app.services.dispatch import _format_dispatch_message, DispatchContext
        task = _make_task(target_url="http://localhost:4200/dashboard")
        agent = _make_agent()
        ctx = DispatchContext()
        msg = _format_dispatch_message(task, agent, ctx)
        assert "http://localhost:4200/dashboard" in msg
        assert "Target URL" in msg

    def test_no_target_url_no_section(self):
        from app.services.dispatch import _format_dispatch_message, DispatchContext
        task = _make_task(target_url=None)
        agent = _make_agent()
        ctx = DispatchContext()
        msg = _format_dispatch_message(task, agent, ctx)
        assert "Target URL" not in msg

    def test_target_url_with_workspace_port(self):
        from app.services.dispatch import _format_dispatch_message, DispatchContext
        task = _make_task(target_url="http://localhost:4200/dashboard", workspace_port=4200)
        agent = _make_agent()
        ctx = DispatchContext()
        msg = _format_dispatch_message(task, agent, ctx)
        assert "http://localhost:4200/dashboard" in msg
        assert "4200" in msg


class TestTargetUrlInReviewMessage:

    @pytest.mark.anyio
    async def test_target_url_in_review_message(self, session):
        from app.services.dispatch import _build_review_message
        task = _make_task(target_url="http://localhost:4200/dashboard")
        agent = _make_agent(name="Rex")
        msg = await _build_review_message(task, agent, session)
        assert "http://localhost:4200/dashboard" in msg

    @pytest.mark.anyio
    async def test_no_target_url_in_review(self, session):
        from app.services.dispatch import _build_review_message
        task = _make_task(target_url=None)
        agent = _make_agent(name="Rex")
        msg = await _build_review_message(task, agent, session)
        assert "Target URL" not in msg


class TestTargetUrlInTestMessage:

    @pytest.mark.anyio
    async def test_tester_uses_target_url(self, session):
        """Tester message uses target_url instead of hardcoded localhost."""
        from app.services.dispatch import _build_test_message
        task = _make_task(target_url="http://localhost:4200/dashboard")
        agent = _make_agent(name="Tester")
        msg = await _build_test_message(task, agent, session)
        assert "http://localhost:4200/dashboard" in msg
        assert 'open "http://localhost"' not in msg

    @pytest.mark.anyio
    async def test_tester_uses_workspace_port_fallback(self, session):
        """Without target_url but with workspace_port: http://localhost:{port}."""
        from app.services.dispatch import _build_test_message
        task = _make_task(target_url=None, workspace_port=4200)
        agent = _make_agent(name="Tester")
        msg = await _build_test_message(task, agent, session)
        assert "http://localhost:4200" in msg

    @pytest.mark.anyio
    async def test_tester_fallback_localhost(self, session):
        """Without target_url and without workspace_port: http://localhost."""
        from app.services.dispatch import _build_test_message
        task = _make_task(target_url=None, workspace_port=None)
        agent = _make_agent(name="Tester")
        msg = await _build_test_message(task, agent, session)
        assert "http://localhost" in msg
