// Vertical-owned legacy types (ADR-044 §4) — NOT in src/lib/types.ts, so the
// core type surface stays clean when the vertical is stripped. Trimmed in
// bench W1 (package D): the create/draft/spark-model/effort-probe request
// shapes went with the old dialog; the W1 shapes live in w1/types.ts.

export interface BenchEntry {
  id: string;
  challenge_id: string;
  model_label: string;
  // Row index from the create dialog (0-based); the API already returns the
  // entries in this order (round-2 user test t2 SOLL-1), so the UI keeps it.
  position?: number;
  source_kind: "spark" | "agent";
  spark_model: string | null;
  agent_id: string | null;
  // Custom chip tag in the branded video frame; null = harness-derived default.
  display_tag: string | null;
  task_id: string | null;
  status: "pending" | "generating" | "generated" | "rendered" | "failed";
  artifact_path: string | null;
  video_path: string | null;
  screenshot_path: string | null;
  metrics: {
    // Effort record (bench W0.6/W0.7) — see EffortRecord.
    effort_requested?: EffortLevel;
    effort_sent?: { key: string; value: string | boolean } | null;
    effort_observed?: EffortObserved;
    thinking_key?: string | null;
    finish_reason?: string | null;
    truncated?: boolean;
    max_tokens?: number;
    duration_ms?: number;
    tokens_in?: number;
    tokens_out?: number;
    tok_per_s?: number;
    // Agent entries: attributed usage snapshot the video outro uses
    // (backend orchestrator.snapshot_entry_usage). cost_usd is the API
    // list price — not spent money when locality is "local".
    cost_usd?: number;
    locality?: "local" | "cloud";
  };
  error: string | null;
  // Detail endpoint only: derived view of the fleet task behind a running
  // agent entry (backend orchestrator.entry_task_states) — null otherwise.
  task_state?: BenchEntryTaskState | null;
}

export interface BenchEntryTaskState {
  task_id: string;
  task_status: string;
  // Blocker question while the task is blocked (approval → triage → comment).
  blocker_reason: string | null;
  blocked_at: string | null;
  // Last server-observed sign of life (same evidence as the silent-card watchdog).
  last_activity_at: string | null;
}

// W0.9 recorder report stored in entry.metrics (absent on recordings made
// before the network block). Kept as its own type so parallel bench PRs that
// extend `metrics` do not collide.
export interface RecordNetworkMetrics {
  network_blocked?: boolean;
  network_attempts?: number;
  libraries_inlined?: string[];
  html_bytes?: number;
  recorded_at?: string;
}

export type BenchChallengeStatus =
  | "generating"
  | "rendering"
  | "composing"
  | "review"
  | "drafted"
  | "published"
  | "failed";

// Extension point (ADR-044): action buttons contributed by an overlay
// vertical (e.g. a private catalog_publisher) via the
// challenge_actions_providers hook. Public build sees an empty/absent
// array — nothing renders.
export interface ChallengeAction {
  id: string;
  label: string;
  style: "default" | "primary" | "danger";
  method: "POST";
  endpoint: string;
  confirm: string | null;
  disabled: boolean;
  disabled_reason: string | null;
  busy: boolean;
}

export interface BenchChallenge {
  id: string;
  title: string;
  prompt_template_id: string | null;
  prompt_text: string;
  mode: "single" | "side_by_side";
  status: BenchChallengeStatus;
  series_label: string | null;
  series_no: number | null;
  // Video length in seconds; null = legacy 10s default (orchestrator.py).
  record_duration_s: number | null;
  composed_video_path: string | null;
  content_pipeline_id: string | null;
  error: string | null;
  // Operator archive (soft-hide) — list hides archived unless requested.
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  entries: BenchEntry[];
  actions?: ChallengeAction[];
}

// Effort (thinking level) — backend services/reasoning_params.EFFORT_LEVELS.
export type EffortLevel = "default" | "off" | "on" | "low" | "medium" | "high" | "max";

// What a run showed about thinking. API path: from the engine response;
// agent path: the thinking level of the task's own omp session.
export type EffortObserved =
  | { thinking: "on" | "off"; reasoning_tokens: number | null; reasoning_chars: number }
  | { source: "omp_session"; level: string; configured: string | null; changed: boolean; attempts: number }
  | { source: "unknown"; reason: string };

export interface PromptTemplate {
  id: string;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}
