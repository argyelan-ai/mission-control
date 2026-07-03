"""Phase Engine — activation logic for ProjectPhases.

Pure functions with no DB dependency — easy to test.
"""
from app.models.project_phase import ProjectPhase


def can_activate_phase(phase: ProjectPhase, completed_phase_ids: set[str]) -> bool:
    """True if all dependencies of the phase are completed.

    Args:
        phase: The phase to check
        completed_phase_ids: Set of UUIDs (as str) of all completed phases
    """
    if not phase.depends_on_phases:
        return True
    return all(dep in completed_phase_ids for dep in phase.depends_on_phases)
