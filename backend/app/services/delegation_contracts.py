"""Delegation Contracts — strukturierte Pflichtfelder pro Task-Typ.

Jeder delegation_type definiert welche Felder gesetzt sein muessen.
Die Validierung ist die primaere Source of Truth (nicht Freitext-Heuristiken).

Contract-Typen:
- code_change: Branch + Akzeptanzkriterien
- visual_proof: URL + Akzeptanzkriterien + optional Auth
- credential_bound: Credentials + URL + Akzeptanzkriterien
- review: source_task_id (strukturelle Referenz zum reviewten Task)
"""

import logging
import re

logger = logging.getLogger("mc.delegation_contracts")

VALID_DELEGATION_TYPES = {"code_change", "visual_proof", "credential_bound", "review", "planning"}

# Contract-Definitionen: required = harter Block (422), recommended = Warning
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

# Felder die lesbare Namen fuer Fehlermeldungen brauchen
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
    """Prueft Contract-Felder basierend auf delegation_type.

    Args:
        delegation_type: Der Contract-Typ (code_change, visual_proof, etc.)
        fields: Dict mit den Task-Feldern (branch_name, target_url, etc.)

    Returns:
        (hard_errors, warnings):
        - hard_errors: Task wird NICHT erstellt (422)
        - warnings: Task wird erstellt, aber Activity Event emittiert
    """
    hard_errors: list[str] = []
    warnings: list[str] = []

    # Kein delegation_type → kein Contract-Check (Legacy/Fallback)
    if not delegation_type:
        # Fallback-Heuristik: Nur Warnings wenn Description Login-Keywords enthaelt
        description = (fields.get("description") or "").lower()
        if re.search(r"login|anmelden|eingeloggt|passwort", description) and not fields.get("credentials"):
            warnings.append("missing_credentials_hint: Description enthaelt Login-Keywords aber kein credentials-Feld gesetzt")
        return hard_errors, warnings

    # Unbekannter delegation_type
    if delegation_type not in VALID_DELEGATION_TYPES:
        hard_errors.append(f"Unbekannter delegation_type: '{delegation_type}'. Erlaubt: {', '.join(sorted(VALID_DELEGATION_TYPES))}")
        return hard_errors, warnings

    contract = DELEGATION_CONTRACTS[delegation_type]

    # Required-Felder pruefen → harter Block
    for field in contract.get("required", []):
        value = fields.get(field)
        if not value:
            label = FIELD_LABELS.get(field, field)
            hard_errors.append(f"missing_{field}: {delegation_type} braucht {label}")

    # Conditional-Felder pruefen → harter Block wenn Bedingung erfuellt
    for condition_field, required_field in contract.get("conditional", []):
        if fields.get(condition_field) and not fields.get(required_field):
            label = FIELD_LABELS.get(required_field, required_field)
            hard_errors.append(
                f"missing_{required_field}: {condition_field} ist gesetzt aber {label} fehlt"
            )

    # Recommended-Felder pruefen → Warning
    for field in contract.get("recommended", []):
        value = fields.get(field)
        if not value:
            label = FIELD_LABELS.get(field, field)
            warnings.append(f"missing_{field}: {delegation_type} empfiehlt {label}")

    return hard_errors, warnings
