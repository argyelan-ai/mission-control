"""Delegation Contracts — structured required fields per task type.

Each delegation_type defines which fields must be set.
Validation is the primary source of truth (not free-text heuristics).

Contract types:
- code_change: branch + acceptance criteria
- visual_proof: URL + acceptance criteria + optional auth
- credential_bound: credentials + URL + acceptance criteria
- review: source_task_id (structural reference to the reviewed task)
"""

import logging
import re

logger = logging.getLogger("mc.delegation_contracts")

VALID_DELEGATION_TYPES = {"code_change", "visual_proof", "credential_bound", "review", "planning"}

# Contract definitions: required = hard block (422), recommended = warning
DELEGATION_CONTRACTS: dict[str, dict] = {
    "code_change": {
        "required": ["branch_name", "acceptance_criteria"],
        "conditional": [("requires_auth", "credentials")],
        "recommended": [],
    },
    "visual_proof": {
        "required": ["target_url", "acceptance_criteria", "expected_content"],
        "conditional": [("requires_auth", "credentials")],
        # branch_name intentionally NOT recommended: visual_proof is a
        # verification contract (screenshots/UI checks), often run against
        # already-deployed URLs without an associated code branch. The
        # recommendation produced noisy `delegation.warning` events on
        # every visual_proof task. (2026-05-18)
        "recommended": [],
    },
    "credential_bound": {
        "required": ["credentials", "target_url", "acceptance_criteria"],
        "conditional": [],
        "recommended": ["branch_name"],
    },
    "review": {
        "required": ["source_task_id"],
        "conditional": [],
        "recommended": ["branch_name"],
    },
    "planning": {
        "required": [],
        "conditional": [],
        "recommended": [],
    },
}

# Fields that need human-readable names for error messages
FIELD_LABELS = {
    "branch_name": "Branch-Name (z.B. feature/xyz)",
    "acceptance_criteria": "Akzeptanzkriterien",
    "target_url": "Ziel-URL (z.B. http://localhost/tasks)",
    "credentials": "Credentials (im credentials-Feld, nicht in der Beschreibung)",
    "source_task_id": "Quell-Task-ID (Referenz zum reviewten Task)",
    "expected_content": "Erwarteter sichtbarer Seiteninhalt (z.B. 'Dashboard mit Sidebar' oder 'Task-Liste')",
}


def validate_delegation_contract(
    delegation_type: str | None,
    fields: dict,
) -> tuple[list[str], list[str]]:
    """Checks contract fields based on delegation_type.

    Args:
        delegation_type: The contract type (code_change, visual_proof, etc.)
        fields: dict with the task fields (branch_name, target_url, etc.)

    Returns:
        (hard_errors, warnings):
        - hard_errors: task is NOT created (422)
        - warnings: task is created, but an activity event is emitted
    """
    hard_errors: list[str] = []
    warnings: list[str] = []

    # No delegation_type → no contract check (legacy/fallback)
    if not delegation_type:
        # Fallback heuristic: only warnings if description contains login keywords
        description = (fields.get("description") or "").lower()
        if re.search(r"login|anmelden|eingeloggt|passwort", description) and not fields.get("credentials"):
            warnings.append("missing_credentials_hint: Description enthaelt Login-Keywords aber kein credentials-Feld gesetzt")
        return hard_errors, warnings

    # Unknown delegation_type
    if delegation_type not in VALID_DELEGATION_TYPES:
        hard_errors.append(f"Unbekannter delegation_type: '{delegation_type}'. Erlaubt: {', '.join(sorted(VALID_DELEGATION_TYPES))}")
        return hard_errors, warnings

    contract = DELEGATION_CONTRACTS[delegation_type]

    # Check required fields → hard block
    for field in contract.get("required", []):
        value = fields.get(field)
        if not value:
            label = FIELD_LABELS.get(field, field)
            hard_errors.append(f"missing_{field}: {delegation_type} braucht {label}")

    # Check conditional fields → hard block if condition is met
    for condition_field, required_field in contract.get("conditional", []):
        if fields.get(condition_field) and not fields.get(required_field):
            label = FIELD_LABELS.get(required_field, required_field)
            hard_errors.append(
                f"missing_{required_field}: {condition_field} ist gesetzt aber {label} fehlt"
            )

    # Check recommended fields → warning
    for field in contract.get("recommended", []):
        value = fields.get(field)
        if not value:
            label = FIELD_LABELS.get(field, field)
            warnings.append(f"missing_{field}: {delegation_type} empfiehlt {label}")

    return hard_errors, warnings
