"""Canonical single-source contracts for agent-facing documentation.

Context-economy Stage 1 (see ADR — Layer-2 reference docs). Referenced by:
- backend/app/services/reference_docs_builder.py (renders the L2 topic docs)
- backend/app/services/docker_agent_sync.py (writes them into agent homes)
- backend/tests/test_agent_docs_contract.py (contract enforcement — CI gate)
- scripts/mc-cli/mc_cli/commands.py (`mc docs`, reads the same topic slugs)

Deliberately dependency-light (no DB, no FastAPI, no jinja) so it stays
cheap to import from tests and doesn't create a cycle with the template
renderer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.constants import (  # re-export — single source, do not duplicate
    REFLECTION_CHARTER,
    REFLECTION_MIN_CHARS,
    REFLECTION_REQUIRED_FIELDS,
)

__all__ = [
    "CANONICAL_VERBS",
    "FORBIDDEN_VERB_PATTERNS",
    "DOC_TOPICS",
    "DocTopicSpec",
    "REFLECTION_REQUIRED_FIELDS",
    "REFLECTION_MIN_CHARS",
    "REFLECTION_CHARTER",
]


# ── Canonical verb list ──────────────────────────────────────────────────
#
# Statically maintained mirror of scripts/mc-cli/mc_cli/commands.py:REGISTRY.
# Kept as a plain dict here (rather than importing mc_cli from the backend)
# so mc-cli stays a standalone stdlib-only tool with no backend dependency.
# test_agent_docs_contract.py::test_canonical_verbs_are_registered verifies
# every key here is still a real REGISTRY entry — CI catches drift in
# either direction (new verb undocumented, or documented verb removed).
CANONICAL_VERBS: dict[str, str] = {
    "ack": "Confirm dispatch (status -> in_progress). Always the first call on a new task.",
    "done": "Set status -> done directly. No reflection gate bypass — for the mandatory close, prefer `mc finish`.",
    "patch": "Set status explicitly: done|review|in_progress|blocked|failed.",
    "task-get": "Fetch the current task's status and details.",
    "vault-search": "Full-text search across the Vault (notes + deliverable wrappers + PDF text).",
    "vault-related": "All notes/wrappers/lessons that share a task_id.",
    "vault-write": "Write a Vault note via the inbox API (cross-agent shared paths).",
    "review": "Hand a task to review (status -> review).",
    "approve": "Approve a review (decision=approve).",
    "reject": "Request changes on a review (decision=request_changes, --feedback required).",
    "finish": "Post the mandatory reflection and set status atomically — the canonical close verb.",
    "blocked": "Block a task with a question/description for the operator.",
    "failed": "Mark a task as failed.",
    "comment": "Post a comment (progress/blocker/feedback/resolution/handoff/message).",
    "ask": "Ask a thread-native question — non-blocking by default, --blocking pauses on the answer.",
    "msg": "Post a plain message/status/decision on the task thread (no questions — use `mc ask`).",
    "checklist": "Manage the task checklist (add/done/skip/list).",
    "question": "Ask the operator a clarifying question.",
    "help": "Ask another agent for help.",
    "delegate": "Delegate a subtask to another agent with an atomic callback wait.",
    "deliverable": "Register a deliverable.",
    "deliverable-get": "Read a deliverable's full content (verification route).",
    "telegram": "Send a report to the operator's Telegram reports chat.",
    "verify": "Visual verification — screenshots + metrics via mc-playwright.",
    "pdf": "Render Markdown to PDF via the mc-playwright sidecar.",
    "memory": "Search memory (Qdrant + board memory).",
    "recover": "Fetch the current task prompt after a restart/crash.",
    "me": "Show own agent info (id, role, scopes, current task, skills/plugins).",
    "plugin-list": "List shared-cache plugins (board-lead-only).",
    "plugin-show": "Show a worker's plugin allowlist.",
    "plugin-assign": "Set a worker's plugin allowlist (replace).",
    "plugin-unassign": "Remove a plugin from a worker's allowlist.",
    "worker-restart": "Restart a cli-bridge worker's session.",
    "remember": "Save something to the Vault (shortcut for vault-write).",
    "file-answer": "Save a research result as a Vault note.",
    "docs": "Read a local reference doc — no network call, works offline.",
}


# ── Forbidden patterns ───────────────────────────────────────────────────
#
# Things agent-facing documentation must NEVER teach because they are
# broken, dead, or internal-only — the exact bugs a W1 audit found and
# fixed (git log --oneline --grep=coherence: 7a80858c, e97e2f37, 35004490).
# Each pattern is deliberately narrow: verified against the current,
# already-corrected SOUL.md.j2 / tools_md_builder.py / cli_terminal.py
# output before being added here, so it catches REGRESSIONS of those bugs
# without flagging legitimate prose (e.g. SOUL.md's Review Policy section
# discusses "`mc done` / `PATCH status: done` directly" as a status choice,
# which must keep passing).
FORBIDDEN_VERB_PATTERNS: dict[str, re.Pattern[str]] = {
    # `mc comment`'s type is a positional argument, not a --type flag.
    # An agent copy-pasting `mc comment --type reflection ...` gets
    # "unrecognized arguments" from argparse. Fixed in 7a80858c.
    "mc_comment_type_flag": re.compile(r"mc comment\s+--type\b"),
    # `mc checkpoint` does not exist as a CLI command (POST /checkpoint is
    # 410 Gone). Matches only an executable example line — the command
    # starts the line (ignoring leading whitespace/backtick) — not prose
    # explaining that checkpoints were retired.
    "mc_checkpoint_dead_command": re.compile(r"^[ \t]*`?mc checkpoint\b", re.MULTILINE),
    # `mc blocked` takes `--blocker-type`, not `--type`.
    "mc_blocked_wrong_flag": re.compile(r"mc blocked\s+--type\b"),
    # Internal Python identifier (cli_terminal.py) — must never leak into
    # agent-facing docs; if an agent reads this name it's reading source,
    # not documentation.
    "cli_bridge_protocol_identifier": re.compile(r"_CLI_BRIDGE_PROTOCOL"),
    # The pre-CLI instruction to PATCH status directly without the mc CLI
    # (and therefore without X-Dispatch-Attempt-Id) — 409s by design.
    "raw_patch_status_in_progress_instruction": re.compile(r"PATCH status:\s*in_progress\b"),
}


# ── L2 reference-doc topic registry ──────────────────────────────────────

@dataclass(frozen=True)
class DocTopicSpec:
    """Metadata for one L2 reference-doc topic.

    audience: "all", or a tuple of role_type values (as produced by
    template_renderer.build_agent_context's role_type_map, e.g.
    "lead"/"orchestrator"/"developer"/...) that should receive this doc.
    max_bytes: hard budget for the rendered doc — enforced by
    test_agent_docs_contract.py so L2 docs stay genuinely on-demand-sized,
    not a second copy of SOUL.md.
    when_to_read: one-line guidance rendered into docs/INDEX.md.
    """
    title: str
    audience: str | tuple[str, ...]
    max_bytes: int
    when_to_read: str


DOC_TOPICS: dict[str, DocTopicSpec] = {
    "telegram": DocTopicSpec(
        title="Telegram Reports",
        audience="all",
        max_bytes=9000,
        when_to_read="Before sending a report to the operator, or when a file/photo needs to be attached.",
    ),
    "pdf-office": DocTopicSpec(
        title="PDF & Office Documents",
        audience="all",
        max_bytes=7000,
        when_to_read="Before generating a PDF/DOCX/XLSX/PPTX deliverable.",
    ),
    "memory": DocTopicSpec(
        title="Memory-First Protocol",
        audience="all",
        max_bytes=3000,
        when_to_read="Before any non-trivial decision — check semantic/agent/episodic memory first.",
    ),
    "delegation": DocTopicSpec(
        title="Delegation Pattern",
        audience=("lead", "orchestrator"),
        max_bytes=9000,
        when_to_read="Before delegating a subtask or waiting on another agent's result.",
    ),
    "vault": DocTopicSpec(
        title="Vault Writing Discipline",
        audience="all",
        max_bytes=6000,
        when_to_read="Before writing a lesson/note to the shared Vault wiki.",
    ),
    "tasks": DocTopicSpec(
        title="Task Lifecycle & Verb Reference",
        audience="all",
        max_bytes=7000,
        when_to_read="When unsure which CLI verb applies, or for the close/reflection protocol.",
    ),
}
