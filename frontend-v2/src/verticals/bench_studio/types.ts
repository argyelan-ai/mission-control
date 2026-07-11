// Vertical-owned types (ADR-044 §4) — NOT in src/lib/types.ts, so the core
// type surface stays clean when the vertical is stripped.

export interface BenchEntry {
  id: string;
  challenge_id: string;
  model_label: string;
  source_kind: "spark" | "agent";
  spark_model: string | null;
  agent_id: string | null;
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

export interface BenchChallenge {
  id: string;
  title: string;
  prompt_template_id: string | null;
  prompt_text: string;
  mode: "single" | "side_by_side";
  status: BenchChallengeStatus;
  series_label: string | null;
  series_no: number | null;
  composed_video_path: string | null;
  content_pipeline_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  entries: BenchEntry[];
}

export interface BenchModelSpec {
  label: string;
  source_kind: "spark" | "agent";
  spark_model?: string | null;
  agent_id?: string | null;
}

export interface BenchChallengeCreate {
  title: string;
  prompt_template_id?: string | null;
  prompt_text?: string | null;
  mode: "single" | "side_by_side";
  models: BenchModelSpec[];
  series_label?: string | null;
}

export interface PromptTemplate {
  id: string;
  title: string;
  body: string;
  tags: string[];
  created_at: string;
  updated_at: string;
}
