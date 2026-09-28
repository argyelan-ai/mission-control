// Made-up numbers and names for tests and screenshots — never real usage.
import type { CostByModel, CostByTask, CostOverview, IntelligenceInsights } from "@/lib/types";
import type { HeadRun } from "@/lib/heads";

export function mkInsights(over: Partial<IntelligenceInsights> = {}): IntelligenceInsights {
  return {
    analyzed_at: "2026-09-30T08:00:00Z",
    task_durations: {
      avg_minutes: 12.5,
      total: 16,
      outliers: [{ task_id: "t9", title: "Long task", agent_id: "a2", minutes: 90 }],
      per_agent: { Coder: 9.1, Reviewer: 4.2 },
    },
    agent_performance: [
      { name: "Coder", agent_id: "a1", done: 6, failed: 1, success_rate: 85.7, avg_minutes: 9.1 },
      { name: "Reviewer", agent_id: "a2", done: 5, failed: 0, success_rate: 100, avg_minutes: 4.2 },
      { name: "Researcher", agent_id: "a3", done: 3, failed: 1, success_rate: 75, avg_minutes: 7 },
      { name: "Idle", agent_id: "a4", done: 0, failed: 0, success_rate: 100, avg_minutes: 0 },
    ],
    failure_patterns: { total: 2, patterns: { timeout: 1, merge_conflict: 1 }, details: [] },
    anomalies: [{ type: "slow_tasks", description: "backend text", severity: "info" }],
    ...over,
  };
}

export function mkHeadRun(state: HeadRun["state"], createdAt: string, id = `${state}-${createdAt}`): HeadRun {
  return {
    run_id: id, task_id: null, title: null, harness: "omp", runtime_slug: "local-box", model: "local-model",
    repo_full_name: null, branch: null, mode: "fresh", restarted_from: null, box_keys: [], created_at: createdAt,
    started_at: createdAt, exited_at: null, state, reason: null, silent_s: null, heartbeat_stale: false,
    step: null, question: null, pr_url: null, tmux: null, run_record: false, task_deleted: false,
  };
}

export function mkByModel(): CostByModel[] {
  return [
    { model: "cloud-large", harness_list: ["host", "cli-bridge"], input_tokens: 2_100_000, output_tokens: 610_000, cache_read_tokens: 38_000_000, cache_write_tokens: 900_000, event_count: 1840, cost_usd: 1204.5 },
    { model: "local-flash", harness_list: ["omp"], input_tokens: 820_000, output_tokens: 240_000, cache_read_tokens: 0, cache_write_tokens: 0, event_count: 310, cost_usd: 0 },
    { model: "cloud-small", harness_list: ["hermes"], input_tokens: 90_000, output_tokens: 22_000, cache_read_tokens: 400_000, cache_write_tokens: 0, event_count: 95, cost_usd: 18.2 },
  ] as CostByModel[];
}

export function mkByTask(): CostByTask[] {
  return [
    { task_id: "t1", task_title: "Rework the settings page", event_count: 420, input_tokens: 310_000, output_tokens: 44_000, cost_usd: 96.4 },
    { task_id: "t2", task_title: "Add a retry to the uploader", event_count: 180, input_tokens: 120_000, output_tokens: 18_000, cost_usd: 31.1 },
  ] as CostByTask[];
}

export function mkCosts(withSessions = false): CostOverview {
  return {
    total_cost_usd: 1222.7,
    total_tokens_in: 3_010_000,
    total_tokens_out: 872_000,
    agents: [
      { agent_id: "a1", agent_name: "Coder", tokens_in: 1_900_000, tokens_out: 500_000, event_count: 1400, cost_usd: 880 },
      { agent_id: "a2", agent_name: "Reviewer", tokens_in: 700_000, tokens_out: 210_000, event_count: 520, cost_usd: 300.2 },
      { agent_id: "a4", agent_name: "Idle", tokens_in: 0, tokens_out: 0, event_count: 0, cost_usd: 0 },
    ],
    sessions: withSessions
      ? Array.from({ length: 24 }, (_, i) => ({
          session_key: `agent:a1:task:${String(i).padStart(8, "0")}-aaaa:work`,
          agent_name: i % 2 ? "Reviewer" : "Coder",
          tokens_in: 10_000 * (24 - i),
          tokens_out: 1_000 * (24 - i),
          cost_usd: 2 * (24 - i),
          last_event_at: "2026-09-30T07:00:00Z",
        }))
      : undefined,
  } as unknown as CostOverview;
}
