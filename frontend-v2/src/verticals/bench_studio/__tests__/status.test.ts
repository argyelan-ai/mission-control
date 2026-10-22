import { describe, it, expect } from "vitest";
import type { BenchChallenge, BenchEntry } from "../types";
import { abortReason } from "../status";

function entry(over: Partial<BenchEntry> = {}): BenchEntry {
  return {
    id: "e", challenge_id: "c", model_label: "omp-alpha", source_kind: "agent",
    spark_model: null, agent_id: "a", display_tag: null, task_id: null,
    status: "failed", artifact_path: null, video_path: null, screenshot_path: null,
    metrics: {}, error: "agent task deleted", ...over,
  };
}

function challenge(over: Partial<BenchChallenge> = {}): BenchChallenge {
  return {
    id: "c", title: "Pagoda - OMP", prompt_template_id: null, prompt_text: "p",
    mode: "single", status: "failed", series_label: null, series_no: null,
    record_duration_s: null, composed_video_path: null, content_pipeline_id: null,
    error: "all entries failed during generation", archived_at: null,
    created_at: "2026-10-19T16:36:36Z", updated_at: "2026-10-19T16:36:36Z",
    entries: [entry()], ...over,
  };
}

describe("abortReason", () => {
  it("names a deleted task — the 3 stuck 19.10. orphans after the sweep", () => {
    expect(abortReason(challenge())).toBe("taskDeleted");
  });

  it("names failed / aborted fleet tasks", () => {
    expect(abortReason(challenge({ entries: [entry({ error: "agent task failed" })] }))).toBe("taskFailed");
    expect(abortReason(challenge({ entries: [entry({ error: "agent task aborted" })] }))).toBe("taskAborted");
  });

  it("names the operator stop", () => {
    expect(abortReason(challenge({ error: "stopped by operator" }))).toBe("stopped");
  });

  it("names recording and video failures", () => {
    expect(abortReason(challenge({ error: "all entries failed during rendering" }))).toBe("renderFailed");
    expect(abortReason(challenge({ error: "entry rerender failed and no other recording survives" }))).toBe("renderFailed");
    expect(abortReason(challenge({ error: "compose failed: HTTP 502" }))).toBe("composeFailed");
  });

  it("gives no reason when entries failed for different reasons", () => {
    expect(
      abortReason(challenge({
        mode: "side_by_side",
        entries: [entry(), entry({ id: "e2", error: "agent task failed" })],
      })),
    ).toBeNull();
  });

  it("gives no reason for unknown errors and for non-failed challenges", () => {
    expect(abortReason(challenge({ error: "fan-out crashed — see backend logs" }))).toBeNull();
    expect(abortReason(challenge({ status: "generating", error: null }))).toBeNull();
  });
});
