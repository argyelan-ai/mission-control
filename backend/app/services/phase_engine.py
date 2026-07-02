"""Phase Engine — Aktivierungslogik für ProjectPhases.

Pure functions ohne DB-Abhängigkeit — einfach testbar.
"""
from app.models.project_phase import ProjectPhase


def can_activate_phase(phase: ProjectPhase, completed_phase_ids: set[str]) -> bool:
    """True wenn alle Dependencies der Phase abgeschlossen sind.

    Args:
        phase: Die zu prüfende Phase
        completed_phase_ids: Set von UUIDs (als str) aller abgeschlossenen Phasen
    """
    if not phase.depends_on_phases:
        return True
    return all(dep in completed_phase_ids for dep in phase.depends_on_phases)
