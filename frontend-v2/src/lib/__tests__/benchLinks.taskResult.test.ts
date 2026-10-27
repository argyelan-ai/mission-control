// A bench agent run is a fleet task; its result lives in the benchmark.
// Core pages (task detail, chat) find the test through the task's
// auto_reason — written by the bench orchestrator as
// "bench_studio challenge <test id>" (backend callbacks.BENCH_AUTO_REASON_PREFIX).
import { describe, expect, it } from "vitest";
import { benchResultHref, benchResultHrefOfTask } from "../benchLinks";

const ID = "bb467c57-8213-4438-a405-08b1d4cc7de2";

describe("benchResultHrefOfTask", () => {
  it("a bench run's task points at its result page", () => {
    expect(benchResultHrefOfTask({ auto_reason: `bench_studio challenge ${ID}` })).toBe(benchResultHref(ID));
  });

  it("any other task points nowhere", () => {
    expect(benchResultHrefOfTask({ auto_reason: null })).toBeNull();
    expect(benchResultHrefOfTask({ auto_reason: undefined })).toBeNull();
    expect(benchResultHrefOfTask({ auto_reason: "watchdog requeue" })).toBeNull();
    // Never from the title: an operator card named "[Bench] …" is no bench run.
    expect(benchResultHrefOfTask({ auto_reason: "bench_studio challenge not-an-id" })).toBeNull();
  });
});
