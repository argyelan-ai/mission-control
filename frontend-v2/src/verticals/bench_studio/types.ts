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
    duration_ms?: number;
    tokens_in?: number;
    tokens_out?: number;
    tok_per_s?: number;
  };
  error: string | null;
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

export interface PromptTemplate {
  id: string;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}
