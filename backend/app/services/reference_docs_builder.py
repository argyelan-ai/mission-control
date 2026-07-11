"""Renders Layer-2 reference docs — on-demand topic docs (`mc docs <topic>`)
that agents read only when they need them, instead of every turn.

Context-economy Stage 1: SOUL.md still carries the full content today (this
module is purely additive, nothing removed from SOUL yet). The Markdown here
is a curated copy of the matching SOUL.md.j2 sections — deliberately
excludes ACK-first / GitHub-leak / credentials / FORBIDDEN safety content,
which stays exclusively in SOUL/the Operating Card.

Templates live in backend/templates/docs/<topic>.md.j2, one per
agent_doc_constants.DOC_TOPICS entry, rendered via the same
render_agent_file (StrictUndefined) used for SOUL/USER/MEMORY.
"""
from __future__ import annotations

from app.agent_doc_constants import CANONICAL_VERBS, DOC_TOPICS
from app.constants import REFLECTION_MIN_CHARS, REFLECTION_REQUIRED_FIELDS
from app.services.template_renderer import render_agent_file


def _doc_render_context(context: dict) -> dict:
    """Builds the (small, topic-doc-specific) Jinja2 context.

    Deliberately does NOT require the full build_agent_context() shape —
    only the handful of variables the docs/*.md.j2 templates actually use.
    Falls back to the canonical constants when the caller's context doesn't
    carry them (e.g. a minimal dict in a test), so generate_reference_docs
    works standalone.
    """
    return {
        "operator_name": context.get("operator_name") or "the operator",
        "reflection_required_fields": context.get("reflection_required_fields")
        or list(REFLECTION_REQUIRED_FIELDS),
        "reflection_min_chars": context.get("reflection_min_chars")
        or REFLECTION_MIN_CHARS,
        "canonical_verbs": CANONICAL_VERBS,
    }


def generate_reference_docs(context: dict) -> dict[str, str]:
    """Renders every DOC_TOPICS entry. Returns {topic: markdown}.

    Renders ALL topics unconditionally — audience filtering (e.g.
    "delegation" only for lead/orchestrator) is the caller's job
    (docker_agent_sync.write_reference_docs), so this function stays a pure
    topic -> content mapper independent of any one agent's role.
    """
    render_context = _doc_render_context(context)
    return {
        topic: render_agent_file(f"docs/{topic}.md.j2", render_context)
        for topic in DOC_TOPICS
    }


def generate_docs_index(topics: dict[str, str]) -> str:
    """Builds docs/INDEX.md — a table of the topics actually written for
    this agent (post audience-filter), with a one-line "when to read" hint
    so the agent can decide whether to open a doc without reading it first.
    """
    lines = [
        "# Reference Docs Index",
        "",
        "On-demand topic docs — read only the ones relevant to your current",
        "task. `mc docs <topic>` prints one to stdout.",
        "",
        "| Topic | Read when |",
        "|---|---|",
    ]
    for topic in sorted(topics):
        spec = DOC_TOPICS.get(topic)
        title = spec.title if spec else topic
        when = spec.when_to_read if spec else ""
        lines.append(f"| `{topic}` — {title} | {when} |")
    lines.append("")
    return "\n".join(lines)
