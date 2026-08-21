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
    "CANONICAL_VERB_SCOPES",
    "CARD_VERBS",
    "filter_verbs_by_scopes",
    "filter_card_verbs_by_scopes",
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
#
# These descriptions render onto EVERY Operating Card, including agents
# without comm_v2 — so a description must not name a comm_v2-only tool
# (`mc inbox`, `mc msg`, `mc ask`) even as a contrast, or it becomes a
# dangling pointer for the agents that do not have it. Draw those contrasts
# in the comm_v2-gated SOUL blocks instead, where the tool exists. Guarded
# by test_card_inbox_reply_rule.py::test_card_rule_is_absent_without_comm_v2;
# `thread` tripped it exactly this way on 2026-07-31.
CANONICAL_VERBS: dict[str, str] = {
    "ack": "Confirm dispatch (status -> in_progress) — always your first call.",
    "done": "Set status -> done directly — for the mandatory close, prefer `mc finish`.",
    "patch": "Set status explicitly: done|review|in_progress|blocked|failed.",
    "task-get": "Fetch the current task's status and details.",
    "vault-search": "Full-text search across the Vault (notes, wrappers, PDFs).",
    "vault-related": "All notes/wrappers/lessons that share a task_id.",
    "vault-write": "Write a Vault note via the inbox API (shared paths).",
    "review": "Hand a task to review (status -> review).",
    "approve": "Approve a review (decision=approve).",
    "reject": "Request changes on a review (--feedback required).",
    "finish": "Post the mandatory reflection + set status — the canonical close verb.",
    "blocked": "Block a task with a question/description for the operator.",
    "failed": "Mark a task as failed.",
    "comment": "Post a comment (progress/blocker/feedback/resolution/handoff/message).",
    "ask": "Ask a thread-native question — --blocking pauses on the answer.",
    "msg": "Post a plain message/status/decision on the task thread (no questions — use `mc ask`).",
    "inbox": "Pull new thread messages and ack them (on 📬 nudge).",
    "group-doc": "Write your group's living result document — lead only (ADR-075).",
    "thread": "Re-read your own task thread — read-only, consumes nothing.",
    "checklist": "Manage the task checklist (add/done/skip/list).",
    "question": "Ask the operator a clarifying question.",
    "help": "Ask another agent for help.",
    "delegate": "Delegate a subtask to another agent with a callback wait.",
    "deliverable": "Register a deliverable.",
    "deliverable-get": "Read a deliverable's full content (verification route).",
    "report": "Send the final report to the operator's reports channel.",
    "telegram": "Send the final report to the operator (alias of `mc report`).",
    "verify": "Visual verification — screenshots + metrics via mc-playwright.",
    "pdf": "Render Markdown to PDF via the mc-playwright sidecar.",
    "memory": "Search memory (Qdrant + board memory).",
    "recover": "Fetch the current task prompt after a restart/crash.",
    "me": "Show own agent info (id, role, scopes, task, plugins).",
    "plugin-list": "List shared-cache plugins (board-lead-only).",
    "plugin-show": "Show a worker's plugin allowlist.",
    "plugin-assign": "Set a worker's plugin allowlist (replace).",
    "plugin-unassign": "Remove a plugin from a worker's allowlist.",
    "worker-restart": "Restart a cli-bridge worker's session.",
    "remember": "Save something to the Vault (shortcut for vault-write).",
    "file-answer": "Save a research result as a Vault note.",
    "docs": "Read a local reference doc — no network call, works offline.",
}


# ── Verb -> scope mapping ────────────────────────────────────────────────
#
# CARD.md.j2's '## Verbs' section is the single biggest block in the
# Operating Card (2563 of 5106 worst-case bytes, measured 2026-07-28) and
# used to render every CANONICAL_VERBS entry to every agent regardless of
# what it may actually call. Each value here is the app.scopes.Scope the
# backend's require_scope() demands on that verb's underlying endpoint
# (traced through scripts/mc-cli/mc_cli/commands.py:REGISTRY ->
# backend/app/routers/{agent_scoped,agent_task_status,agent_comments,
# tasks,vault}.py); None means the endpoint only requires require_agent
# (no scope check) and so the verb is a base verb, always shown.
#
# test_agent_docs_contract.py::test_canonical_verbs_are_registered keeps
# CANONICAL_VERBS honest against the real REGISTRY; the sibling test in
# test_card_verbs_by_scope.py::test_every_canonical_verb_has_a_scope_mapping
# keeps this dict's keys in exact sync with CANONICAL_VERBS.
CANONICAL_VERB_SCOPES: dict[str, str | None] = {
    "ack": "tasks:write",
    "done": "tasks:write",
    "patch": "tasks:write",
    "task-get": "tasks:read",
    "vault-search": "vault:read",
    "vault-related": "vault:read",
    "vault-write": "vault:write",
    "review": "tasks:write",
    "approve": "tasks:write",
    "reject": "tasks:write",
    "finish": "tasks:write",
    "blocked": "tasks:write",
    "failed": "tasks:write",
    "comment": "tasks:write",
    "ask": "chat:write",
    "msg": "chat:write",
    # Der Endpoint prueft zusaetzlich, dass der Aufrufer der LEAD der
    # Gruppe ist — der Scope allein oeffnet also nichts Fremdes.
    "group-doc": "chat:write",
    "inbox": None,  # GET /agent/me/inbox — require_agent only (Nudge+Pull)
    "thread": "tasks:read",  # GET /agent/me/thread — require_scope(TASKS_READ)
    "checklist": "tasks:write",
    "question": "tasks:help",
    "help": "tasks:help",
    "delegate": "tasks:create",
    "deliverable": "tasks:write",
    "deliverable-get": "tasks:read",
    "report": "chat:write",
    "telegram": "chat:write",
    "verify": "chat:write",
    "pdf": "tasks:write",
    "memory": "memory:read",
    "recover": None,  # GET /agent/me/active-task-recovery — require_agent only
    "me": None,  # GET /agent/me — require_agent only, explicit self-lookup
    "plugin-list": "agents:manage",
    "plugin-show": "agents:manage",
    "plugin-assign": "agents:manage",
    "plugin-unassign": "agents:manage",
    "worker-restart": "agents:manage",
    "remember": "vault:write",
    "file-answer": "vault:write",
    "docs": None,  # local file read, no network call, works offline
}


def filter_verbs_by_scopes(scopes: list[str] | None) -> dict[str, str]:
    """CANONICAL_VERBS, filtered to what the given effective scopes allow.

    Mirrors the `_has(scope)` pattern in services/tools_md_builder.py:
    scopes=None or scopes=[] means ALL_SCOPES (backward compat — an agent
    row with no scopes column set never loses tools), and a verb whose
    CANONICAL_VERB_SCOPES entry is None (base verb, no require_scope
    beyond require_agent) is always included.
    """
    if not scopes:
        return dict(CANONICAL_VERBS)
    allowed = set(scopes)
    return {
        verb: desc
        for verb, desc in CANONICAL_VERBS.items()
        if CANONICAL_VERB_SCOPES.get(verb) is None or CANONICAL_VERB_SCOPES[verb] in allowed
    }


# ── Which verbs the operating card carries inline ────────────────────────
#
# CARD.md is injected into the agent's context on every turn; `mc docs tasks`
# is fetched only when needed (offline, no network call) and already documents
# 37 of these verbs. Rendering the full table on the card duplicated that
# reference at ~2.5 KB — half of the card's 5120-byte budget — and left six
# bytes of headroom, so the next verb anyone added broke the budget test.
#
# The split is by kind, not by taste: the card carries the **task lifecycle**
# (anything that moves task state, plus talking and re-orienting), while
# **capability** verbs (vault, memory, report, pdf, verify, deliverable,
# delegate, plugins, …) live in `mc docs tasks`. An agent needs the lifecycle
# to work at all; it needs a capability only when the task calls for one, and
# then it can afford a lookup.
#
# `done` and `patch` are deliberately off the card even though they are
# lifecycle: both are documented as "prefer `mc finish`", and putting them
# in front of every agent on every turn advertises the path we do not want.
CARD_VERBS: tuple[str, ...] = (
    "ack",
    "task-get",
    "checklist",
    "comment",
    "finish",
    "review",
    "approve",
    "reject",
    "blocked",
    "failed",
    "ask",
    "msg",
    "inbox",
    "thread",
    "recover",
    "me",
    "docs",
)


def filter_card_verbs_by_scopes(scopes: list[str] | None) -> tuple[str, ...]:
    """CARD_VERBS, intersected with what the given effective scopes allow.

    Two independent reasons a verb may be off the card, and both must hold
    for it to appear: it is lifecycle (CARD_VERBS) *and* the agent's scopes
    let it call the endpoint (filter_verbs_by_scopes). The intersection is
    not cosmetic — CARD.md.j2 renders `canonical_verbs[verb]`, and
    canonical_verbs is itself scope-filtered, so a card verb the agent may
    not call would raise an undefined-key error at render time.
    """
    allowed = filter_verbs_by_scopes(scopes)
    return tuple(verb for verb in CARD_VERBS if verb in allowed)


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
    "report": DocTopicSpec(
        title="Operator Reports",
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
