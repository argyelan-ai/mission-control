"""TaskComment.comment_type — Single Source of Truth (REL-01).

Imported by:
  - backend/app/routers/agents.py (DELIVERABLE_SYSTEM_TYPES → _DELIVER_SYSTEM_COMMENT_TYPES)
  - backend/app/routers/agent_scoped.py (ALL_COMMENT_TYPES → VALID_COMMENT_TYPES)

No other place is allowed to define its own comment-type sets.

Live-Bug-Regression: 2026-04-23 (Tester-blocked) und 2026-04-24
(install_completed silent-drop, PR #110). Fix-PR: REL-01 dieser Phase.
"""
from __future__ import annotations
from typing import Final

# All comment_type values that the API accepts via AgentCommentCreate.
# Add new types HERE FIRST. Keep alphabetical groups intact for grep-ability.
ALL_COMMENT_TYPES: Final[frozenset[str]] = frozenset({
    "message", "handoff", "blocker", "progress", "resolution",
    "feedback", "checkpoint", "report_back", "reflection",
    "waiting_on_callback",
    # Phase Approval Workflow
    "subtask_completed", "phase_approved", "phase_rewrite_request",
    # Install-Approval Callback
    "install_completed", "install_failed",
})

# Comment_types that get delivered as actionable system events to the
# responsible agent via /me/poll. Subset of ALL_COMMENT_TYPES, minus the
# historical server-only `system` type.
#
# `handoff` (Bug 9, 2026-05-13): Board Lead → worker briefing on an
# already-assigned task. Default comments (`message`) are deliberately NOT
# delivered (echo-loop protection / audit routine) — anyone wanting to
# nudge a worker uses either `mc delegate` (own sub-task) or
# `mc comment --type handoff` (wake signal on an existing task). See bug memo
# project_open_bugs_mc_agent_observability.md.
DELIVERABLE_SYSTEM_TYPES: Final[frozenset[str]] = frozenset({
    "subtask_completed", "resolution", "blocker", "system", "feedback",
    "handoff",
    "install_completed", "install_failed",
})

# Sanity-check at import time (defense in depth — D-02). No other
# code path is allowed to extend DELIVERABLE_SYSTEM_TYPES into a superset
# without extending ALL_COMMENT_TYPES at the same time.
_drift = DELIVERABLE_SYSTEM_TYPES - ALL_COMMENT_TYPES - {"system"}
assert not _drift, (
    f"comment_types.py drift: {_drift} are in DELIVERABLE_SYSTEM_TYPES "
    "but not in ALL_COMMENT_TYPES. Add to ALL or remove from DELIVERABLE."
)


# Content-Envelope detection (Bug 2026-05-17, Researcher).
# Agents sometimes wrap their content in `{"content": "..."}` JSON because they
# mistakenly think the CLI/API requires an envelope. The mc CLI now refuses this
# client-side (commit 9eae2594), but defense-in-depth at the API layer is
# important: internal scripts, manual curls, or future agents that bypass the
# CLI must not be able to write malformed JSON into task_comments.content.
#
# Narrow detection: only reject a string that parses to a dict whose key-set
# is EXACTLY {"content"} or {"content", "comment_type"}. Legitimate user
# comments starting with `{` (e.g. "{foo} is a placeholder") parse to invalid
# JSON or to dicts with different keys and pass through unchanged.
_ENVELOPE_KEYSETS: Final[tuple[frozenset[str], ...]] = (
    frozenset({"content"}),
    frozenset({"content", "comment_type"}),
)


def validate_comment_content(value: str) -> str:
    """Pydantic field-validator helper for TaskComment.content.

    Rules:
      1. Reject empty / whitespace-only content.
      2. If content parses as JSON to a dict matching a known envelope shape,
         reject with a hint pointing back at plain-text usage.
      3. Everything else passes through unchanged (including dicts that
         happen to share one key with the envelope but have others — they're
         not envelopes, they're legitimate technical comments).

    Returns the original string on success; raises ValueError on rejection.
    """
    if not isinstance(value, str):
        raise ValueError("comment content must be a string")
    stripped = value.strip()
    if not stripped:
        raise ValueError("comment content must not be empty")

    # Fast path: anything that doesn't even look like a JSON object can't be
    # an envelope. Avoids `json.loads` on every comment write.
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return value

    import json as _json
    try:
        parsed = _json.loads(stripped)
    except (ValueError, _json.JSONDecodeError):
        return value  # malformed JSON-ish but not an envelope — let it through

    if isinstance(parsed, dict):
        keys = frozenset(parsed.keys())
        if keys in _ENVELOPE_KEYSETS:
            raise ValueError(
                "comment content looks like a JSON envelope "
                '({"content": "..."}). Submit the content directly as plain '
                "text — the API does not expect a wrapper. See mc CLI hint "
                "for examples."
            )
    return value
