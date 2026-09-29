import { describe, it, expect } from "vitest";
import { defaultTabFor, resolveTab, isTaskTabKey } from "../tabs";

describe("defaultTabFor", () => {
  it("opens Comments while the task is running — that is where the live activity is", () => {
    expect(defaultTabFor("in_progress")).toBe("comments");
  });
  it("opens Summary for every other status", () => {
    for (const s of ["inbox", "review", "user_test", "waiting", "done", "blocked", "failed", "aborted"] as const) {
      expect(defaultTabFor(s)).toBe("summary");
    }
  });
});

describe("resolveTab", () => {
  const available = ["summary", "comments", "timeline", "history", "thread"] as const;

  it("keeps a valid, available tab from the URL", () => {
    expect(resolveTab("timeline", [...available], "blocked")).toBe("timeline");
  });
  it("falls back to the status default for an unknown tab", () => {
    expect(resolveTab("e2e", [...available], "blocked")).toBe("summary");
    expect(resolveTab("nonsense", [...available], "in_progress")).toBe("comments");
  });
  it("falls back when the tab exists but is not offered for this task", () => {
    expect(resolveTab("workspace", [...available], "done")).toBe("summary");
  });
  it("falls back to the default without a tab", () => {
    expect(resolveTab(null, [...available], "done")).toBe("summary");
  });
});

describe("isTaskTabKey", () => {
  it("rejects the removed E2E tab", () => {
    expect(isTaskTabKey("e2e")).toBe(false);
    expect(isTaskTabKey("summary")).toBe(true);
  });
});
