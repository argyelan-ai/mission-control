// Vertical-owned types (ADR-044 §4) — NOT in src/lib/types.ts, so the core
// type surface stays clean when the vertical is stripped.

export interface BenchEntry {
  id: string;
  challenge_id: string;
  model_label: string;
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

export interface BenchModelSpec {
  label: string;
  source_kind: "spark" | "agent";
  spark_model?: string | null;
  agent_id?: string | null;
  // Custom chip tag for the branded video (e.g. "OMP · DGX SPARK");
  // empty/undefined = harness-derived default (backend).
  display_tag?: string | null;
  // Direct-API rows only: a level the effort probe found for the served
  // model; null/undefined = "default" (nothing sent). Agent rows: never set.
  effort?: EffortLevel | null;
}

// Effort (thinking level) — backend services/reasoning_params.EFFORT_LEVELS.
export type EffortLevel = "default" | "off" | "on" | "low" | "medium" | "high" | "max";

// What a run showed about thinking. API path: from the engine response;
// agent path: the thinking level of the task's own omp session.
export type EffortObserved =
  | { thinking: "on" | "off"; reasoning_tokens: number | null; reasoning_chars: number }
  | { source: "omp_session"; level: string; configured: string | null; changed: boolean; attempts: number }
  | { source: "unknown"; reason: string };

// GET /bench/effort-levels — the effort probe for one served model.
export interface EffortLevels {
  served_model_id: string | null;
  reachable: boolean;
  // ok: levels found · no_effect: no key changed thinking · failed: the probe could not run
  status: "ok" | "no_effect" | "failed";
  thinking_key: string | null;
  effort_param: string | null;
  levels: EffortLevel[];
  default_observed: "on" | "off" | "unknown";
  probed_at: string | null;
  serving_since: string | null;
}

export interface BenchChallengeCreate {
  title: string;
  prompt_template_id?: string | null;
  prompt_text?: string | null;
  mode: "single" | "side_by_side";
  models: BenchModelSpec[];
  series_label?: string | null;
  // Video length in seconds (5..60); omitted/null = legacy 10s default.
  record_duration_s?: number | null;
}

// Bench #21: live Spark vLLM model list for the vanilla (direct-API) row.
export interface SparkModelsStatus {
  reachable: boolean;
  models: string[];
  active: string | null;
}

export interface PromptTemplate {
  id: string;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}
