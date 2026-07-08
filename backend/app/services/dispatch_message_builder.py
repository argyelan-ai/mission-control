"""
Dispatch Message Builder — Message Assembly extracted from dispatch.py (REF-01).

Owns: DispatchSection + budget constants + curl/auth-token formatters +
_build_review_message + _build_test_message + _build_dispatch_message +
build_planning_brief + _format_dispatch_message.

Phase-4 Boundary: Pure builder/formatter. No RPC. No DB writes (reads only via
the AsyncSession passed to async builders). Imports DispatchContext from
task_context_builder for type signatures.

Sibling of task_context_builder.py — together they replaced the dispatch.py
message-assembly block (Plan 04-02, REF-01 step 2). Re-export shim in
dispatch.py preserves all import sites and race-test patches (Pattern S1).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import settings
from app.models.agent import Agent
from app.models.board import Project
from app.models.deliverable import TaskDeliverable
from app.models.task import Task, TaskComment
from app.services.runtime_context import workspace_path_for_runtime
from app.services.task_context_builder import DispatchContext, _load_dispatch_context

logger = logging.getLogger(__name__)

# API base for agent callbacks — as a shell variable, expanded in the agent context
# Docker agents: MC_API_URL=http://backend:8000 (via docker-compose.agents.yml)
# Host/Gateway agents: MC_API_URL=http://localhost (via agent.env / workspace/.env)
_API_BASE = "$MC_API_URL"

# Dedup set for size warnings — keyed by (task_id, dispatch_attempt_id). The
# poll endpoint re-renders the dispatch message on every `/agent/me/poll`
# request, which spammed identical "over budget" lines under the prior ERROR
# log. We log once per attempt; a new attempt_id (re-dispatch on review fail,
# unblock, etc.) re-arms the log. Sized cap evicts FIFO so the set can't grow
# unbounded across a long-running process. See Bug 2026-05-12.
_SIZE_LOG_DEDUP: set[tuple[str, str]] = set()


# ── Dispatch-Message Budget (Workstream A2) ─────────────────────────────
#
# Token-wise claude-sonnet-4-6 has 200k context, but agent quality degrades
# past ~2000 token task-prompts ("lost in the middle"). These targets apply
# to the task-prompt body only — SOUL.md is accounted separately.
#
# Zone semantics:
#   0 .. TARGET   — green, best quality
#   TARGET .. WARN — yellow, logged info only
#   WARN .. HARD  — orange, warning event + still sent
#   > HARD        — red, graceful degradation drops optional sections until
#                    the message fits; mandatory sections are never cut.
DISPATCH_TARGET_CHARS = 2000
DISPATCH_WARN_CHARS = 2500
DISPATCH_HARD_CHARS = 4000

# When Qdrant/board memory is attached automatically, cap total size here.
# Agents pull more via `mc memory search` on demand (Workstream A3).
MEMORY_AUTO_MAX_CHARS = 800

# Phase 1 Adoption: agent lessons are injected as a droppable section.
# Budget separate from MEMORY_AUTO_MAX_CHARS — lessons are per-agent
# context, not per-task semantic hits.
LESSON_AUTO_MAX_CHARS = 400

@dataclass
class DispatchSection:
    """One renderable chunk of a dispatch message, with drop priority.

    `priority=0` is mandatory (task header, description, AC, credentials,
    recovery snippet). Higher values get dropped first when the message
    exceeds DISPATCH_HARD_CHARS.
    """
    name: str
    content: str
    priority: int = 0  # 0 = mandatory, higher = drop first


def render_agent_lessons_section(lessons_context: str) -> DispatchSection | None:
    """Render agent lessons as an optional dispatch section.

    Returns None if no lessons available (caller should skip).
    """
    if not lessons_context or not lessons_context.strip():
        return None

    lines = [l for l in lessons_context.strip().split("\n") if l.strip()]
    if not lines:
        return None

    header = "## Your Prior Lessons Learned\n"
    budget = LESSON_AUTO_MAX_CHARS - len(header)
    kept = []
    used = 0

    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1

    body = "\n".join(kept)
    if len(kept) < len(lines):
        body += "\n_(more via `mc vault-search`)_"

    return DispatchSection(
        name="agent_lessons",
        content=header + body,
        priority=2,
    )


def _assemble_with_budget(
    sections: list[DispatchSection],
    *,
    target: int = DISPATCH_TARGET_CHARS,
    warn: int = DISPATCH_WARN_CHARS,
    hard: int = DISPATCH_HARD_CHARS,
    task_id: str | uuid.UUID | None = None,
) -> str:
    """Join sections into a dispatch message while staying within the budget.

    Never truncates section content mid-stream — only drops entire optional
    sections in descending-priority order. Emits a warning log past `warn`
    and an error log past `hard` (after dropping).
    """
    keep = list(sections)
    total = sum(len(s.content) for s in keep)

    while total > hard and any(s.priority > 0 for s in keep):
        # Drop the lowest-priority (= highest number) optional section.
        dropped = max((s for s in keep if s.priority > 0), key=lambda s: s.priority)
        keep.remove(dropped)
        total -= len(dropped.content)
        logger.info(
            "dispatch_budget: dropped section=%s size=%d task=%s",
            dropped.name, len(dropped.content), task_id,
        )

    if total > hard:
        # All mandatory — we must send it anyway but the caller should know.
        logger.error(
            "dispatch_budget: over hard cap size=%d hard=%d task=%s "
            "(mandatory-only sections remain)",
            total, hard, task_id,
        )
    elif total > warn:
        logger.warning(
            "dispatch_budget: past warn cap size=%d warn=%d target=%d task=%s",
            total, warn, target, task_id,
        )
    elif total > target:
        logger.debug("dispatch_budget: past target size=%d task=%s", total, task_id)

    return "\n".join(s.content for s in keep)


def _extract_auth_token(agent: Agent) -> str | None:
    """Bearer token placeholder for dispatch curl commands.

    Per ADR-023 Ultrareview (security finding): NEVER embed plaintext tokens
    in dispatch messages — they end up in Redis pubsub, recovery recaps,
    Discord logs, SSE streams.
    All runtimes (cli-bridge, host, openclaw) get `$MC_AGENT_TOKEN` as a
    shell variable. The OpenClaw Gateway injects the token per session into
    the env variable; cli-bridge/host read it from agent.env.
    """
    return "$MC_AGENT_TOKEN"


def _curl(method: str, path: str, token: str | None, body: str | None = None,
          dispatch_attempt_id: str | None = None) -> str:
    """Build a complete curl command — self-contained, no TOOLS.md lookup needed."""
    parts = [f'curl -X {method} "{_API_BASE}{path}"']
    if token:
        parts.append(f'  -H "Authorization: Bearer {token}"')
    if dispatch_attempt_id:
        parts.append(f'  -H "X-Dispatch-Attempt-Id: {dispatch_attempt_id}"')
    if body:
        parts.append('  -H "Content-Type: application/json"')
        parts.append(f"  -d '{body}'")
    return " \\\n".join(parts)


async def _build_review_message(
    task: Task,
    reviewer: Agent,
    session: AsyncSession,
    developer: Agent | None = None,
) -> str:
    """Review message for reviewer agents — with developer code context.

    Loads the latest developer comments (progress/resolution) and the
    workspace path, so the reviewer knows WHERE and WHAT to check.

    All API calls as complete curl commands — self-contained.
    """
    token = _extract_auth_token(reviewer)
    task_path = f"/api/v1/agent/boards/{task.board_id}/tasks/{task.id}"
    review_path = f"{task_path}/review"

    # Precompute curl commands — new review endpoint (1 action instead of 2)
    approve_curl = _curl("POST", review_path, token,
                         '{"decision": "approve", "comment": "Update — Approved: [Grund]"}')
    reject_curl = _curl("POST", review_path, token,
                        '{"decision": "request_changes", "comment": "Problem — ... / Erwartung — ... / Action — ..."}')

    # ── Determine developer (for workspace path + name) ──
    if not developer and task.assigned_agent_id:
        # At review handoff, assigned_agent_id was already set to the reviewer.
        # Find the developer via the last progress comment.
        dev_comment_result = await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.author_type == "agent",
                TaskComment.comment_type.in_(["progress", "resolution"]),  # type: ignore[union-attr]
            )
            .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
            .limit(1)
        )
        last_dev_comment = dev_comment_result.first()
        if last_dev_comment and last_dev_comment.author_agent_id:
            developer = await session.get(Agent, last_dev_comment.author_agent_id)

    # ── Load developer comments (progress + resolution) ──
    dev_comments_text = ""
    try:
        comments_result = await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.author_type == "agent",
                TaskComment.comment_type.in_(["progress", "resolution"]),  # type: ignore[union-attr]
            )
            .order_by(TaskComment.created_at.asc())  # type: ignore[union-attr]
        )
        dev_comments = comments_result.all()
        if dev_comments:
            comment_lines = []
            for c in dev_comments[-3:]:  # At most the 3 most recent
                # Cap at 800 characters so the message doesn't explode
                content = c.content[:800]
                if len(c.content) > 800:
                    content += "\n[...truncated]"
                comment_lines.append(f"### {c.comment_type.title()} ({c.created_at.strftime('%H:%M')})\n{content}")
            dev_comments_text = "\n\n".join(comment_lines)
    except Exception:
        pass  # best-effort

    # ── Distinguish phase review vs code review ──
    _child_result = await session.exec(
        select(Task).where(Task.parent_task_id == task.id).limit(1)
    )
    _is_phase_review = _child_result.first() is not None

    # ── Assemble message ──
    dev_name = developer.name if developer else "Unknown"

    if _is_phase_review:
        # Phase/root review: check the overall result, not individual code
        msg = f"# Phase Review: {task.title}\n\n"
        msg += f"**Task ID:** {task.id}\n"
        msg += f"**Board:** {task.board_id}\n\n"
        msg += "## IMPORTANT: This is a phase review\n"
        msg += "You're reviewing the **overall result** of this phase, not a single code task.\n"
        msg += "All subtasks of this phase are complete.\n\n"
        msg += "**Check:**\n"
        msg += "- Does the overall result fulfill the request?\n"
        msg += "- Are all required artifacts present?\n"
        msg += "- Are there any obvious gaps or errors?\n\n"
        msg += "**Do NOT check:** individual lines of code (the subtasks already covered that).\n\n"
    else:
        msg = f"# Code Review: {task.title}\n\n"
        msg += f"**Task ID:** {task.id}\n"
        msg += f"**Board:** {task.board_id}\n"
        msg += f"**Developer:** {dev_name}\n\n"

    if task.description:
        msg += f"## Description\n{task.description}\n\n"

    # Code location: Task.workspace_path (source of truth, Bundle 4)
    # Fallback: project workspace > mc_repo_path > agent workspace
    _code_path = getattr(task, "workspace_path", None)
    _project: Project | None = None
    if task.project_id:
        _project = await session.get(Project, task.project_id)
    if not _code_path and _project and _project.workspace_path:
        _code_path = _project.workspace_path
    if not _code_path:
        _code_path = getattr(settings, 'mc_repo_path', None)
    if not _code_path and developer:
        _code_path = developer.workspace_path
    if _code_path:
        # Review FS-2: the path is embedded in the reviewer's prompt, so
        # rewrite against the reviewer's runtime (cli-bridge → /workspace/…)
        # instead of the developer's. Matters when reviewer + developer
        # are on different runtimes (reviewer on cli-bridge, developer on
        # host for example); otherwise no visible diff.
        _agent_path = workspace_path_for_runtime(reviewer, _code_path)
        msg += f"## Code Location\n"
        msg += f"**Working directory:** `{_agent_path}/`\n"
        if getattr(task, "workspace_port", None):
            msg += f"**Dev server port:** {task.workspace_port}\n"
        msg += f"Change into this directory FIRST: `cd {_agent_path}`\n"
        msg += f"Search there for the relevant files.\n\n"

    # T-1 Phase D: project config section from project_config
    if _project and _project.project_config:
        from app.services.work_context import resolve_project_config, build_config_dispatch_section
        _resolved = resolve_project_config(
            auto_config=None,
            manual_config=_project.project_config,
        )
        if _resolved:
            _port = getattr(task, "workspace_port", None)
            msg += build_config_dispatch_section(_project.name, _resolved, port=_port) + "\n\n"

    if task.target_url:
        msg += f"**Target URL:** {task.target_url}\n"
        msg += "Open this URL in the browser to visually check the result.\n\n"

    # Developer comments (evidence of what was done)
    if dev_comments_text:
        msg += f"## What the developer did\n{dev_comments_text}\n\n"

    # ── Subtask evidence (only for phase/root reviews) ──
    # Subtasks are `done` after completion and no longer visible in the board context.
    # We load them explicitly here so the reviewer can see the work that was done.
    if _is_phase_review:
        subtasks_result = await session.exec(
            select(Task).where(Task.parent_task_id == task.id)
        )
        subtasks = subtasks_result.all()
        if subtasks:
            agent_id_map: dict = {}
            subtask_sections = []
            for sub in subtasks:
                # Cache agent name
                if sub.assigned_agent_id and sub.assigned_agent_id not in agent_id_map:
                    sub_agent = await session.get(Agent, sub.assigned_agent_id)
                    agent_id_map[sub.assigned_agent_id] = sub_agent.name if sub_agent else "?"
                sub_agent_name = agent_id_map.get(sub.assigned_agent_id, "?") if sub.assigned_agent_id else "?"

                section = f"### [{sub.status}] {sub.title} ({sub_agent_name})\n"
                section += f"ID: `{sub.id}`\n"

                # Subtask comments (progress + resolution, max 3 most recent)
                sub_comments_result = await session.exec(
                    select(TaskComment)
                    .where(
                        TaskComment.task_id == sub.id,
                        TaskComment.author_type == "agent",
                        TaskComment.comment_type.in_(["progress", "resolution"]),  # type: ignore[union-attr]
                    )
                    .order_by(TaskComment.created_at.desc())  # type: ignore[union-attr]
                    .limit(3)
                )
                sub_comments = list(reversed(sub_comments_result.all()))
                if sub_comments:
                    for c in sub_comments:
                        content = c.content[:600]
                        if len(c.content) > 600:
                            content += "\n[...truncated]"
                        section += f"\n**{c.comment_type.title()}** ({c.created_at.strftime('%H:%M')}):\n{content}\n"

                # Deliverables of the subtask
                sub_deliverables_result = await session.exec(
                    select(TaskDeliverable).where(TaskDeliverable.task_id == sub.id)
                )
                sub_deliverables = sub_deliverables_result.all()
                if sub_deliverables:
                    section += "\n**Deliverables:**\n"
                    for d in sub_deliverables:
                        path_hint = f" → `{d.path}`" if d.path else ""
                        section += f"- [{d.deliverable_type}] {d.title}{path_hint}\n"

                subtask_sections.append(section)

            msg += "## Subtask Evidence\n"
            msg += "*(Subtasks are `done` once complete and no longer visible in the board context — loaded explicitly here)*\n\n"
            msg += "\n---\n".join(subtask_sections) + "\n\n"

    # Load PR link (if present)
    pr_comment = None
    try:
        pr_result = await session.exec(
            select(TaskComment)
            .where(
                TaskComment.task_id == task.id,
                TaskComment.content.like("%PR erstellt:%"),
            )
            .order_by(TaskComment.created_at.desc())
            .limit(1)
        )
        pr_comment = pr_result.first()
    except Exception:
        pass

    if pr_comment:
        msg += f"## Pull Request\n{pr_comment.content}\n\n"
        msg += f"Use `gh pr diff` or the GitHub UI to check the changes.\n\n"

    # Reviewer's process rules (from rules_md)
    if reviewer.rules_md:
        msg += f"## Process Rules (MANDATORY)\n\n{reviewer.rules_md}\n\n"

    msg += f"""## Your Task

Read the relevant code in the developer's workspace. Run the tests. Decide: Approved or Request Changes.

**IMPORTANT:** Use the review endpoint — ONE action for comment + decision:

---

### APPROVED — review passed
If the code is good, tests pass, requirements are met:

```bash
{approve_curl}
```

---

### REQUEST CHANGES — changes needed
ONLY if there are real problems that need to be fixed:

```bash
{reject_curl}
```

Comment format for rejection:
**Problem** — what exactly is wrong
**Expectation** — how it should be
**Action** — what the developer should change

---

**A comment alone does not close a review.** You MUST use the review endpoint.

You don't write or change any code. Only read and judge."""
    return msg


async def _build_test_message(
    task: Task,
    tester: Agent,
    session: AsyncSession,
) -> str:
    """Test message for tester agent — browser-based QA."""
    token = _extract_auth_token(tester)
    task_path = f"/api/v1/agent/boards/{task.board_id}/tasks/{task.id}"

    ack_curl = _curl("PATCH", task_path, token, '{"status": "in_progress"}')
    done_curl = _curl("PATCH", task_path, token, '{"status": "done"}')
    reject_curl = _curl("PATCH", task_path, token, '{"status": "in_progress"}')
    comment_curl = _curl("POST", f"{task_path}/comments", token,
                         '{"content": "...", "comment_type": "progress"}')
    video_curl = _curl(
        "POST", "/api/v1/agent/me/deliverable", token,
        '{"deliverable_type": "video", "title": "E2E test recording", '
        f'"path": "/shared-mcp/{task.id}/e2e-run.webm"}}',
    )

    msg = f"# QA Test: {task.title}\n\n"
    msg += f"**Task ID:** {task.id}\n"
    msg += f"**Board:** {task.board_id}\n\n"

    # ── Determine test URL: target_url > workspace_port > localhost ──
    if task.target_url:
        _test_url = task.target_url
    elif getattr(task, "workspace_port", None):
        _test_url = f"http://localhost:{task.workspace_port}"
    else:
        _test_url = "http://localhost"

    if task.description:
        msg += f"## Assignment\n{task.description}\n\n"

    if task.acceptance_criteria:
        msg += f"## Acceptance Criteria\n{task.acceptance_criteria}\n\n"

    if task.target_url:
        msg += f"**Target URL:** {task.target_url}\n\n"
    elif getattr(task, "workspace_port", None):
        msg += f"**Dev server:** http://localhost:{task.workspace_port}\n\n"

    msg += f"""## Your Task

You are the QA tester. PASS only if everything works when you USE it.

You test through the **Playwright MCP server** (tools `mcp__playwright__browser_*`).
The browser session is STATEFUL — one context until `browser_close`. Screenshots
come back inline in the tool response, so you SEE what you verify. Save them
under your task ID so they get captured as deliverables.

### Mandatory workflow

**Step 1: ACK the task**
```bash
{ack_curl}
```

**Step 2: Start recording — BEFORE you navigate anywhere**
```
browser_start_video(filename="{task.id}/e2e-run.webm")
browser_video_show_actions()
```
This captures your whole run (with action overlays) as a video the operator
can watch afterwards. Do this immediately after the ACK, before any
navigation, so nothing is missed.

**Step 3: Load the page (desktop)**
```
browser_navigate(url="{_test_url}")
browser_take_screenshot(filename="{task.id}/test-desktop.png")
```
Check: page loads, all elements visible, layout/colors/fonts correct.

**Step 4: Drive the real user flows (the core of this test)**
Work through the acceptance criteria above as flows a human would perform —
navigate, click, type, submit. Base every interaction on element refs from a
fresh snapshot:
```
browser_snapshot()                                  # accessibility tree with refs
browser_click(element="Submit button", ref="<ref>")
browser_type(element="Email field", ref="<ref>", text="test@example.com")
browser_fill_form(fields=[...])                     # several fields at once
browser_wait_for(text="Saved")                      # wait for the visible result
browser_take_screenshot(filename="{task.id}/test-interaction.png")
```
Verify the OUTCOME of each flow (visible result, navigation, persisted data
after reload) — not just that clicking didn't crash.

**Step 5: Mobile pass**
```
browser_resize(width=402, height=874)
browser_navigate(url="{_test_url}")
browser_take_screenshot(filename="{task.id}/test-mobile.png")
```
Then `browser_close()` when done.

**Step 6: Stop recording and register it as a deliverable**
```
browser_stop_video()
```
```bash
{video_curl}
```
Do this for both PASS and FAIL runs — the operator wants to see the run
either way.

**Step 7: Document the result**
```bash
{comment_curl}
```

On PASS:
```
**Result:** TEST_PASS
**Desktop:** {task.id}/test-desktop.png — OK
**Mobile:** {task.id}/test-mobile.png — OK
**Flows tested:** [each flow + outcome]
**Recording:** {task.id}/e2e-run.webm
**Summary:** Everything works as expected
```

On FAIL:
```
**Result:** TEST_FAIL
**Problem:** [which flow/element] — expected: [X], actual: [Y]
**Screenshots:** [filenames]
**Recording:** {task.id}/e2e-run.webm
**Recommendation:** [what the builder needs to fix]
```

**Step 8: Decision**
- TEST_PASS → done:
```bash
{done_curl}
```
- TEST_FAIL → back to the builder (in_progress):
```bash
{reject_curl}
```
On FAIL: the task automatically goes back to the developer who built it.

### FORBIDDEN
- Do NOT change any code
- Do NOT make fixes yourself — that's the builder's job
- ONLY test and document
"""

    return msg


async def _build_dispatch_message(
    task: Task,
    agent: Agent,
    session: AsyncSession,
    recovery_context: str | None = None,
) -> str:
    """Structured dispatch message with context + callback protocol.

    For review tasks (status='review'), _build_review_message is used automatically.
    Uses DispatchContext for parallel DB queries (asyncio.gather).
    """
    if task.status == "review":
        return await _build_review_message(task, agent, session)

    if task.parent_task_id and (not task.credentials_encrypted or not task.credential_id):
        parent_task = await session.get(Task, task.parent_task_id)
        if parent_task is not None:
            # Inherit inline credentials from the parent (existing behavior)
            if not task.credentials_encrypted and parent_task.credentials_encrypted:
                setattr(task, "_inherited_credentials_encrypted", parent_task.credentials_encrypted)
            # Inherit the vault credential ID from the parent — symmetric to the inline
            # inheritance. Without this block, credential_id stays isolated on the root
            # task; subtasks wouldn't get the vault credentials in their dispatch context.
            if not task.credential_id and parent_task.credential_id:
                setattr(task, "_inherited_credential_id", parent_task.credential_id)

    ctx = await _load_dispatch_context(task, agent, session)
    return _format_dispatch_message(task, agent, ctx, recovery_context)


def build_planning_brief(task: Task) -> str | None:
    """Builds a structured planning brief from operator-intake fields.

    Only for root/intake tasks with intake_mode set.
    Workers do NOT get this brief — only Henry (Board Lead).
    """
    if not getattr(task, "intake_mode", None):
        return None

    sections = [f"\n## Operator Briefing ({task.intake_mode})"]

    if task.request_kind:
        sections.append(f"**Request type:** {task.request_kind}")
    if getattr(task, "desired_output", None):
        sections.append(f"**Desired output:** {task.desired_output}")
    if task.acceptance_criteria:
        sections.append(f"**Acceptance criteria:** {task.acceptance_criteria}")
    if getattr(task, "scope_out", None):
        sections.append(f"**Out of scope:** {task.scope_out}")
    if getattr(task, "risk_notes", None):
        sections.append(f"**Risks / don't break:** {task.risk_notes}")
    if getattr(task, "needs_browser", None):
        sections.append("**Browser needed:** Yes")
    if task.requires_auth:
        sections.append("**Credentials needed:** Yes")
    if getattr(task, "approval_policy", None):
        sections.append(f"**Approval policy:** {task.approval_policy}")
    if getattr(task, "autonomy_level", None):
        sections.append(f"**Autonomy:** {task.autonomy_level}")
    ref_urls = getattr(task, "reference_urls", None)
    if ref_urls:
        sections.append(f"**References:** {', '.join(ref_urls)}")
    if getattr(task, "reference_notes", None):
        sections.append(f"**Notes:** {task.reference_notes}")
    if getattr(task, "publish_allowed", None) is not None:
        sections.append(f"**Publishing:** {'Allowed' if task.publish_allowed else 'Not allowed'}")

    return "\n".join(sections) if len(sections) > 1 else None


def _format_dispatch_message(
    task: Task,
    agent: Agent,
    ctx: DispatchContext,
    recovery_context: str | None = None,
) -> str:
    """Pure function — builds the dispatch message from preloaded context.

    No DB dependency, so it's unit-testable.
    """
    is_redispatch = bool(ctx.feedback_context)

    # ── Phase 3: Sections with priority. Optional sections (priority>0) are
    # dropped by `_assemble_with_budget` when total exceeds DISPATCH_HARD_CHARS.
    # Priority key:
    #   0 = mandatory (header, description, credentials, recovery, role-specific orchestration)
    #   1 = drop second-to-last (project context, dependency context — re-queryable)
    #   2 = drop second (semantic memory, child-task list — informational)
    #   3 = drop first (planning brief — Board Lead has task.description anyway)
    sections: list[DispatchSection] = []

    def _add(name: str, content: str, priority: int = 0) -> None:
        sections.append(DispatchSection(name=name, content=content, priority=priority))

    # Header (mandatory)
    _add("header", "\n".join([
        f"# {'CORRECTION NEEDED' if is_redispatch else 'New Task'}: {task.title}",
        f"**Priority:** {task.priority}",
        f"**Task ID:** {task.id}",
        f"**Board ID:** {task.board_id}",
    ]))

    # Response language (mandatory). Templates/prompts are English; the
    # per-agent `language` field steers how the agent talks to the operator.
    # Injected at dispatch time so it covers every agent kind — template-
    # created (no Jinja2 pass), specialized, and pre-existing fleets.
    lang = (getattr(agent, "language", "en") or "en").lower()
    if lang != "en":
        _add("language", (
            f"**Language:** Respond to your operator in `{lang}` "
            "(comments, reports, questions). Code and commits stay English."
        ))

    # On re-dispatch: show reviewer feedback prominently (mandatory)
    if ctx.feedback_context:
        _add("feedback", f"\n## ⚠ Review Feedback — you need to fix this\n{ctx.feedback_context}")

    if task.description:
        _add("description", f"\n## Description\n{task.description}")

    if task.target_url:
        _add("target_url", f"**Target URL:** {task.target_url}")

    # Help request context (mandatory)
    if task.help_request_from:
        requester_info = task.auto_reason or "another agent"
        _add("help_request", f"""
---
## ⚡ Help Request
This is a help request from {requester_info}.
The requesting agent is blocked and waiting for your result.

**What you need to do:**
1. Work the task (see description above)
2. Register the result as a deliverable (POST deliverables)
3. Write a short summary comment (POST comments)
4. Set the task to done (PATCH status: done)

The requesting agent automatically resumes with your result.
---""")

    # Planning Brief (optional, priority=3) — Board Lead + Root-Task only,
    # recaps operator-intake fields. Board Lead has task.description anyway.
    if agent.is_board_lead and not task.parent_task_id:
        brief = build_planning_brief(task)
        if brief:
            _add("planning_brief", brief, priority=3)

    # Decrypt encrypted credentials and include them
    # Priority: 1) vault credential (preloaded in ctx), 2) inline (credentials_encrypted), 3) parent-inherited
    creds_text = ctx.credentials_text

    if not creds_text:
        # Inline credentials or parent-inherited (as before)
        creds_encrypted = task.credentials_encrypted or getattr(task, "_inherited_credentials_encrypted", None)
        if creds_encrypted:
            from app.services.encryption import safe_decrypt
            decrypted_creds = safe_decrypt(creds_encrypted)
            if decrypted_creds:
                creds_text = decrypted_creds

    if creds_text:
        # Credentials are mandatory — agent literally cannot do the task without them
        _add("credentials", f"\n## Credentials\n{creds_text}")

    # Referenz-Dateien (ADR-053): vom Operator hochgeladene Beispiele/Assets.
    # Pfade sind im Agent-Container identisch lesbar (1:1 ~/.mc-Mount).
    if getattr(ctx, "reference_files_context", ""):
        _add("reference_files", (
            "\n## Reference files (uploaded by the operator)\n"
            "Read these directly from disk — same absolute paths in your container. "
            "Use them as examples/assets for this task:\n"
            f"{ctx.reference_files_context}"
        ))

    # Project context (optional priority=1 — agent can read project via API if needed)
    if ctx.project:
        project_parts = ["\n## Project Context", f"**Project:** {ctx.project.name}"]
        if ctx.project_tags:
            project_parts.append(f"**Tags:** {', '.join(ctx.project_tags)}")
        if ctx.project.description:
            project_parts.append(f"**Description:** {ctx.project.description}")
        _add("project_context", "\n".join(project_parts), priority=1)

    # Dependency context (optional priority=1 — re-queryable via mc context predecessors)
    if ctx.dependency_context:
        _add(
            "dependency_context",
            f"\n## Results of Predecessor Tasks\n\nThese tasks were completed before you. Use their workspaces and outputs as a starting point:\n\n{ctx.dependency_context}",
            priority=1,
        )

    # Memory (optional priority=2 — Top-3 semantic hits. Agent can pull more via
    # `mc memory search` on demand. Capped at MEMORY_AUTO_MAX_CHARS even if kept.)
    if ctx.semantic_memory_context:
        _mem = ctx.semantic_memory_context
        if len(_mem) > MEMORY_AUTO_MAX_CHARS:
            _mem = _mem[: MEMORY_AUTO_MAX_CHARS - 40].rstrip() + (
                "\n\n(more hits via `mc memory search`)"
            )
        _add("semantic_memory", f"\n## Relevant Memory (Top-3 Vector)\n{_mem}", priority=2)

    # Agent Lessons (Phase 1 Adoption): rendered as a DispatchSection so they
    # participate in _assemble_with_budget and can be dropped under pressure.
    _lessons_section: DispatchSection | None = None
    if ctx.agent_lessons_context:
        _lessons_section = render_agent_lessons_section(ctx.agent_lessons_context)

    # Agent's process rules (mandatory — agent-specific extra rules from DB)
    if agent.rules_md:
        _add("agent_rules", f"\n## Process Rules (MANDATORY)\n\n{agent.rules_md}")

    # Auth token from TOOLS.md for self-contained curl commands — Board-Lead /
    # Planner blocks below still render curl (their orchestration workflow
    # needs create_task payloads that `mc` does not yet cover).
    token = _extract_auth_token(agent)
    task_path = f"/api/v1/agent/boards/{task.board_id}/tasks/{task.id}"
    comment_path = f"{task_path}/comments"
    attempt_id = getattr(task, "dispatch_attempt_id", None)
    # Subtasks → `mc done` (phase review runs at the parent level).
    # Roots → dev decides on their own (ADR-023): `mc review` if code/API/security,
    # otherwise `mc done` directly. Policy details in SOUL.md (## Review-Policy).
    _is_subtask = task.parent_task_id is not None

    # ── ACK-Reminder (Lifecycle moved to SOUL.md.j2 — Phase 2/2.2) ───────
    # The full Lifecycle command catalog (progress/blocked/failed/finish syntax)
    # now lives in SOUL.md worker footer + role blocks. The ONE thing dispatch
    # adds that's task-specific is the "ACK or get re-assigned in 10 min" hint
    # for tasks that haven't been ACK'd yet. For re-dispatch (status already
    # in_progress, recovery context attached), no ACK reminder is needed —
    # SOUL footer "Recovery-Protokoll" covers what the agent needs.
    if task.status != "in_progress":
        _add(
            "ack_reminder",
            "\n**ACK IMMEDIATELY** with `mc ack` before you start — without an ACK "
            "the task gets re-assigned after 10 min. "
            "Progress: `mc comment progress \"...\"`. "
            "If you hit problems: `mc blocked --question \"...\"`. "
            "Full lifecycle (finish/failed/help/question) — see SOUL.md.",
        )

    # ── Recovery-Kontext (mandatory — without it, the agent re-starts from scratch
    #    after session loss, duplicating work) ──
    if recovery_context:
        _add("recovery_context", recovery_context)

    # ── Per-Phase 1+2 (2026-05-23): Worker-Truth/Lifecycle/Two-Zone moved
    #    to SOUL.md.j2. HEARTBEAT.md was deleted in migration 0125 (never
    #    read by agents). Only role-specific task-content (orchestrator
    #    delegation block / worker arbeitsweise+git_section) stays here. ──

    # ── Active subtasks (optional priority=2 — Board Lead overview, informational;
    #    re-queryable via mc tasks list) ──
    if agent.is_board_lead and ctx.child_tasks:
        agent_name_map = {a.id: a.name for a in ctx.team_agents}
        agent_name_map[agent.id] = agent.name
        child_lines = []
        for ct in ctx.child_tasks:
            agent_label = agent_name_map.get(ct.assigned_agent_id, "?") if ct.assigned_agent_id else "unassigned"
            child_lines.append(f"- [{ct.status}] \"{ct.title}\" ({agent_label})")
        _add("child_tasks", "\n## Active Subtasks\n" + "\n".join(child_lines), priority=2)

    if agent.is_board_lead:
        # ── Board Lead: delegation instructions instead of implementation ──
        create_task_path = f"/api/v1/agent/boards/{task.board_id}/tasks"

        team_lines = []
        developer_names = []
        reviewer_names = []
        for ta in ctx.team_agents:
            role_hint = ta.role or ta.name
            runtime_hint = f", runtime={ta.agent_runtime}" if getattr(ta, "agent_runtime", "openclaw") != "openclaw" else ""
            team_lines.append(f"- **{ta.name}** ({role_hint}{runtime_hint}): `{ta.id}`")
            r = (ta.role or "").lower()
            if "review" in r:
                reviewer_names.append(ta.name)
            elif r not in ("lead", "planner"):
                developer_names.append(ta.name)

        team_section = "\n".join(team_lines) if team_lines else "- (No team agents found — check board config)"
        _dev_names_str = ", ".join(developer_names) if developer_names else "developer agents"
        _rev_names_str = ", ".join(reviewer_names) if reviewer_names else "reviewer agents"

        subtask_body = (
            '{"title": "Concrete task", '
            '"description": "## Goal\\nWhat exactly should be achieved.\\n\\n'
            '## Context\\n- Path: ~/Workspace/Projects/mission-control/\\n'
            '- URL: http://localhost\\n- Stack: FastAPI + Next.js\\n\\n'
            '## Guardrails\\n- Do not change the DB schema\\n- PR only, do not merge\\n\\n'
            '## Expected Output\\n- PR with changes\\n- Before/after screenshots\\n\\n'
            '## Definition of Done\\n- Tests green\\n- Screenshots attached", '
            '"credential_id": "CREDENTIAL-UUID-FROM-VAULT-OR-OMIT", '
            f'"parent_task_id": "{task.id}", '
            '"assigned_agent_id": "AGENT-UUID-HERE", '
            '"priority": "medium", '
            '"tags": ["relevant-tag"]}'
        )
        delegate_curl = _curl("POST", create_task_path, token, subtask_body)

        # Planner-mode instruction removed (Phase 6, 2026-04-11).
        # Boss plans on its own via openclaude subagents, delegates directly to workers.
        _planner_instruction = ""  # empty — no longer rendered in the template string below

        _add("orchestrator_instructions", f"""
## Orchestrator Instructions

You are the orchestrator. Implement NOTHING yourself — delegate EVERYTHING to your team.

### Your Team
{team_section}

### Step 1: Create and delegate a subtask
```bash
{delegate_curl}
```
Replace "AGENT-UUID-HERE" with the UUID of the matching agent.
IMPORTANT: parent_task_id MUST be `{task.id}` (your task ID).

Create one subtask per agent. If a subtask needs to wait on another:
add `"depends_on": ["other-subtask-id"]`.

### MANDATORY: Delegation checklist
Every task description MUST contain these 5 points:
1. **Goal** — what exactly should be achieved?
2. **Context** — paths, URLs, files, stack info
3. **Guardrails** — what NOT to do
4. **Expected output** — screenshots, PRs, files
5. **Definition of Done** — measurable completion criteria

**Credentials** (ADR-033) — if a task needs a login/token:
- **Primary:** `credential_id` (UUID from the vault, Settings → Credentials). The agent fetches it at runtime via `GET /api/v1/agent/boards/{{board_id}}/credentials/{{credential_id}}`.
- **Fallback (one-shot):** inline `credentials` field `"credentials": "email: x@y.z / password: ..."`. Stored encrypted, the agent gets it decrypted.
- **NEVER** write a login/key into the description. If unknown: ask the operator, do NOT guess.
- **NEVER** reference system tokens (OpenAI, GitHub, Discord, OpenClaw) — those live in `secrets` and backend services fetch them themselves.

IMPORTANT: The agent has NO chat context. Everything must be in the description.

### IMPORTANT: Don't create review tasks
Create ONLY implementation tasks for developer agents ({_dev_names_str}).
NO separate review tasks for reviewer agents ({_rev_names_str}).
The review happens AUTOMATICALLY — when a developer sets their task to "review", a
reviewer is notified automatically and checks the code.

If you manually create a review task, the reviewer reviews BEFORE the developer is done.

### Step 2: Write a progress comment
```bash
mc comment progress "**Update** — delegated subtask X to {_dev_names_str.split(", ")[0] if developer_names else "the developer"}
**Evidence** — subtask IDs: [IDs here]
**Next** — waiting for completion"
```

### Step 3: Wait
Your parent task gets automatically set to in_progress once you create the first subtask.
When all subtasks are done, the watchdog automatically sets your task to review.

IMPORTANT: NEVER create tasks without parent_task_id — otherwise they get assigned to you.

### Project workflow
For large tasks (website, app, feature with multiple steps):
1. Create a project: POST {create_task_path.rsplit('/tasks', 1)[0]}/projects
   Body: {{"name": "Project name", "description": "...", "project_type": "feature"}}
2. Create subtasks with parent_task_id + project_id
3. Phases then run fully automatically — you don't need to wait for the operator""")
        # End of _add("orchestrator_instructions", ...)
    # Planner branch removed (Migration 0086, ADR-022 review) — Boss
    # orchestrates directly. Any legacy task with `delegation_type="planning"`
    # or an agent with "planner" in role falls through to the regular
    # worker branch; Boss re-assigns via his standard orchestration flow.
    else:
        # ── Regular agent: implementation instructions ──
        project = ctx.project

        # Two-zone convention moved to SOUL.md.j2 (common header Zone 1/Zone 2
        # + per-role blocks). Was duplicated per task; now lives only in SOUL.

        # ── Git section: dynamic based on project repo ──
        if not getattr(agent, 'requires_git_workflow', True):
            git_section = ""  # Non-coder agent — no git instructions
        elif project and project.github_repo_url and agent.workspace_path:
            from app.services.git_service import slugify_project
            _proj_slug = slugify_project(project.name)
            _task_slug = slugify_project(task.title)
            _work_dir_host = getattr(task, "workspace_path", None) or project.workspace_path or f"{agent.workspace_path}/{_proj_slug}"
            _work_dir = workspace_path_for_runtime(agent, _work_dir_host) or _work_dir_host
            git_section = (
                f"**Git workflow:**\n"
                f"- Repository: `{project.github_repo_name}`\n"
                f"- Your branch: `task/{_task_slug}`\n"
                f"- Working directory: `{_work_dir}/`\n"
                f"- Commit after every major step with a meaningful message\n"
                f"- Before review: `git add . && git commit -m \"...\" && git push origin task/{_task_slug}`\n"
                f"- NEVER commit to main — only to your task branch\n"
                f"- Don't commit API keys, tokens, or passwords"
            )
            if task.workspace_port:
                git_section += (
                    f"\n\n**Dev server port:** {task.workspace_port} (use this port, NOT the default)\n"
                    f"Start the dev server: `npm run dev -- -p {task.workspace_port}` or `python -m http.server {task.workspace_port}`"
                )
        elif project and project.workspace_path:
            git_section = (
                f"**Working directory:** `{project.workspace_path}/`\n"
                f"**Git:** commit after every major step with a meaningful message. Push to GitHub.\n"
                "Use a feature branch — never directly on main."
            )
            if task.workspace_port:
                git_section += (
                    f"\n**Dev server port:** {task.workspace_port} (use this port, NOT the default)\n"
                    f"Start the dev server: `npm run dev -- -p {task.workspace_port}` or `python -m http.server {task.workspace_port}`"
                )
        elif getattr(task, "workspace_path", None):
            # Task has its own workspace (worktree, Bundle 4) but no project repo
            _ws_host = task.workspace_path
            _ws_view = workspace_path_for_runtime(agent, _ws_host) or _ws_host
            git_section = (
                f"**Working directory:** `{_ws_view}/`\n"
                f"Change into it FIRST: `cd {_ws_view}`\n\n"
                "**Git:** create a feature branch, never commit directly to main.\n"
                "Commit after every major step. Push to GitHub."
            )
            if task.workspace_port:
                git_section += (
                    f"\n**Dev server port:** {task.workspace_port} (use this port, NOT the default)\n"
                    f"Start the dev server: `npm run dev -- -p {task.workspace_port}` or `python -m http.server {task.workspace_port}`"
                )
        else:
            # Fallback: mc_repo_path config (applies only to tasks without a project and without a git repo)
            _mc_repo_path = getattr(settings, 'mc_repo_path', None)
            if _mc_repo_path:
                git_section = (
                    f"**Working directory (fallback):** `{_mc_repo_path}/`\n"
                    f"Change into it FIRST: `cd {_mc_repo_path}`\n\n"
                    "**Git:** create a feature branch, never commit directly to main.\n"
                    "Commit after every major step. Push to GitHub."
                )
            else:
                git_section = (
                    "**Git:** commit after every major step with a meaningful message. Push to GitHub.\n"
                    "Use a feature branch — never directly on main."
                )
            if task.workspace_port:
                git_section += (
                    f"\n**Dev server port:** {task.workspace_port} (use this port, NOT the default)\n"
                    f"Start the dev server: `npm run dev -- -p {task.workspace_port}` or `python -m http.server {task.workspace_port}`"
                )

        # Per-repo working rules (ADR-050): Mark-defined conventions for this
        # repo (test commands, branch policy, style). Attached to the git
        # section so they sit right next to the repo the agent works in.
        if git_section and getattr(ctx, "repo_rules_context", ""):
            git_section += (
                f"\n\n**Repository-Arbeitsregeln ({ctx.repo_rules_repo_name}) — BINDEND:**\n"
                f"{ctx.repo_rules_context}"
            )

        # Worker-Contract content (Task-Status truth, 5-Min-Blocker, Fokus-Regel,
        # Output-Location-Regel) moved to SOUL.md.j2 worker footer in Phase 1.
        # SOUL.md is loaded as --append-system-prompt at openclaude start and
        # reaches ALL non-lead worker roles persistently. Repeating it per-task
        # was redundant boilerplate that pushed messages over the HARD cap.

        # ── Checklist step (re-dispatch gate, 2026-07-08 incident fix) ──────
        # A re-dispatched agent (recovery context attached / task already
        # in_progress) that gets told to "create a checklist" verbatim
        # replays every `mc checklist add` call from its previous attempt,
        # duplicating rows — mirrors the ACK-reminder gate above. On
        # re-dispatch, point at the existing checklist (shown in the
        # recovery block) instead of re-seeding it.
        if task.status != "in_progress":
            _checklist_step = (
                '1. `mc checklist add "..."` for every step — the checklist is the single source\n'
                "   of truth for progress (recovery reads it)"
            )
        else:
            _checklist_step = (
                "1. Continue the EXISTING checklist shown above under Recovery — do NOT "
                "re-create it. Only `mc checklist add \"...\"` for newly discovered steps."
            )

        _add("worker_approach", f"""
## Approach

Work independently on this task until it's done:
{_checklist_step}
2. Work through each item, after completing it: `mc checklist done <id>`
3. In between: `mc comment progress "Update/Evidence/Next"` for the audit trail
4. When everything is done: `mc {'done' if _is_subtask else 'review'}`

Register a deliverable: `mc deliverable --title "..." --path /deliverables/{task.id}/<file>`  (absolute container path, ADR-022)

{git_section}

IMPORTANT: Do NOT stop before the task is at "{'done' if _is_subtask else 'review'}".""")

    # Add agent lessons section if available
    if _lessons_section:
        sections.append(_lessons_section)

    # ── Phase 3: live budget enforcement ────────────────────────────────
    # _assemble_with_budget drops optional sections in priority order until
    # the message fits under DISPATCH_HARD_CHARS, or logs at ERROR if even
    # mandatory-only content exceeds the cap. Dedup on (task_id, attempt_id)
    # prevents poll-cycle log spam (Bug 2026-05-12) — the dispatch message
    # is re-rendered on every /agent/me/poll call.
    _attempt_id = getattr(task, "dispatch_attempt_id", None)
    _dedup_key = (str(task.id), _attempt_id or "")
    _already_logged = _dedup_key in _SIZE_LOG_DEDUP

    if _already_logged:
        # Suppress logging by passing very-high thresholds so internal
        # warn/error logs don't fire. The drop logic itself still runs.
        rendered = _assemble_with_budget(
            sections,
            target=10**9,  # effectively disable target/warn/error logs
            warn=10**9,
            hard=DISPATCH_HARD_CHARS,
            task_id=task.id,
        )
    else:
        rendered = _assemble_with_budget(
            sections,
            target=DISPATCH_TARGET_CHARS,
            warn=DISPATCH_WARN_CHARS,
            hard=DISPATCH_HARD_CHARS,
            task_id=task.id,
        )
        _SIZE_LOG_DEDUP.add(_dedup_key)
        if len(_SIZE_LOG_DEDUP) > 256:
            for _stale in list(_SIZE_LOG_DEDUP)[:128]:
                _SIZE_LOG_DEDUP.discard(_stale)
    return rendered
