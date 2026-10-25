// Bench W1 API contract — TypeScript mirror of
// backend/app/verticals/bench_studio/w1_schemas.py (frozen 24.10.2026).
//
// CONTRACT RULE: change only together with the pydantic schema, the
// fixtures (build.py regenerates fixtures.ts) and the lead's OK — see
// ../CONTRACT.md. Responses carry every key (pydantic model_dump), so
// response fields are required here and nullable where the backend may
// send null. Request shapes (BenchTestCreate, ContestantPick, …) keep
// optional fields optional.

export type Harness = "api" | "omp" | "hermes" | "grok-build" | "claude-code";
/** The bench's own "no harness" marker: the model answers straight through
 *  its OpenAI-compatible API (class A). Not a CLI harness — the one place the
 *  W1 frontend names it; everything else asks `isDirectApi` (PRINCIPLES §3.4:
 *  no harness-name comparisons spread through the UI). */
export const DIRECT_API: Harness = "api";
export function isDirectApi(h: Harness | null | undefined): boolean {
  return h === DIRECT_API;
}
export type RunClass = "A" | "B" | "C";
export type Effort = "default" | "off" | "on" | "low" | "medium" | "high" | "max";
export type CostKind = "local" | "api_price" | "subscription" | "unknown";
export type Locality = "local" | "cloud";
export type PresetId = "standard" | "quick" | "effort_ladder" | "rerun";
export type Trigger = "runtimes" | "quick" | "template" | "rerun" | "auto_switch" | "list";
/** Human states (concept §3). */
export type BenchTestState = "waiting" | "running" | "done" | "done_missing" | "aborted";
/** List sections (P7): Läuft · Braucht Blick · Fertig. */
export type Section = "running" | "needs_look" | "done";
export type GateStatus = "green" | "amber" | "red" | "pending";
export type RunState = "waiting" | "running" | "done" | "failed";
export type RunPhase = "queued" | "generating" | "recording" | "composing" | "done";
export type DeviceState = "free" | "busy" | "asleep" | "offline";
export type ProbeStatus = "ok" | "only_on_off" | "no_effect" | "failed" | "unchecked";
export type WaitCode = "model_fresh" | "box_busy" | "model_loading" | "after_class_a" | "box_asleep";
export type BlockerCode =
  | "box_busy"
  | "model_fresh"
  | "box_asleep"
  | "engine_down"
  | "agent_busy"
  | "protocol_mismatch"
  | "no_api_key"
  | "no_model";
export type AbortCode =
  | "stopped_by_operator"
  | "task_deleted"
  | "superseded_by_switch"
  | "switch_failed"
  | "all_runs_failed"
  | "backend_restart";
export type GateRule = "R1" | "R2" | "R3" | "R4" | "R5" | "R6" | "amber";
export type KitFileName = "video.mp4" | "card.png" | "kit.zip";

/** A machine code + params — worded by the UI (i18n `benchShared`). */
export interface Reason {
  code: string;
  params: Record<string, unknown>;
}

export interface DurationRange {
  min_s: number | null;
  max_s: number | null;
  basis: "history" | "default" | "probe";
}

/** Money per D3/D4 — never a bare number (see w1_schemas.Money). */
export interface Money {
  cost_kind: CostKind;
  usd: number | null;
  hypothetical_usd: number | null;
  price_source: string | null;
  price_date: string | null;
  needs_confirmation: boolean;
  second_confirmation: boolean;
  reason: Reason | null;
}

/** Display names only — never IPs or hostnames. */
export interface Device {
  name: string;
  nodes: number | null;
  gpu_mode: string | null;
  state: DeviceState;
}

export interface Blocker {
  code: BlockerCode;
  /** false = the test starts and waits (`until` when known). */
  blocking: boolean;
  until: string | null;
  params: Record<string, unknown>;
}

export interface EffortOption {
  level: Effort;
  /** "hoch (Standard)": the server default merged into `merged_level`. */
  is_default: boolean;
  merged_level: Effort | null;
}

export interface EffortProbeView {
  status: ProbeStatus;
  options: EffortOption[];
  /** First probe after serving_since — the stamp "geprüft am 21.10.". */
  probed_at: string | null;
  default_observed: "on" | "off" | "unknown";
}

export interface HarnessOption {
  harness: Harness;
  run_class: RunClass;
  available: boolean;
  reason: Reason | null;
  /** D5: only the API path can set effort in W1. */
  effort_settable: boolean;
  effort_note: Reason | null;
  estimate: DurationRange;
  n_runs: number;
}

export interface LastTest {
  id: string;
  title: string;
  created_at: string;
}

export interface Contestant {
  key: string;
  runtime_id: string;
  served_model_id: string | null;
  /** Always the served model id verbatim (gate R2). */
  display_name: string;
  alias: string | null;
  locality: Locality;
  provider: string | null;
  device: Device | null;
  /** null = unknown — print "unknown", never guess. */
  engine: string | null;
  quant: string | null;
  recipe_slug: string | null;
  spec_decoding: string | null;
  serving_since: string | null;
  harnesses: HarnessOption[];
  effort: EffortProbeView;
  money: Money;
  blockers: Blocker[];
  /** N7: "gleicher Modellname · Cloud-Version unbekannt". */
  cloud_build_unknown: boolean;
  last_test: LastTest | null;
}

export interface ContestantList {
  local: Contestant[];
  cloud: Contestant[];
}

export interface PromptRef {
  template_id: string | null;
  title: string;
  sha256: string;
}

export interface PresetView {
  id: PresetId;
  n_runs: number;
  multi_effort: boolean;
  prompt: PromptRef;
}

/** Request: one contestant row of the sheet. */
export interface ContestantPick {
  runtime_id: string;
  harness?: Harness;
  /** One level; 2-3 only for the effort ladder. */
  efforts?: Effort[];
}

/** GET /api/v1/bench/plan */
export interface BenchPlan {
  preset: PresetView;
  presets: PresetView[];
  contestants: Contestant[];
  picks: Required<ContestantPick>[];
  estimate: DurationRange;
  money: Money;
  blockers: Blocker[];
  /** Mixed A + B: one after the other, two result blocks. */
  sequential: boolean;
  video_run_index: number;
  max_video_contestants: number;
}

/** POST /api/v1/bench/tests */
export interface BenchTestCreate {
  preset?: PresetId;
  prompt_template_id?: string | null;
  picks: ContestantPick[];
  trigger?: Trigger;
  rerun_of?: string | null;
  budget?: "same" | "larger";
  cost_confirmed_usd?: number | null;
  confirm_over_cap?: boolean;
  /** D4 fail-closed: "Kosten unbekannt — OK?" was confirmed. */
  confirm_unknown_cost?: boolean;
}

/** Body extra of POST /tests/{id}/rerun after a 409 CostConfirmation
 *  (w1_schemas.RerunRequest). */
export interface RerunCostConfirm {
  cost_confirmed_usd?: number | null;
  confirm_over_cap?: boolean;
  confirm_unknown_cost?: boolean;
}

/** 409 detail when money must be confirmed (D4: step 2 over $1). */
export interface CostConfirmation {
  code: "bench_cost_confirmation";
  step: 1 | 2;
  /** null with `price_unknown`: a cloud API without a known price. */
  usd: number | null;
  cap_usd: number;
  price_unknown: boolean;
}

/** Median + range; `measured: false` + `reason` = headline missing (K15). */
export interface Stat {
  measured: boolean;
  median: number | null;
  min: number | null;
  max: number | null;
  n: number;
  reason: Reason | null;
}

export interface EffortRecord {
  requested: Effort;
  /** The level the server default maps to ("hoch (Standard)"). */
  default_level: Effort | null;
  sent: Record<string, unknown> | null;
  observed: Record<string, unknown> | null;
}

export interface RunView {
  entry_id: string;
  repeat_idx: number;
  attempt_no: number;
  state: RunState;
  phase: RunPhase;
  elapsed_ms: number | null;
  tokens_out: number | null;
  /** Of tokens_out: thought, not written — when the engine says. */
  tokens_reasoning: number | null;
  decode_tok_s: number | null;
  ttft_ms: number | null;
  finish_reason: string | null;
  truncated: boolean;
  wait: Reason | null;
  error: Reason | null;
  is_video_run: boolean;
}

/** Class B has no tempo: decode_tok_s / ttft_ms are null — omit the row. */
export interface Summary {
  time: Stat;
  time_kind: "gen_ms" | "active_ms";
  wait_ms: number | null;
  tokens_out: Stat;
  tokens_reasoning: Stat | null;
  tokens_cache_read: number | null;
  decode_tok_s: Stat | null;
  ttft_ms: Stat | null;
  cost: Money;
  page_check: "loads" | "blank" | "unknown" | null;
  tie_with: string[];
}

/** Concept §7.2. `unknown` names each unknowable field and why. */
export interface Receipt {
  protocol_version: "v0" | "v1";
  run_class: RunClass;
  served_model_id: string | null;
  display_name: string | null;
  alias: string | null;
  locality: Locality | null;
  device: string | null;
  nodes: number | null;
  provider: string | null;
  engine: string | null;
  engine_version: string | null;
  recipe_slug: string | null;
  quant: string | null;
  gpu_mode: string | null;
  spec_decoding: string | null;
  harness: Harness | null;
  harness_version: string | null;
  effort_requested: Effort | null;
  effort_sent: Record<string, unknown> | null;
  effort_observed: Record<string, unknown> | null;
  thinking_key: string | null;
  temperature: number | null;
  max_tokens: number | null;
  system_prompt_sha: string | null;
  prompt_sha256: string | null;
  brief_sha: string | null;
  tools_allowed: boolean | null;
  time_limit_min: number | null;
  n_runs: number | null;
  video_run_index: number | null;
  attempts: number | null;
  failures: number | null;
  network_blocked: boolean | null;
  network_attempts: number | null;
  /** Of those, blocked scripts/modules — the page code could not run (gate R6). */
  network_blocked_scripts: number | null;
  libraries_inlined: string[];
  /** Lane lib:three-local: the recorder served MC's pinned three.js for the
   *  page's CDN request (null = recorded before the lane existed). */
  lib_track: LibTrack | null;
  libraries_provided: string[];
  /** Versions the page asked for ("r160"). */
  three_requested: string[] | null;
  three_version_mismatch: boolean | null;
  /** A bare `from 'three'` without an import map got MC's map. */
  three_import_map_injected: boolean | null;
  html_bytes: number | null;
  quiet_box: "ok" | "busy" | "timeout" | null;
  serving_age_min: number | null;
  recorded_at: string | null;
  harvested_at: string | null;
  unknown: Record<string, string>;
}

/** "three-local" = MC served its pinned three.js; never compared silently with "none". */
export type LibTrack = "three-local" | "none";

export interface ContestantResult {
  key: string;
  runtime_id: string | null;
  served_model_id: string | null;
  display_name: string;
  alias: string | null;
  locality: Locality | null;
  device_name: string | null;
  harness: Harness | null;
  effort: EffortRecord;
  runs: RunView[];
  summary: Summary;
  receipt: Receipt | null;
  lib_track: LibTrack;
  receipt_version: number | null;
  receipt_checksum: string | null;
}

/** One class — never mixed (§5.1). */
export interface ResultBlock {
  run_class: RunClass;
  contestants: ContestantResult[];
  starts_after: RunClass | null;
}

export interface GateReason {
  rule: GateRule;
  severity: "red" | "amber";
  code: string;
  params: Record<string, unknown>;
  fix: "harvest" | "rerun" | "rerun_larger" | null;
}

export interface Gate {
  status: GateStatus;
  reasons: GateReason[];
  checksum: string | null;
}

/** PRIVATE (operator decision 24.10.): runs cut at max_tokens. Never on public artefacts. */
export interface TruncationWarning {
  runs_cut: number;
  n_runs: number;
  max_tokens: number;
  suggested_max_tokens: number;
  entry_ids: string[];
  /** Runs that stopped before they wrote any page. */
  runs_without_page: number;
  /** Run 1 (the video run) was cut — the video shows an unfinished page. */
  video_run_cut: boolean;
}

export interface Verdict {
  served_model_id: string;
  run_class: RunClass;
  n_runs: number;
  time: Stat;
  tokens_out: Stat;
  page_check: "loads" | "blank" | "unknown" | null;
}

export interface VideoRef {
  /** Absolute /shared-deliverables/... path (files API via benchApi.sharedSubpath). */
  path: string;
  duration_s: number | null;
  run_index: number;
}

export interface OverlayAction {
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

/** Allowed action ids in BenchTestDetail.allowed. */
export type TestAction =
  | "stop"
  | "rerun"
  | "rerun_larger"
  | "harvest"
  | "kit"
  | "rerender"
  | "recompose"
  | "archive"
  | "delete";

/** GET /api/v1/bench/tests/{id} — result page AND live run. */
export interface BenchTestDetail {
  id: string;
  title: string;
  created_at: string;
  trigger: Trigger | null;
  preset: PresetId | null;
  protocol_version: "v0" | "v1";
  state: BenchTestState;
  section: Section;
  state_reason: Reason | null;
  prompt: PromptRef;
  blocks: ResultBlock[];
  gate: Gate;
  verdict: Verdict | null;
  video: VideoRef | null;
  truncation: TruncationWarning | null;
  rerun_of: string | null;
  allowed: string[];
  overlay_actions: OverlayAction[];
}

export interface BenchTestListContestant {
  display_name: string;
  harness: Harness | null;
  locality: Locality | null;
}

export interface BenchTestListItem {
  id: string;
  title: string;
  created_at: string;
  section: Section;
  state: BenchTestState;
  state_reason: Reason | null;
  run_class: RunClass | null;
  contestants: BenchTestListContestant[];
  progress: Record<string, number> | null;
  gate_status: GateStatus;
  /** A page screenshot exists: GET /bench/tests/{id}/poster (gallery). */
  poster: boolean;
}

/** GET /api/v1/bench/tests */
export interface BenchTestList {
  items: BenchTestListItem[];
  counts: Record<Section, number>;
  total: number;
  last_at: string | null;
}

export interface KitFile {
  name: KitFileName;
  ready: boolean;
  bytes: number | null;
  width: number | null;
  height: number | null;
  duration_s: number | null;
}

/** English public texts (decision 24.10.). */
export interface KitTexts {
  settings: string[];
  alt_text: string;
  facts: string[];
  link: string | null;
  thread_guide: string[];
}

/** GET /api/v1/bench/tests/{id}/kit */
export interface XKit {
  test_id: string;
  gate: Gate;
  downloadable: boolean;
  blocked_reason: Reason | null;
  files: KitFile[];
  texts: KitTexts;
  footer: string;
  truncation: TruncationWarning | null;
}

export interface DownloadToken {
  token: string;
  expires_in: number;
}

/** GET /api/v1/bench/runtime-summary — Runtimes cards (P1). */
export interface RuntimeTestSummary {
  runtime_id: string;
  testable: boolean;
  reason: Reason | null;
  /** Only the most recently started live card is primary (K11). */
  primary: boolean;
  last_test: LastTest | null;
  running_test_id: string | null;
  /** An auto test after a recipe switch that waits for the model. */
  planned_test: PlannedTest | null;
}

export interface PlannedTest {
  id: string;
  not_before: string | null;
}

/** POST /api/v1/bench/tests/{id}/seen */
export interface SeenResponse {
  resolved: number;
}

/** POST /api/v1/bench/auto-tests — request (P1b). */
export interface AutoTestCreate {
  host_id: string;
  recipe_slug: string;
}

export interface AutoTestResponse {
  test_id: string;
  state: BenchTestState;
  not_before: string | null;
  superseded_test_id: string | null;
}
