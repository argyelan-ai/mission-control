import type { HeadPair, HeadRun } from "../heads";

export function mkPair(over: Partial<HeadPair> = {}): HeadPair {
  const status = over.status ?? "ok";
  return {
    harness: "omp",
    harness_label: "omp",
    runtime_slug: "glm-local",
    runtime_label: "GLM local",
    model: "glm",
    locality: "local",
    status,
    reason_code: null,
    live: true,
    box_keys: ["host-1"],
    busy_by: null,
    engine_in_use: false,
    startable: status !== "blocked",
    ...over,
  };
}

export function mkRun(over: Partial<HeadRun> = {}): HeadRun {
  return {
    run_id: "11111111-1111-4111-8111-111111111111",
    task_id: "task-1",
    title: "Fix flaky retry test",
    harness: "omp",
    runtime_slug: "glm-local",
    model: "glm",
    repo_full_name: "acme/tool",
    branch: "mc-head/fix-flaky-1111",
    mode: "fresh",
    restarted_from: null,
    box_keys: ["host-1"],
    created_at: "2026-09-23T10:00:00Z",
    started_at: "2026-09-23T10:00:05Z",
    exited_at: null,
    state: "running",
    reason: null,
    silent_s: 40,
    heartbeat_stale: false,
    step: "4/7 sabotage probe",
    question: null,
    pr_url: null,
    tmux: null,
    run_record: false,
    task_deleted: false,
    ...over,
  };
}
