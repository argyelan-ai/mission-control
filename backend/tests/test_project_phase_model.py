"""Tests: ProjectPhase and DeliverableReference models."""
import uuid
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.project_phase import ProjectPhase
from app.models.deliverable_reference import DeliverableReference


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)


def test_project_phase_defaults(engine):
    phase = ProjectPhase(project_id=uuid.uuid4(), title="Research")
    with Session(engine) as session:
        session.add(phase)
        session.commit()
        session.refresh(phase)
    assert phase.status == "pending"
    assert phase.failure_policy == "retry"
    assert phase.gate_required is False
    assert phase.order == 0


def test_project_phase_depends_on_phases(engine):
    dep1, dep2 = uuid.uuid4(), uuid.uuid4()
    phase = ProjectPhase(
        project_id=uuid.uuid4(),
        title="Dev",
        depends_on_phases=[str(dep1), str(dep2)],
    )
    with Session(engine) as session:
        session.add(phase)
        session.commit()
        result = session.exec(select(ProjectPhase).where(ProjectPhase.title == "Dev")).one()
    assert len(result.depends_on_phases) == 2


def test_deliverable_reference_creates(engine):
    ref = DeliverableReference(
        source_deliverable_id=uuid.uuid4(),
        target_project_id=uuid.uuid4(),
    )
    with Session(engine) as session:
        session.add(ref)
        session.commit()
        session.refresh(ref)
    assert ref.id is not None
