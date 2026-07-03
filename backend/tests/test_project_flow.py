"""Tests for Project Flow — phase completion and task injection."""
import uuid
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.project_phase import ProjectPhase
from app.models.task import Task


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


def test_task_has_phase_id_field():
    """Task model has phase_id field."""
    fields = Task.__fields__
    assert "phase_id" in fields
    assert "triggered_by_deliverable_id" in fields


def test_phase_dependency_resolution():
    """Phase completion logic: activate phases once all dependencies are met."""
    from app.services.phase_engine import can_activate_phase

    phase_a_id = str(uuid.uuid4())
    phase_b_id = str(uuid.uuid4())

    # Phase B depends on Phase A
    phase_b = ProjectPhase(
        id=uuid.UUID(phase_b_id),
        project_id=uuid.uuid4(),
        title="Phase B",
        depends_on_phases=[phase_a_id],
    )

    completed_phase_ids = {phase_a_id}
    assert can_activate_phase(phase_b, completed_phase_ids) is True

    # Without A: not activatable
    assert can_activate_phase(phase_b, set()) is False


def test_phase_no_dependencies_always_activatable():
    """Phase with no dependencies can always be activated."""
    from app.services.phase_engine import can_activate_phase

    phase = ProjectPhase(
        project_id=uuid.uuid4(),
        title="Phase A",
        depends_on_phases=None,
    )
    assert can_activate_phase(phase, set()) is True


def test_can_activate_with_multiple_deps_all_done():
    """All dependencies met → activatable."""
    from app.services.phase_engine import can_activate_phase
    dep1, dep2 = str(uuid.uuid4()), str(uuid.uuid4())
    phase = ProjectPhase(
        project_id=uuid.uuid4(),
        title="Phase C",
        depends_on_phases=[dep1, dep2],
    )
    assert can_activate_phase(phase, {dep1, dep2}) is True
    assert can_activate_phase(phase, {dep1}) is False  # dep2 missing


def test_can_activate_with_multiple_deps_partial():
    """Only one of two dependencies met → not activatable."""
    from app.services.phase_engine import can_activate_phase
    dep1, dep2 = str(uuid.uuid4()), str(uuid.uuid4())
    phase = ProjectPhase(
        project_id=uuid.uuid4(),
        title="Phase D",
        depends_on_phases=[dep1, dep2],
    )
    assert can_activate_phase(phase, {dep1}) is False
    assert can_activate_phase(phase, set()) is False
