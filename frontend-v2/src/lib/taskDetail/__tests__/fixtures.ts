// Shared fixtures for the task-detail tests (state card, summary, URL tab).
// Neutral example names only — this repo is public.
import type { Agent, Approval, RunRecord, Task, TaskComment } from "@/lib/types";

export function taskFixture(overrides: Partial<Task> = {}): Task {
  return {
    id: "task-1",
    board_id: "board-1",
    project_id: null,
    phase_id: null,
    parent_task_id: null,
    title: "Sample task",
    description: null,
    status: "inbox",
    priority: "medium",
    task_type: "story",
    assigned_agent_id: null,
    started_at: null,
    completed_at: null,
    due_at: null,
    sort_order: 0,
    is_auto_created: false,
    auto_reason: null,
    pipeline_id: null,
    pipeline_stage: null,
    owner_agent_id: null,
    delegation_type: null,
    branch_name: null,
    triggered_by_deliverable_id: null,
    target_url: null,
    acceptance_criteria: null,
    requires_auth: false,
    source_task_id: null,
    report_back_required: false,
    report_back_status: null,
    review_decision: null,
    review_decided_at: null,
    dispatch_phase: null,
    intake_mode: null,
    request_kind: null,
    desired_output: null,
    scope_out: null,
    risk_notes: null,
    reference_urls: null,
    reference_notes: null,
    approval_policy: null,
    autonomy_level: null,
    publish_allowed: null,
    needs_browser: null,
    use_separate_repo: false,
    repo_id: null,
    credential_consent: null,
    credential_id: null,
    planner_mode: "auto",
    run_control: null,
    dispatch_intent: "root",
    dispatch_attempt_id: null,
    spawn_session_key: null,
    spawn_run_id: null,
    workspace_port: null,
    workspace_path: null,
    checklist_total: 0,
    checklist_done: 0,
    dispatched_at: null,
    ack_at: null,
    last_activity_at: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    created_by_user_id: null,
    ...overrides,
  };
}

export function agentFixture(overrides: Partial<Agent> = {}): Agent {
  return {
    id: "agent-1",
    name: "alpha",
    emoji: null,
    status: "online",
    ...overrides,
  } as Agent;
}

export function approvalFixture(overrides: Partial<Approval> = {}): Approval {
  return {
    id: "ap-1",
    board_id: "board-1",
    task_id: "task-1",
    agent_id: "agent-1",
    action_type: "blocker_decision",
    description: "Needs a decision",
    payload: null,
    confidence: null,
    status: "pending",
    autonomy_level: null,
    resolved_at: null,
    resolver_note: null,
    failure_reason: null,
    expires_at: null,
    created_at: "2026-09-21T08:00:00Z",
    ...overrides,
  };
}

export function commentFixture(overrides: Partial<TaskComment> = {}): TaskComment {
  return {
    id: "c1",
    task_id: "task-1",
    author_type: "agent",
    author_agent_id: "agent-1",
    author_agent_name: "alpha",
    author_agent_emoji: null,
    comment_type: "progress",
    content: "A comment",
    created_at: "2026-09-21T09:00:00Z",
    ...overrides,
  };
}

export function runRecordFixture(overrides: Partial<RunRecord> = {}): RunRecord {
  return {
    auftrag: {
      task_id: "task-1",
      board_id: "board-1",
      titel: "Sample task",
      beschreibung: "Fix the sideways scroll in chat.",
      status: "done",
      kinder: { total: 0, by_status: {}, items: [] },
    },
    zeiten: {
      erstellt: "2026-09-18T08:00:00",
      dispatched: "2026-09-18T08:05:00",
      bestaetigt: "2026-09-18T08:06:00",
      abgeschlossen: "2026-09-21T10:00:00",
      dauer_sekunden: 3 * 86400 + 2 * 3600,
    },
    plan: [],
    schritte: [
      { ts: "2026-09-18T08:05:00", quelle: "status", actor_label: "alpha", changed_by: "agent", text: "inbox -> in_progress" },
    ],
    beweise: {
      anzahl: 3,
      nach_typ: { file: 2, screenshot: 1 },
      items: [],
    },
    kosten: {
      gesamt_usd: 0.05,
      je_anbieter: { "local-openai": { usd: 0.05, input_tokens: 1000, output_tokens: 200 } },
      kinder_anteil_usd: 0,
      hinweis: "Claude-Kosten nicht zugeordnet (keine session-Verknuepfung).",
    },
    entscheidungen: [
      { ts: "2026-09-20T08:00:00", typ: "blocker_decision", status: "offen", description: "Flip to review?", resolver_note: null },
      { ts: "2026-09-19T08:00:00", typ: "visual_review", status: "approved", description: "Looks fine", resolver_note: null },
      { ts: "2026-09-19T09:00:00", typ: "visual_review", status: "approved", description: "Second look", resolver_note: null },
    ],
    reibung: {
      "blocker.escalated_to_operator": { anzahl: 2, erste: "2026-09-19T08:00:00", letzte: "2026-09-20T08:00:00" },
      review_stuck: { anzahl: 1, erste: "2026-09-20T09:00:00", letzte: "2026-09-20T09:00:00" },
    },
    ...overrides,
  };
}
