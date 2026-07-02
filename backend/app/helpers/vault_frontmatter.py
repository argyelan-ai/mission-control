"""Frontmatter parse + validate. Single responsibility: read/validate
YAML+Markdown files. No side effects, no I/O beyond the passed path."""

from datetime import datetime
from pathlib import Path
from typing import Any

import frontmatter
import yaml


class FrontmatterError(Exception):
    pass


VALID_TYPES = {
    "lesson", "knowledge", "reference",
    "journal", "weekly_review", "note",
    # Auto-generated wrapper notes for TaskDeliverables (Phase A vault-as-brain).
    # Carry attachment_path pointing into ~/.mc/vault/attachments/.
    "deliverable",
}

VALID_STATUS = {"draft", "published", "stale", "archived"}
VALID_CONFIDENCE = {"high", "medium", "low"}

REQUIRED_FIELDS = ("id", "type", "agent", "date")


def parse_frontmatter(path: Path) -> frontmatter.Post:
    try:
        return frontmatter.load(str(path))
    except yaml.YAMLError as e:
        raise FrontmatterError(f"YAML parse error in {path}: {e}") from e
    except Exception as e:
        raise FrontmatterError(f"Cannot read {path}: {e}") from e


def validate_frontmatter(metadata: dict[str, Any]) -> None:
    for field in REQUIRED_FIELDS:
        if field not in metadata:
            raise FrontmatterError(f"missing required field: {field}")

    if metadata["type"] not in VALID_TYPES:
        raise FrontmatterError(
            f"invalid type: {metadata['type']!r}. Must be one of {sorted(VALID_TYPES)}"
        )

    date_val = metadata["date"]
    if isinstance(date_val, str):
        try:
            datetime.fromisoformat(date_val.replace("Z", "+00:00"))
        except ValueError as e:
            raise FrontmatterError(f"invalid date {date_val!r}: must be ISO-8601") from e
    elif not isinstance(date_val, datetime):
        raise FrontmatterError(f"invalid date {date_val!r}: must be ISO-8601 string or datetime")

    # Phase E (Task-Klammer): optional `task` field carries the originating
    # task UUID so the operator + agents can pull every note + deliverable that share
    # the same source-task ("show me everything from the Wetterbericht run").
    # Not in REQUIRED_FIELDS — old notes without it stay valid.
    task_val = metadata.get("task")
    if task_val is not None:
        import uuid as _uuid
        try:
            _uuid.UUID(str(task_val))
        except ValueError as e:
            raise FrontmatterError(f"invalid task {task_val!r}: must be a UUID string") from e

    # Phase 2: optional status field
    status_val = metadata.get("status")
    if status_val is not None and status_val not in VALID_STATUS:
        raise FrontmatterError(
            f"invalid status: {status_val!r}. Must be one of {sorted(VALID_STATUS)}"
        )

    # Phase 2: optional confidence field
    confidence_val = metadata.get("confidence")
    if confidence_val is not None and confidence_val not in VALID_CONFIDENCE:
        raise FrontmatterError(
            f"invalid confidence: {confidence_val!r}. Must be one of {sorted(VALID_CONFIDENCE)}"
        )

    # Phase 2: optional updated field (ISO-8601 or datetime)
    updated_val = metadata.get("updated")
    if updated_val is not None:
        if isinstance(updated_val, str):
            try:
                datetime.fromisoformat(updated_val.replace("Z", "+00:00"))
            except ValueError as e:
                raise FrontmatterError(
                    f"invalid updated {updated_val!r}: must be ISO-8601"
                ) from e
        elif not isinstance(updated_val, datetime):
            raise FrontmatterError(
                f"invalid updated {updated_val!r}: must be ISO-8601 string or datetime"
            )
