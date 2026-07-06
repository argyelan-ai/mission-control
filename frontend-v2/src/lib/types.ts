// ── Core Entities ──────────────────────────────────────────────────────────────

export type AgentStatus = "online" | "offline" | "busy" | "idle" | "error" | "provisioning" | "restarting" | "archived";
export type TaskStatus = "inbox" | "in_progress" | "review" | "user_test" | "done" | "blocked" | "failed" | "aborted";
export type Priority = "low" | "medium" | "high" | "critical";
export type Severity = "info" | "warning" | "error" | "critical";
export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";
export type MemoryType = "knowledge" | "lesson" | "reference" | "journal" | "weekly_review" | "research" | "insight";
export type ReviewDecision = "approved" | "changes_requested" | "hold";

export interface BoardGroup {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  icon: string | null;
  color: string | null;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface Board {
  id: string;
  board_group_id: string | null;
  name: string;
  slug: string;
  description: string | null;
  icon: string | null;
  color: string | null;
  require_approval_for_done: boolean;
  require_review_before_done: boolean;
  only_lead_can_change_status: boolean;
  auto_dispatch_enabled: boolean;
  objective: string | null;
  target_date: string | null;
  stats_cache: BoardStats | null;
  sort_order: number;
  is_archived: boolean;
  created_at: string;
  updated_at: string;
}

export interface BoardStats {
  tasks_total?: number;
  tasks_active?: number;
  agents_online?: number;
  agents_total?: number;
}

// ── Project Phase ──────────────────────────────────────────────────────────

export type ProjectPhaseStatus =
  | "pending"
  | "active"
  | "completed"
  | "blocked"
  | "awaiting_approval";

export type PhaseFailurePolicy = "retry" | "halt" | "skip";

export interface ProjectPhase {
  id: string;
  project_id: string;
  title: string;
  order: number;
  status: ProjectPhaseStatus;
  depends_on_phases: string[] | null;
  gate_required: boolean;
  failure_policy: PhaseFailurePolicy;
  default_agent_id: string | null;
  git_branch: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface Project {
  id: string;
  board_id: string;
  name: string;
  description: string | null;
  project_type: ProjectType;
  status: ProjectStatus;
  priority: Priority;
  plan_summary: string | null;
  progress_pct: number;
  github_repo_url: string | null;
  github_repo_name: string | null;
  workspace_path: string | null;
  project_config: ProjectConfig | null;
  created_by: string;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  // Project System Extensions
  briefing_doc?: string | null;
  parent_project_id?: string | null;
  last_active_phase_id?: string | null;
  resume_briefing?: string | null;
}

export interface ProjectConfig {
  stack?: "node" | "python" | string;
  framework?: string;
  source_dir?: string;
  source_dirs?: string[];
  dev_command?: string;
  test_command?: string;
  build_command?: string;
  notes?: string;
  has_docker?: boolean;
  review_policy?: "always" | "browser_only" | "never";
  test_policy?: string;
}

export interface ProjectGitInfo {
  has_repo: boolean;
  repo_name: string | null;
  repo_url: string | null;
  branches: string[];
  // ADR-052: Registry-Anbindung fuer die Task-Maske (Rules-Badge + Link)
  repo_id: string | null;
  has_rules: boolean;
}

export interface TaskChecklistItem {
  id: string;
  task_id: string;
  agent_id: string | null;
  title: string;
  status: "pending" | "done" | "blocked" | "skipped";
  sort_order: number;
  completed_at: string | null;
  created_at: string;
}

export interface TaskGitCommit {
  hash: string;
  message: string;
  author: string;
  date: string;
}

export interface TaskGitInfo {
  branch: string | null;
  last_commit: string | null;
  uncommitted: boolean;
  ahead: number;
  workspace_path: string | null;
  commits?: TaskGitCommit[];
  pr_url?: string | null;
  repo_url?: string | null;
  repo_name?: string | null;
}

export interface CommitDiffLine {
  type: "add" | "del" | "ctx";
  content: string;
  old_no: number | null;
  new_no: number | null;
}

export interface CommitDiffHunk {
  header: string;
  lines: CommitDiffLine[];
}

export interface CommitDiffFile {
  filename: string;
  additions: number;
  deletions: number;
  hunks: CommitDiffHunk[];
}

export interface CommitDiff {
  hash: string;
  message: string;
  author: string;
  date: string;
  stats: { files: number; additions: number; deletions: number };
  files: CommitDiffFile[];
}

export interface Task {
  id: string;
  board_id: string;
  project_id: string | null;
  phase_id: string | null;
  parent_task_id: string | null;
  title: string;
  description: string | null;
  status: TaskStatus;
  priority: Priority;
  task_type: "story" | "bug" | "revision" | "chore";
  assigned_agent_id: string | null;
  started_at: string | null;
  completed_at: string | null;
  due_at: string | null;
  sort_order: number;
  is_auto_created: boolean;
  auto_reason: string | null;
  pipeline_id: string | null;
  pipeline_stage: string | null;
  // Ownership
  owner_agent_id: string | null;
  // Delegation Contract
  delegation_type: "code_change" | "visual_proof" | "credential_bound" | "review" | null;
  branch_name: string | null;
  triggered_by_deliverable_id: string | null;
  target_url: string | null;
  acceptance_criteria: string | null;
  requires_auth: boolean;
  source_task_id: string | null;
  // Report-Back
  report_back_required: boolean;
  report_back_status: "none" | "pending" | "sent" | "fallback_sent" | "failed" | null;
  // Review Decision
  review_decision: ReviewDecision | null;
  review_decided_at: string | null;
  // Pre-Dispatch Gating
  dispatch_phase: "planning" | "ready" | null;
  // Operator-Intake (Phase 2)
  intake_mode: "quick" | "structured" | null;
  request_kind: "code_change" | "content_create" | "research" | "browser_task" | "credential_task" | "mixed" | null;
  desired_output: string | null;
  scope_out: string | null;
  risk_notes: string | null;
  reference_urls: string[] | null;
  reference_notes: string | null;
  approval_policy: "never" | "on_plan" | "on_execution" | "on_publish" | "on_sensitive_action" | "always" | null;
  autonomy_level: "advise_only" | "draft_only" | "execute_low_risk" | "execute_with_approval_on_risk" | "manual_dispatch_required" | null;
  publish_allowed: boolean | null;
  needs_browser: boolean | null;
  e2e_test_required?: boolean | null;
  human_review_required?: boolean | null;
  use_separate_repo: boolean;
  repo_id: string | null;
  credential_consent: boolean | null;
  credential_id: string | null;
  // Planner Mode
  planner_mode: "auto" | "with_planner" | "direct";
  // Operational Controls
  run_control: "stopped" | "manual_hold" | null;
  dispatch_intent: "root" | "subtask" | "review_handoff" | "review_rework" | "manual_redispatch";
  dispatch_attempt_id: string | null;
  // Spawn Tracking
  spawn_session_key: string | null;
  spawn_run_id: string | null;
  workspace_port: number | null;
  workspace_path: string | null;
  checklist_total: number;
  checklist_done: number;
  dispatched_at: string | null;
  ack_at: string | null;
  last_activity_at: string | null;
  created_at: string;
  updated_at: string;
  // Creator
  created_by_user_id: string | null;
}

// ── Task Deliverables ──────────────────────────────────────────────────────

export type DeliverableScope = "task" | "phase" | "project";

export interface TaskDeliverable {
  id: string;
  task_id: string;
  agent_id: string;
  agent_name?: string;
  deliverable_type: "screenshot" | "file" | "url" | "artifact" | "document" | "data" | "video";
  title: string;
  path: string | null;
  description: string | null;
  // V2 fields
  content?: string | null;
  scope?: DeliverableScope;
  tags?: string[] | null;
  is_pinned?: boolean;
  is_reusable?: boolean;
  git_commit_hash?: string | null;
  created_at: string;
  // Only set when LIST was called with ?include_subtasks=true.
  // Allows UI grouping by source task without an extra query.
  source_task_id?: string;
  source_task_title?: string;
  source_depth?: number; // 0 = self, 1 = direct child, 2 = grandchild, ...
}

export interface DeliverableReference {
  id: string;
  source_deliverable_id: string;
  target_project_id: string;
  created_at: string;
}

export interface DeliverableDirectoryEntry {
  name: string;
  type: "file" | "directory";
  size: number | null;
}

export interface DeliverableDirectory {
  root_path: string;
  current_path: string;
  entries: DeliverableDirectoryEntry[];
}

// ── Reference Files (ADR-054) ────────────────────────────────────────────────
// Operator-uploaded example/asset files for tasks & projects. Agents read
// them directly — `abs_path` is baked into the dispatch directive.

export interface ReferenceFile {
  id: string;
  board_id: string;
  task_id: string | null;
  project_id: string | null;
  rel_path: string;
  original_name: string;
  mime: string;
  size: number;
  note: string | null;
  created_at: string;
  abs_path: string;
  /** Only set by GET /references?task_id=X — true when inherited from the task's project. */
  inherited?: boolean;
}

// ── Files (global filesystem browser, /api/v1/files/*) ─────────────────────
// Backend contract: deliverables, workspaces, vault, etc. as browsable roots.
// `native_open` (per root) + `native_open_available` (per response/meta) gate
// the macOS "Im Finder" affordance; on mobile/remote it is simply absent.

export interface FsRoot {
  key: string;
  label: string;
  icon: string;
  native_open: boolean;
  indexed_count: number;
  deletable: boolean;
}

export interface FsEntry {
  name: string;
  type: "file" | "directory";
  size: number;
  mime: string | null;
  mtime: number;
  is_directory: boolean;
}

// ── Task Workspace (read-only browser over task.workspace_path) ────────────

export interface TaskWorkspaceListing {
  exists: boolean;
  subpath: string;
  entries: FsEntry[];
}

export interface FsMeta {
  root: string;
  subpath: string;
  name: string;
  type: string;
  size: number;
  mime: string | null;
  mtime: number;
  is_directory: boolean;
  reachable: boolean;
  native_open_available: boolean;
  task_id: string | null;
  deliverable_id: string | null;
  agent_slug: string | null;
}

export interface FsSearchResult {
  root: string;
  rel_path: string;
  name: string;
  size: number;
  mime: string | null;
  mtime: number;
  agent_slug: string | null;
  task_id: string | null;
}

/** A single file sitting in the trash (~/.mc/.trash). `trash_id` is the
 *  `.trash`-relative `<ts>/<root_key>/<rel>` string — the stable selection key
 *  (analogous to FsBrowser keying off subpaths). `deleted_at` is derived from
 *  the timestamp directory and groups entries in the trash view. */
export interface TrashEntry {
  trash_id: string;
  original_root: string;
  original_subpath: string;
  name: string;
  size: number;
  mtime: number;
  deleted_at: string;
}

export type SystemMode = "active" | "draining" | "halted";
export type RunControl = "stopped" | "manual_hold" | null;
export type DispatchIntent = "root" | "subtask" | "review_handoff" | "review_rework" | "manual_redispatch";
export type AgentOperationalMode = "active" | "paused";

export interface SystemModeMeta {
  mode: SystemMode;
  previous_mode: SystemMode | null;
  changed_by: string | null;
  changed_at: string | null;
  reason: string;
}

export interface TaskComment {
  id: string;
  task_id: string;
  author_type: "user" | "agent" | "system";
  author_agent_id: string | null;
  author_agent_name: string | null;
  author_agent_emoji: string | null;
  comment_type?: string;
  content: string;
  created_at: string;
}

export interface TaskDependencyInfo {
  task_id: string;
  title: string;
  status: TaskStatus;
}

export interface TaskEvent {
  id: string;
  task_id: string;
  board_id?: string;
  agent_id: string | null;
  agent_name?: string | null;
  event_type?: string;
  title?: string;
  from_status: TaskStatus | string;
  to_status: TaskStatus | string;
  changed_by: "user" | "agent" | "watchdog" | "system" | string;
  metadata?: Record<string, unknown> | null;
  reason?: string | null;
  created_at: string;
}

// ── Pipeline ──────────────────────────────────────────────────────────────────

export interface PipelineTask {
  id: string;
  title: string;
  priority: Priority;
  parent_task_id: string | null;
  agent: { name: string; emoji: string } | null;
  has_blocked_deps: boolean;
  tags?: { name: string; color: string | null }[];
  review_decision?: ReviewDecision | null;
  dispatch_phase?: "planning" | "ready" | null;
}

export interface TaskPipelineResponse {
  pipeline: {
    inbox: PipelineTask[];
    in_progress: PipelineTask[];
    review: PipelineTask[];
    user_test: PipelineTask[];
    blocked: PipelineTask[];
    failed: PipelineTask[];
    aborted: PipelineTask[];
  };
  done_count: number;
  failed_count: number;
}

export type ProvisionStatus = "local" | "provisioning" | "provisioned" | "error" | "offline";

/**
 * Task transcript entry as returned by GET /tasks/{id}/transcript. Since the
 * Phase 29 Gateway removal, the transcript is reconstructed from TaskComment
 * rows (runtime-agnostic) rather than Anthropic chat-history blocks — plain
 * `content` text, not `parts`.
 */
export interface TranscriptMessage {
  role: string;
  content: string;
  ts: string | null;
  comment_type: string;
}

// Harness/Provider-Decoupling (ADR-056) — the CLI harness driving an agent's
// session, independent of the LLM runtime/protocol behind it.
export type Harness = "claude" | "openclaude" | "omp";

export const HARNESS_LABELS: Record<Harness, string> = {
  claude: "Claude Code",
  openclaude: "OpenClaude",
  omp: "omp",
};

export interface Agent {
  id: string;
  board_id: string | null;
  name: string;
  role: string | null;
  emoji: string | null;
  status: AgentStatus;
  model: string | null;
  secret_id: string | null;  // Per-Agent API-Key override (FK to secrets.id)
  is_board_lead: boolean;
  heartbeat_config: { interval: string; target: string };
  skills: string[];
  skill_filter: string[] | null;
  cli_plugins: string[] | null;
  cli_skills: string[] | null;
  mcp_servers: string[] | null;
  scopes: string[];
  identity_md: string | null;
  soul_md: string | null;
  tools_md: string | null;
  heartbeat_md: string | null;
  rules_md: string | null;
  memory_md: string | null;
  last_seen_at: string | null;
  last_task_activity_at: string | null;
  current_task_id: string | null;
  context_tokens: number;
  context_max: number;
  session_message_count: number;
  total_tasks_completed: number;
  total_compactions: number;
  template_id: string | null;
  // Agent Council fields
  /**
   * Agent home path on host (~/.mc/workspaces/{slug} for cli-bridge;
   * ~/.openclaw/agents/{slug} for legacy host). Phase 14 / ADR-022 repurpose;
   * NOT a Gateway VPS path despite legacy SQLModel docstring. Phase 30 D-12.
   */
  workspace_path: string | null;
  provision_status: ProvisionStatus;
  provisioned_at: string | null;
  discord_channel_id: string | null;
  discord_channel_name: string | null;
  // Runtime observability
  last_trigger_at: string | null;
  last_dispatch_error: string | null;
  run_state: "idle" | "running" | "recovering" | "aborted" | "blocked";
  operational_mode: AgentOperationalMode;
  agent_runtime: "openclaw" | "claude-code" | "manual" | "cli-bridge" | "free-code-bridge" | "host";
  // Per-agent runtime selection (cli-bridge only — Phase 2). NULL means
  // the agent falls back to docker-compose env defaults.
  runtime_id: string | null;
  pending_runtime_sync: boolean;
  // Harness/Provider-Decoupling (ADR-056) — explicit CLI harness override.
  // NULL means the harness is derived from the runtime's protocol.
  harness?: Harness | null;
  created_at: string;
  updated_at: string;
}

export interface AgentTemplate {
  id: string;
  name: string;
  emoji: string;
  role: string | null;
  default_model: string | null;
  soul_md: string | null;
  skills: string[];
  skill_filter: string[] | null;
  cli_plugins: string[] | null;
  scopes: string[];
  is_builtin: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentMetrics {
  id: string;
  agent_id: string;
  period_start: string;
  period_end: string;
  tasks_started: number;
  tasks_completed: number;
  comments_posted: number;
  context_tokens_avg: number;
  context_tokens_max: number;
  heartbeats_total: number;
  heartbeats_failed: number;
  errors_total: number;
  compactions: number;
  resets: number;
  avg_task_duration_minutes: number | null;
  idle_minutes: number;
}

// Phase 5 MSY-03: attachment metadata stored in BoardMemory.attachments JSON.
// path is relative to ~/.mc/attachments/ (e.g. "{board_id}/{memory_id}/{filename}");
// backend resolves absolute via HOME_HOST env-var (see RESEARCH.md Pattern 2).
export interface BoardMemoryAttachment {
  path: string;
  mime_type: string;
  size_bytes: number;
  original_name: string;
}

export interface BoardMemory {
  id: string;
  board_id: string | null;
  agent_id: string | null;
  title: string | null;
  content: string;
  tags: string[];
  source: string;
  memory_type: MemoryType;
  is_pinned: boolean;
  auto_generated: boolean;
  linked_ids: string[];
  created_at: string;
  updated_at: string;
  // Phase 5 MSY-02 (dedup) — both nullable, set by Plan 05-05 dedup logic:
  content_hash?: string | null;
  merge_candidate_id?: string | null;
  // Phase 5 MSY-03 (attachments) — nullable; populated by Plan 05-06 routes:
  attachments?: BoardMemoryAttachment[] | null;
}

export type AutonomyLevel = "L1" | "L2" | "L3";

export interface Approval {
  id: string;
  board_id: string;
  task_id: string | null;
  agent_id: string;
  action_type: string;
  description: string;
  payload: Record<string, unknown> | null;
  confidence: number | null;
  status: ApprovalStatus;
  autonomy_level: AutonomyLevel | null;
  resolved_at: string | null;
  resolver_note: string | null;
  failure_reason: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface VisualReviewPayload {
  screenshots: string[];
  preview_url?: string;
}

export interface ActivityEvent {
  id: string;
  event_type: string;
  board_id: string | null;
  task_id: string | null;
  agent_id: string | null;
  project_id: string | null;
  title: string;
  detail: Record<string, unknown> | null;
  severity: Severity;
  created_at: string;
}

// Phase 31 / OCS-15: Gateway, OpenClawHealth, OpenClawModel,
// OpenClawSyncResult, GatewaySession interfaces removed (Phase 29+30
// deleted the backing tables and routes). DiscordChannel kept — used by
// the new /api/v1/discord/* router (Plan 29-01).

export interface DiscordChannel {
  id: string;
  name: string;
  context: string;
  bound_agent_id?: string;
}

export interface Tag {
  id: string;
  name: string;
  slug: string;
  color: string | null;
  created_at: string;
}

export interface MetricsSnapshot {
  ts: string;
  cpu_pct: number;
  memory_pct: number;
  memory_used_gb: number;
  memory_total_gb: number;
  disk_pct: number;
  disk_used_gb: number;
  disk_total_gb: number;
  db_latency_ms: number | null;
  redis_latency_ms: number | null;
}

export interface MetricsHistoryResponse {
  snapshots: MetricsSnapshot[];
  count: number;
}

export interface SystemStatus {
  status: "healthy" | "degraded" | "down";
  components: {
    database: { status: string; latency_ms?: number; error?: string };
    redis: { status: string; latency_ms?: number; error?: string };
    gateway: { status: string; url?: string; error?: string };
    watchdog: { status: string; last_check?: string | null; checks_total?: number };
  };
  resources: MetricsSnapshot | null;
  agents: { total: number; online: number; offline: number };
  uptime_seconds: number;
  version: string;
}

export interface SystemMetrics {
  tasks: { total: number; active: number };
  agents: { total: number; online: number };
  approvals: { pending: number };
}

// ── Intelligence ──────────────────────────────────────────────────────────────

export interface IntelligenceTaskDurations {
  avg_minutes: number;
  total: number;
  outliers: { task_id: string; title: string; agent_id: string | null; minutes: number; agent?: string }[];
  per_agent: Record<string, number>;
}

export interface IntelligenceAgentPerformance {
  name: string;
  agent_id: string;
  done: number;
  failed: number;
  success_rate: number;
  avg_minutes: number;
}

export interface IntelligenceFailurePatterns {
  total: number;
  patterns: Record<string, number>;
  details: { title: string; agent: string; reason: string; pattern: string }[];
}

export interface IntelligenceAnomaly {
  type: string;
  description: string;
  severity: "info" | "warning";
  agent_name?: string;
  agent_id?: string;
}

export interface IntelligenceConfig {
  enabled: boolean;
  interval_seconds: number;
  analysis_window_days: number;
  ollama_model: string;
  temperature: number;
  max_tokens: number;
  system_prompt: string;
  outlier_multiplier: number;
  success_rate_threshold: number;
  failure_count_threshold: number;
}

export interface IntelligenceInsights {
  task_durations: IntelligenceTaskDurations;
  agent_performance: IntelligenceAgentPerformance[];
  failure_patterns: IntelligenceFailurePatterns;
  anomalies: IntelligenceAnomaly[];
  analyzed_at: string | null;
}

// ── OpenClaw Skills ───────────────────────────────────────────────────────────

export type SkillStatus = "ready" | "missing_bin" | "missing_env" | "disabled" | "not_installed";

export interface OpenClawSkill {
  name: string;
  key: string;
  description: string;
  emoji?: string;
  homepage?: string;
  os?: string[];
  status: SkillStatus;
  source: "bundled" | "managed" | "workspace";
  requires?: {
    bins?: string[];
    env?: string[];
    config?: string[];
    missingBins?: string[];
    missingEnv?: string[];
  };
  install?: {
    id: string;
    kind: "brew" | "node" | "go" | "uv" | "download";
    label: string;
  }[];
  config?: {
    enabled?: boolean;
    hasApiKey?: boolean;
    env?: Record<string, string>;
  };
}

export interface SkillsResponse {
  skills: OpenClawSkill[];
  gateway_connected: boolean;
  total: number;
  ready: number;
  message?: string;
  error?: string;
}

export interface AgentSkillsResponse {
  skills: OpenClawSkill[];
  agent_skill_filter: string[] | null;
  gateway_connected: boolean;
  cli_plugins: CliPlugin[];
  agent_cli_plugins: string[] | null;
  /** Local custom skills from ~/.mc/skills/ (runtime-agnostic, no gateway). */
  custom_skills?: CustomSkill[];
  /** Agent allowlist for custom skills: null = all, [] = none, [...] = these. */
  agent_cli_skills?: string[] | null;
}

export interface CliPlugin {
  key: string;
  name: string;
  source: string;
  version: string;
  installed: boolean;
}

export interface CustomSkill {
  name: string;
  description: string;
  path: string;
}

export interface GithubSkillRepo {
  name: string;
  source: string;
  version: string;
  skills: string[];
}

// Phase 31 / OCS-15: ClawHubSkill, ClawHubSearchResponse removed (Marketplace
// UI deleted in Plan 31-01; backend /api/v1/clawhub/* routes removed in Phase 29).

// ── Model Catalog ─────────────────────────────────────────────────────────────

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  context_window: number | null;
  max_output: number | null;
  input_cost?: number | null;
  output_cost?: number | null;
  capabilities: string[];
  tier: string;
  params: string | null;
  description: string | null;
  available: boolean;
  used_by: { id: string; name: string; emoji: string | null }[];
}

export interface ModelCatalog {
  models: ModelInfo[];
  providers: Record<string, { name: string; color: string }>;
  gateway_connected: boolean;
  total: number;
}

// ── Secrets / API Keys ────────────────────────────────────────────────────────

export interface SecretEntry {
  id: string;
  key: string;
  value_masked: string;
  provider: string | null;
  label: string | null;
  description: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Credential {
  id: string;
  name: string;
  credential_type: "login" | "token" | "custom";
  data_masked: Record<string, string>;
  url: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProviderTemplate {
  provider: string;
  key: string;
  label: string;
  description: string;
  placeholder: string;
}

// ── Planner ─────────────────────────────────────────────────────────────────────

export type ProjectType = "feature" | "website" | "content" | "research" | "automation" | "design" | "free";
export type ProjectStatus = "draft" | "planning" | "active" | "paused" | "done" | "archived";

export interface ProjectTypeInfo {
  label: string;
  emoji: string;
  description: string;
}

export interface PlannerMessage {
  id: string;
  project_id: string;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface PlannerStartResponse {
  project: Project;
  planning_agent: { id: string; name: string; emoji: string | null } | null;
}

export interface PlannerFinalizeResponse {
  project: Project;
  tasks_created: number;
  tasks: Task[];
}

// ── Research ──────────────────────────────────────────────────────────────────

export interface ResearchStartResponse {
  project: Project;
  research_agent: { id: string; name: string; emoji: string | null } | null;
}

export interface ResearchSaveResponse {
  project: Project;
  knowledge_entry: BoardMemory;
}

// ── Cron Scheduler ───────────────────────────────────────────────────────────

export interface ScheduledJob {
  id: string;
  name: string;
  description: string | null;
  enabled: boolean;
  schedule_type: "daily" | "weekdays" | "interval" | "cron" | "weekly_custom";
  schedule_time: string | null;
  schedule_interval_hours: number | null;
  action_type: "chat_send" | "api_call" | "create_task";
  agent_id: string | null;
  agent_name: string | null;
  message: string | null;
  api_endpoint: string | null;
  retry_max: number;
  retry_delay_minutes: number;
  depends_on_job_id: string | null;
  notify_on_failure: boolean;
  task_board_id: string | null;
  task_title: string | null;
  task_priority: string | null;
  task_skip_review: boolean;
  last_run_at: string | null;
  last_run_status: "success" | "failed" | null;
  last_run_error: string | null;
  next_run_at: string | null;
  created_at: string;
  discord_channel_id: string | null;
  discord_channel_name: string | null;
  // New scheduling fields
  schedule_cron?: string | null;
  schedule_weekdays?: number[] | null;  // [0-6], 0=Mon
  start_date?: string | null;  // ISO date
  end_date?: string | null;    // ISO date
  // New action fields
  task_payload?: Record<string, unknown>;  // full task creation payload
  tags?: string[];  // default []
  // New monitoring fields
  snoozed_until?: string | null;  // ISO datetime
  consecutive_failures?: number;  // default 0
}

export interface ScheduledJobCreate {
  name: string;
  description?: string;
  enabled?: boolean;
  schedule_type: "daily" | "weekdays" | "interval" | "cron" | "weekly_custom";
  schedule_time?: string;
  schedule_interval_hours?: number;
  action_type: "chat_send" | "api_call" | "create_task";
  agent_id?: string;
  agent_name?: string;
  message?: string;
  api_endpoint?: string;
  retry_max?: number;
  retry_delay_minutes?: number;
  depends_on_job_id?: string;
  notify_on_failure?: boolean;
  task_board_id?: string;
  task_title?: string;
  task_priority?: string;
  task_skip_review?: boolean;
  discord_channel_id?: string;
  discord_channel_name?: string;
  // New scheduling fields
  schedule_cron?: string | null;
  schedule_weekdays?: number[] | null;
  start_date?: string | null;
  end_date?: string | null;
  // New action fields
  task_payload?: Record<string, unknown>;
  tags?: string[];
  // New monitoring fields
  snoozed_until?: string | null;
  consecutive_failures?: number;
}

export interface ScheduledJobRun {
  id: string;
  job_id: string;
  started_at: string;
  finished_at: string | null;
  status: "success" | "failed" | "skipped" | "running";
  error: string | null;
  detail: Record<string, unknown> | null;
  retry_attempt: number;
  task_id?: string | null;
  task_title?: string | null;
  task_status?: string | null;
}

export interface ScheduleJobStats {
  success_rate_7d: number;       // 0-1
  success_rate_30d: number;
  avg_duration_ms: number;
  p95_duration_ms: number;
  total_runs_30d: number;
  runs_by_day: Array<{ date: string; success: number; failed: number }>;
}

export interface ScheduleHeatmapCell {
  weekday: number;  // 0=Mon
  hour: number;     // 0-23
  count: number;
}

export interface ScheduleUpcomingFiring {
  job_id: string;
  job_name: string;
  fire_at: string;  // ISO datetime
  tags: string[];
}

export interface ScheduleFiringPreview {
  firings: string[];  // list of ISO datetimes
  description: string;  // human-readable e.g. "Mo, Mi, Fr um 09:00"
}

// ── Loops (ADR-051) ──────────────────────────────────────────────────────────
// Outcome-driven task loops: a runner spins up one normal parent task per
// round, then decides continue/pause/escalate/done once the round ends.

export type LoopStatus = "draft" | "running" | "waiting_gate" | "paused" | "done" | "failed";
export type LoopBacklogSource = "markdown" | "project" | "tag" | "open_ended";
export type LoopRoundOutcome = "done" | "failed" | null;

export interface Loop {
  id: string;
  board_id: string;
  project_id: string | null;
  name: string;
  goal: string;
  backlog_source: LoopBacklogSource;
  backlog_md: string | null;
  backlog_tag: string | null;
  round_brief: string | null;
  human_every_n_rounds: number;
  pause_on_failed_rounds: number;
  escalate_on: string | null;
  max_rounds: number | null;
  budget_usd?: number | null;
  budget_tokens?: number | null;
  max_duration_minutes: number | null;
  stop_on_backlog_empty: boolean;
  telegram_reports: boolean;
  status: LoopStatus;
  rounds_completed: number;
  consecutive_failed_rounds: number;
  current_round_no: number | null;
  current_task_id: string | null;
  last_error: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface LoopRound {
  id: string;
  round_no: number;
  task_id: string;
  outcome: LoopRoundOutcome;
  report: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface LoopDetail extends Loop {
  rounds: LoopRound[];
}

export interface LoopCreate {
  board_id: string;
  name: string;
  goal: string;
  project_id?: string;
  backlog_source?: LoopBacklogSource;
  backlog_md?: string;
  backlog_tag?: string;
  round_brief?: string;
  human_every_n_rounds?: number;
  pause_on_failed_rounds?: number;
  max_rounds?: number;
  budget_usd?: number | null;
  budget_tokens?: number | null;
  max_duration_minutes?: number;
  stop_on_backlog_empty?: boolean;
  telegram_reports?: boolean;
}

export type LoopUpdate = Partial<Omit<LoopCreate, "board_id">>;

// ── Henry / Playbooks ───────────────────────────────────────────────────────

export interface PlaybookCatalogOption {
  value: string;
  label: string;
}

export interface PlaybookCatalogField {
  key: string;
  label: string;
  type: "short_text" | "long_text" | "select" | "boolean" | "number";
  required?: boolean;
  default?: string | number | boolean;
  placeholder?: string;
  options?: PlaybookCatalogOption[];
}

export interface PlaybookCatalogItem {
  key: string;
  name: string;
  summary: string;
  icon: string;
  default_skill_pack_key: string;
  suggested_mode: "manual" | "scheduled";
  fields: PlaybookCatalogField[];
  output_contract?: {
    sections?: string[];
  };
}

export interface SkillPack {
  id: string;
  key: string;
  name: string;
  description: string | null;
  category: string;
  status: string;
  icon: string | null;
  color: string | null;
  skill_keys: string[];
  guidance: Record<string, unknown> | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface Playbook {
  id: string;
  workflow_id: string | null;
  board_id: string | null;
  project_id: string | null;
  skill_pack_id: string | null;
  default_agent_id: string | null;
  kind: string;
  name: string;
  summary: string | null;
  goal: string | null;
  scope: "global" | "board" | "project";
  status: "draft" | "review" | "active" | "archived";
  current_version: number;
  input_contract: Record<string, unknown> | null;
  output_contract: Record<string, unknown> | null;
  current_config: Record<string, unknown>;
  preview_markdown: string | null;
  extra_metadata: Record<string, unknown> | null;
  review_notes: string | null;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlaybookVersion {
  id: string;
  playbook_id: string;
  version: number;
  snapshot: Record<string, unknown>;
  change_reason: string | null;
  created_by: string;
  created_at: string;
}

export interface Automation {
  id: string;
  playbook_id: string;
  workflow_id: string | null;
  board_id: string | null;
  project_id: string | null;
  name: string;
  summary: string | null;
  status: "draft" | "active" | "paused" | "archived";
  trigger_type: "manual" | "scheduled";
  trigger_config: Record<string, unknown> | null;
  delivery_config: Record<string, unknown> | null;
  runtime_overrides: Record<string, unknown> | null;
  last_run_at: string | null;
  next_run_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface SkillCandidate {
  id: string;
  board_id: string | null;
  project_id: string | null;
  playbook_id: string | null;
  automation_id: string | null;
  candidate_type: "new_skill" | "patch" | "playbook_improvement";
  title: string;
  summary: string | null;
  target_skill_key: string | null;
  status: "open" | "approved" | "rejected" | "applied";
  evidence: Record<string, unknown> | null;
  source_run_ids: string[];
  draft_skill_content: string | null;
  proposed_by: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlaybookRunProjection {
  run: Record<string, unknown>; // raw workflow-engine run — no typed client since the UI was removed

  playbook: Playbook | null;
  automation: Automation | null;
}

export interface HenrySessionState {
  session: Project;
  messages: PlannerMessage[];
  playbook: Playbook | null;
  selected_kind: string | null;
  pending_field_key: string | null;
  stage: "intake" | "review";
}

// ── Meetings ──────────────────────────────────────────────────────────────────

export type MeetingType = "weekly" | "ad_hoc" | "retrospective";
export type MeetingStatus = "scheduled" | "running" | "completed" | "failed" | "cancelled";

export interface Meeting {
  id: string;
  board_id: string;
  title: string;
  meeting_type: MeetingType;
  status: MeetingStatus;
  agenda: string[] | null;
  participant_ids: string[] | null;
  summary: string | null;
  decisions: Record<string, unknown>[] | null;
  action_items: Record<string, unknown>[] | null;
  memory_id: string | null;
  scheduled_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface MeetingMessage {
  id: string;
  meeting_id: string;
  agent_id: string | null;
  agent_name: string | null;
  role: "facilitator_question" | "agent_response" | "system_note" | "summary";
  content: string;
  round: number;
  topic_index: number;
  created_at: string;
}

// ── Task Transcript ──────────────────────────────────────────────────────────

export interface TaskTranscriptResponse {
  transcript_mode: "direct" | "reconstructed" | "unavailable" | "taskcomment";
  session_role: "work" | "review" | null;
  session_key: string | null;
  messages: TranscriptMessage[];
}

export interface TaskSessionInfo {
  task_id: string;
  title: string;
  status: TaskStatus;
  session_key: string | null;
  has_active_session: boolean;
  dispatched_at: string | null;
  completed_at: string | null;
}

// ── Task Hierarchy (Theme 1: Wave 2) ────────────────────────────────────────

export interface TaskSummary {
  id: string;
  title: string;
  status: TaskStatus;
  priority: Priority;
}

export interface TaskHierarchy {
  parent: TaskSummary | null;
  children: TaskSummary[];
  report_back: {
    required: boolean;
    channel: string | null;
    status: string | null;
    requirements: string | null;
  } | null;
  has_credentials: boolean;
  requester: {
    channel: string;
    id: string | null;
  } | null;
}

// ── Autonomy (Theme 3: Wave 2) ──────────────────────────────────────────────

export interface AutonomyConfig {
  levels: Record<string, AutonomyLevel>;
  defaults: Record<string, AutonomyLevel>;
}

// ── Usage Tracking V1 (Theme 4: Wave 2) ─────────────────────────────────────
// Honest V1: snapshot only, no token counting.

export interface AgentUsageSnapshot {
  agent_id: string;
  name: string;
  emoji: string | null;
  model: string | null;
  status: AgentStatus;
  run_state: string;
  context_tokens: number;
  context_max: number;
  context_pct: number;
  tasks_completed: number;
  total_compactions: number;
  last_seen_at: string | null;
}

export interface UsageAnalytics {
  agents: AgentUsageSnapshot[];
  models: Record<string, number>;  // model_id -> agent_count
  total_agents: number;
  total_tasks_completed: number;
}

// ── Cost Tracking ─────────────────────────────────────────────────────────────

export interface CostAgentSummary {
  agent_id: string;
  agent_name: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  event_count: number;
}

export interface CostSessionSummary {
  agent_id: string;
  agent_name: string;
  session_key: string;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  event_count: number;
  last_event_at: string | null;
}

export interface CostOverview {
  period_days: number;
  total_tokens_in: number;
  total_tokens_out: number;
  total_cost_usd: number;
  agents: CostAgentSummary[];
  sessions?: CostSessionSummary[];
}

// ── Model Prices (Settings → Kosten) ─────────────────────────────────────────

export interface ModelPrice {
  id: string;
  model_pattern: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  cache_write_per_mtok: number;
  currency: string;
  valid_from: string; // ISO string
  priority: number;
  note: string | null;
}

export interface ModelPriceCreate {
  model_pattern: string;
  input_per_mtok: number;
  output_per_mtok: number;
  cache_read_per_mtok: number;
  cache_write_per_mtok: number;
  currency?: string;
  valid_from: string;
  priority: number;
  note?: string | null;
}

export interface ModelPriceUpdate {
  model_pattern?: string;
  input_per_mtok?: number;
  output_per_mtok?: number;
  cache_read_per_mtok?: number;
  cache_write_per_mtok?: number;
  currency?: string;
  valid_from?: string;
  priority?: number;
  note?: string | null;
}

export interface UnmatchedModel {
  model: string;
  event_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

// ── Cost Aggregation (Insights → Kosten-Tab) ──────────────────────────────────

export interface CostByModel {
  model: string;
  harness_list: string[];
  event_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
}

export interface CostTimeseries {
  date: string; // "YYYY-MM-DD"
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface CostByTask {
  task_id: string;
  task_title: string;
  event_count: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

// ── Runtimes ──────────────────────────────────────────────────────────────────

export type RuntimeState =
  | "stopped"
  | "starting"
  | "warming"
  | "ready"
  | "failed"
  | "unknown";

export type RuntimeType =
  | "vllm_docker"
  | "lmstudio"
  | "unsloth"
  | "unsloth_porsche"
  | "openai_compatible"
  | "cloud"
  | "hermes";

export interface Runtime {
  // On the GET /runtimes (legacy JSON) response `id` is the slug.
  // On the GET /runtimes/db (DB-backed) response `id` is a UUID.
  id: string;
  slug?: string;
  display_name: string;
  runtime_type: RuntimeType;
  /** Phase 24 (Hermes): runtime cannot be re-bound to other agents
   *  and other agents cannot switch into it. Backend enforces; frontend
   *  uses this purely as a UX hint (disabled state + lock icon). */
  single_instance?: boolean;
  provider: string;
  endpoint: string;
  healthcheck_path: string;
  container_name: string | null;
  lms_identifier?: string;
  model_identifier?: string | null;
  role_tags: string[];
  supports_tools: boolean;
  supports_reasoning: boolean;
  supports_streaming: boolean;
  preferred_context_len: number;
  max_context_len: number;
  gpu_profile: string;
  memory_notes: string;
  startup_notes: string;
  ui_order: number;
  enabled: boolean;
  // Power-managed runtime (unsloth_porsche): box sleeps when idle, woken via WoL.
  control_url?: string | null;      // Flask :5555 control plane (e.g. http://192.0.2.20:5555)
  wol_mac_address?: string | null;  // target MAC for the Wake-on-LAN magic packet
  power_managed?: boolean;          // true → runtime sleeps when idle, gets a "Wecken" action
  // State (enriched by the API) — optional on DB responses.
  state?: RuntimeState;
  http_reachable?: boolean;
  container_status?: string | null;
  // Host Registry (ADR-048): resolved host binding via runtime.host_id.
  // null/absent = no host bound (legacy string fallback or settings fallback).
  host?: HostRef | null;
  // Harness/Provider-Decoupling (ADR-056): API key the runtime authenticates
  // with, stored as a `secrets` row reference (never the raw value).
  api_key_secret_id?: string | null;
  // Engine Control v0 (ADR-057): autostart flag file toggled over SSH on the
  // bound host. autostart_supported opts the runtime into the /runtimes
  // toggle; autostart_flag_path is the file a systemd unit checks on boot.
  autostart_supported?: boolean;
  autostart_flag_path?: string | null;
}

// Engine Control v0 (ADR-057) — GET/POST .../db/{slug}/autostart response.
// enabled: null = host unreachable, autostart state unknown.
export interface RuntimeAutostartStatus {
  slug: string;
  flag_path: string | null;
  enabled: boolean | null;
  reachable: boolean;
}

export interface RuntimeAgentRef {
  id: string;
  name: string;
  agent_runtime: string;
  pending_runtime_sync?: boolean;
}

export interface RuntimeAgentsResponse {
  runtime_slug: string;
  count: number;
  agents: RuntimeAgentRef[];
}

// CLI-Tool-Updates (Task 7) — /api/v1/cli-tools cockpit.
export type CliUpdatePhase = "manifest" | "build" | "recreate" | "done" | "failed";

export interface CliToolAgentRef {
  id: string;
  name: string;
  busy: boolean;
}

export interface CliToolStatus {
  tool: string; // "openclaude" | "claude" | "omp"
  image: string;
  installed: string | null;
  target: string | null;
  latest: string | null;
  update_available: boolean;
  checked_at: string | null;
  agents_affected: CliToolAgentRef[];
  // Current update phase for this tool if one is in flight, else null.
  build_state: CliUpdatePhase | null;
}

export interface CliToolsResponse {
  tools: CliToolStatus[];
}

export interface CliUpdateProgress {
  phase: CliUpdatePhase | "idle";
  tool?: string;
  from_version?: string | null;
  to_version?: string | null;
  log_tail?: string | null;
  error?: string | null;
  updated_at?: string;
}

// Phase 15 T2.1 — return shape of switch_agent_runtime() service. Used by
// both POST /agents/{id}/preview-runtime-switch (dry_run=true) and the
// `_switch` summary attached to PATCH /agents/{id} when runtime_id changes.
export interface RuntimeSwitchSummary {
  id: string;
  slug: string;
  display_name: string;
  runtime_type: string;
  model_identifier: string | null;
  /** Phase 24 (Hermes): mirror of Runtime.single_instance for switch preview. */
  single_instance?: boolean;
}

export interface RuntimeSwitchPreview {
  old_runtime: RuntimeSwitchSummary | null;
  new_runtime: RuntimeSwitchSummary;
  image_switched: boolean;
  duration_ms: number;
  warnings: string[];
  dry_run: boolean;
  health: { healthy?: boolean; reason?: string } | null;
}

export interface RuntimeLiveStatus {
  reachable: boolean;
  served_model: string | null;
  latency_ms: number | null;
  last_probe_at: string;
  consecutive_failures: number;
  drift: boolean;
}

export interface RuntimesLiveResponse {
  live: Record<string, RuntimeLiveStatus>;
  watcher_enabled: boolean;
  interval: number;
}

export interface ProbeEndpointResult {
  reachable: boolean;
  models: string[];
  detected_type: "lmstudio" | "vllm_docker" | "openai_compatible" | null;
  suggested_model: string | null;
  error: string | null;
}

export interface RuntimeSwitchProgress {
  step: "rendering" | "restarting" | "waiting_healthy" | "done" | "rolled_back" | null;
  error?: string | null;
  ts?: number;
}

export interface RuntimeCreate {
  slug: string;
  display_name: string;
  runtime_type: RuntimeType;
  endpoint: string;
  healthcheck_path?: string;
  model_identifier?: string;
  container_name?: string;
  lms_identifier?: string;
  lms_cli_path?: string;
  /** DEPRECATED legacy host string — bind via host_id (Host Registry, ADR-048). */
  host?: string;
  /** Host Registry binding: host UUID; explicit null = unbind (PATCH /runtimes/db/{slug}). */
  host_id?: string | null;
  role_tags?: string[];
  supports_tools?: boolean;
  supports_reasoning?: boolean;
  supports_streaming?: boolean;
  preferred_context_len?: number;
  max_context_len?: number;
  gpu_profile?: string;
  memory_notes?: string;
  startup_notes?: string;
  ui_order?: number;
  enabled?: boolean;
  /** Harness/Provider-Decoupling (ADR-056): bind an existing `secrets` row as this runtime's API key. */
  api_key_secret_id?: string | null;
  /** Engine Control v0 (ADR-057): opt this runtime into the autostart toggle. */
  autostart_supported?: boolean;
  /** ADR-057: absolute path on the bound host checked by its autostart systemd unit. */
  autostart_flag_path?: string | null;
}

export interface RuntimesResponse {
  runtimes: Runtime[];
}

export interface RuntimeActionResult {
  ok: boolean;
  message: string;
}

// Sparkrun recipe management (ADR-059) — `tp`/`nodes` come straight from
// `sparkrun list`'s TP/Nodes columns (null when the CLI prints `-`).
// `solo_capable` is derived server-side against the target host's actual
// GPU count (see sparkrun_manager.list_recipes) — a recipe needing more
// GPUs/nodes than the host has is NOT solo-startable, even though nothing
// about its name suggests that.
export interface SparkrunRecipe {
  name: string;
  model: string | null;
  registry: string;
  tp: number | null;
  nodes: number | null;
  solo_capable: boolean;
}

// Harness/Provider-Decoupling (ADR-056) — compat matrix for the harness
// selector in RuntimeSwitchModal.
export interface CompatMatrixHarness {
  key: Harness;
  label: string;
}

export interface CompatMatrixRuntime {
  slug: string;
  display_name: string;
  protocol: "openai" | "anthropic" | null;
  compatible_harnesses: Harness[];
  reasons: Record<string, string>;
}

export interface CompatMatrix {
  harnesses: CompatMatrixHarness[];
  runtimes: CompatMatrixRuntime[];
}

// ── Runtime Schedules ─────────────────────────────────────────────────────────────

export interface RuntimeScheduleRun {
  executed_at: string;
  success: boolean;
  message: string | null;
}

export interface RuntimeSchedule {
  id: string;
  runtime_id: string;
  name: string;
  action: "start" | "stop" | "kv_reset";
  time_of_day: string;
  days: "daily" | "weekdays" | "weekends";
  unload_first: boolean;
  enabled: boolean;
  created_at: string;
  last_run: RuntimeScheduleRun | null;
}

export interface RuntimeScheduleCreate {
  name: string;
  action: "start" | "stop" | "kv_reset";
  time_of_day: string;
  days: "daily" | "weekdays" | "weekends";
  unload_first?: boolean;
  enabled?: boolean;
}

export interface LMStudioModel {
  id: string;
  display_name: string;
  size_gb: number;
  is_loaded: boolean;
  is_embedding: boolean;
}

export interface LMStudioModelsResponse {
  models: LMStudioModel[];
  reachable: boolean;
}

export interface VllmContainer {
  container_name: string;
  image: string;
  endpoint: string;
  state: string;
  is_registered: boolean;
  registered_id: string | null;
}

export interface VllmDiscoverResponse {
  containers: VllmContainer[];
}

export interface LMSCatalogModel {
  model_id: string;
  name: string;
  params?: string;
  size_gb: number;
  quantization: string;
  architecture: string;
  hf_repo: string;
}

export interface HFRepoInfo {
  name: string;
  error?: string;
  files: { filename: string; size_gb: number; size_mb: number }[];
}

export interface LMSActiveDownload {
  id: string;
  name: string;
  type: "lmstudio" | "huggingface";
  repo?: string;
  progress_pct: number | null;
  progress_text: string;
}

export interface SparkMetrics {
  reachable: boolean;
  gpu_util_pct: number | null;
  vram_used_mb: number | null;
  vram_total_mb: number | null;
  gpu_temp_c: number | null;
  ram_used_mb: number | null;
  ram_total_mb: number | null;
}

// ── Repos (Repo Registry, ADR-050) ───────────────────────────────────────────

export type RepoVisibility = "private" | "public";
export type RepoSource = "mc" | "imported";

export interface RepoLinkedProject {
  id: string;
  name: string;
  status: ProjectStatus;
  board_id: string;
}

export interface Repo {
  id: string;
  full_name: string;
  url: string;
  default_branch: string;
  description: string | null;
  rules_md: string | null;
  visibility: RepoVisibility;
  is_active: boolean;
  source: RepoSource;
  last_synced_at: string | null;
  created_at: string;
  updated_at: string;
  linked_projects: RepoLinkedProject[];
}

/** GitHub repos of the configured owner that are not yet registered in MC. */
export interface RepoImportCandidate {
  full_name: string;
  url: string;
  description: string | null;
  visibility: RepoVisibility;
  default_branch: string;
  is_archived: boolean;
  pushed_at: string | null;
}

export interface RepoUpdate {
  description?: string | null;
  rules_md?: string | null;
  default_branch?: string | null;
  is_active?: boolean;
}

// ── GitHub connection (ADR-055) ──────────────────────────────────────────────

export type GithubConfigSource = "vault" | "env" | null;

/** GET /api/v1/repos/github-status. Without ?probe=true, connected/login/…
 * are always null (fast config-only view); with probe, a live `gh api` check
 * runs (up to ~15s) and fills them in (or sets connected=false + error). */
export interface GithubStatus {
  owner: string | null;
  owner_source: GithubConfigSource;
  token_set: boolean;
  token_source: GithubConfigSource;
  configured: boolean;
  connected: boolean | null;
  login: string | null;
  owner_type: string | null;
  rate_limit_remaining: number | null;
  rate_limit_total: number | null;
  error: string | null;
}

/** Config-only slice returned by PUT /api/v1/repos/github-config. */
export type GithubConfigStatus = Pick<
  GithubStatus,
  "owner" | "owner_source" | "token_set" | "token_source" | "configured"
>;

/** null/omitted = leave unchanged; "" = clear (falls back to .env). */
export interface GithubConfigUpdate {
  owner?: string | null;
  token?: string | null;
}

// ── Hosts (Host Registry, ADR-048) ───────────────────────────────────────────

export type HostKind = "ssh" | "flask_wol" | "local";

export interface Host {
  id: string;
  slug: string;
  display_name: string;
  kind: HostKind;
  ssh_host: string | null;
  ssh_user: string | null;
  ssh_key_path: string | null;
  control_url: string | null;      // flask_wol control plane (PORSCHE :5555)
  wol_mac_address: string | null;  // target MAC for the Wake-on-LAN magic packet
  power_managed: boolean;
  notes: string | null;
  enabled: boolean;
  ui_order: number;
  created_at: string;
  updated_at: string;
}

export interface HostCreate {
  slug: string;
  display_name: string;
  kind: HostKind;
  ssh_host?: string | null;
  ssh_user?: string | null;
  ssh_key_path?: string | null;
  control_url?: string | null;
  wol_mac_address?: string | null;
  power_managed?: boolean;
  notes?: string | null;
  enabled?: boolean;
  ui_order?: number;
}

/** Compact host reference embedded in GET /runtimes responses. */
export interface HostRef {
  id: string;
  slug: string;
  display_name: string;
}

/**
 * Live metrics per host (GET /hosts/{id}/metrics).
 * ssh hosts: nvidia-smi + free -m (same shape as SparkMetrics);
 * flask_wol hosts: awake/health only; local hosts: empty (reachable only).
 */
export interface HostMetrics {
  reachable: boolean;
  gpu_util_pct?: number | null;
  vram_used_mb?: number | null;
  vram_total_mb?: number | null;
  gpu_temp_c?: number | null;
  ram_used_mb?: number | null;
  ram_total_mb?: number | null;
  // flask_wol health status ("asleep" | "booted_no_model" | "serving" | …)
  awake?: boolean | null;
  status?: string | null;
}

// ── CLI Sessions ─────────────────────────────────────────────────────────────
export interface CliGlobalSession {
  task_id: string;
  session: string;
  elapsed_seconds: number;
  permanent?: boolean;
  shell?: boolean;
  agent_slug: string;
  agent_id: string | null;
  agent_name: string;
}

// ── SSE Event ──────────────────────────────────────────────────────────────────
export interface SSEEvent {
  id: string;
  event: string;
  data: Record<string, unknown>;
}

// ── Install Requests ──────────────────────────────────────────────────────────

export type InstallType = "skill" | "plugin" | "mcp";
export type InstallOperation = "install" | "uninstall";
export type InstallActionType =
  | "install_skill" | "uninstall_skill"
  | "install_plugin" | "uninstall_plugin"
  | "install_mcp" | "uninstall_mcp";

export interface InstallRequestBody {
  type: InstallType;
  operation: InstallOperation;
  source?: string;
  name: string;
  target_agent_id: string;
  reason: string;
  autonomy_level?: "L1" | "L2" | "L3";
  proposed_config?: Record<string, unknown>;
}

export interface InstallRequestResponse {
  approval_id: string;
  status: "pending" | "approved" | "rejected" | "expired";
  existing: boolean;
}

export interface InstallApprovalPayload {
  name: string;
  source?: string | null;
  target_agent_id: string;
  target_agent_slug?: string;
  requester_agent_id: string;
  requester_agent_slug?: string;
  reason: string;
  proposed_config?: Record<string, unknown> | null;
}

export interface InstallLogEntry {
  id: string;
  approval_id: string | null;
  requester_agent_id: string | null;
  target_agent_id: string;
  action_type: InstallActionType;
  resource_name: string;
  source: string | null;
  result: "success" | "failed" | "rolled_back";
  error: string | null;
  installed_version: string | null;
  previous_state: Record<string, unknown> | null;
  created_at: string;
}

// ── MCP Servers ──────────────────────────────────────────────────────────────

export type MCPTransport = "stdio" | "http" | "sse";

export interface MCPServer {
  name: string;
  transport: MCPTransport;
  description?: string | null;
  source?: string | null;
  installed_version?: string | null;
  command?: string | null;
  args?: string[] | null;
  url?: string | null;
  installed_at?: string | null;
}

// ── Vault (M.3) ───────────────────────────────────────────────────────────────

export type VaultNoteType =
  | "lesson" | "knowledge" | "reference"
  | "journal" | "weekly_review" | "note"
  | "deliverable";

/**
 * A single vault note as returned by GET /api/v1/vault/notes and
 * GET /api/v1/vault/search.
 *
 * `content` is the markdown BODY only — the backend strips the YAML
 * frontmatter at index time, so `title` and `date` are surfaced as their
 * own fields (sourced from frontmatter at write time). `tags` is a
 * space-joined string (FTS5 storage) — split(" ") for an array.
 */
export interface VaultNote {
  path: string;          // vault-relative path, e.g. "agents/sparky/lessons/x.md"
  id: string;            // UUID / slug from frontmatter
  agent: string;         // owner slug
  type: VaultNoteType;
  tags: string;          // space-joined string (FTS5 storage) — split(" ") for array
  project: string;       // empty string if unset (FTS5 never returns null for indexed cols)
  title: string;         // frontmatter.title — empty string when the writer omitted it
  date: string;          // frontmatter.date / created_at as raw string — empty when undated
  content: string;       // markdown body (list = full, search = snippet)
}

export interface VaultSearchResponse {
  q: string;
  hits: VaultNote[];
}

export interface VaultNotesListResponse {
  count: number;
  notes: VaultNote[];
}

/** Response from GET /api/v1/vault/note/{path} */
export interface VaultNoteDetail {
  frontmatter: Record<string, unknown>;
  content: string;
}

/** Response from POST /api/v1/vault/track-view */
export interface VaultTrackViewResponse {
  ok: boolean;
  error?: string;
}

// ── Vault Graph (M.4) ─────────────────────────────────────────────────────────
// Field names mirror vault_graph.py build_graph() output exactly.

/** One node in the 3D constellation — id = vault-relative path */
export interface GraphNode {
  id: string;           // vault-relative path (e.g. "agents/sparky/lessons/x.md")
  label: string;        // file stem (filename without .md)
  type: VaultNoteType;
  agent: string;        // owner slug
  tags: string[];       // already split (vault_graph.py calls _parse_tags)
  viewCount: number;    // from VaultActivity.top_n_views (0 when no data)
  cluster_id: number | null;
}

export interface GraphEdge {
  source: string;   // vault path
  target: string;   // vault path
  weight: number;   // wikilink occurrence count
}

export interface GraphCluster {
  cluster_id: number;
  member_paths: string[];
  centroid: number[];   // mean embedding vector (may be [] when no embeddings)
}

export interface VaultGraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  clusters: GraphCluster[];
  built_at: string;   // ISO-8601 UTC, e.g. "2026-05-15T10:00:00Z"
  stats: {
    nodes: number;
    edges: number;
    clusters: number;
    build_ms: number;
  };
}

/** Filter applied by voice (T9) or UI controls to highlight a node subset */
export interface GraphFilter {
  agent?: string | string[];
  type?: VaultNoteType | VaultNoteType[];
  tag?: string;
}

// ── Live Browser View (view-only CDP screencast) ──────────────────────────────

/** One open page target from GET /api/v1/browser-live/targets */
export interface BrowserLiveTarget {
  id: string;
  title: string;
  url: string;
}
