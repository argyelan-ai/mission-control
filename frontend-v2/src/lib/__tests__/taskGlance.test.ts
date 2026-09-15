import { describe, expect, it } from "vitest";
import { plainSummary, humanStatus, silenceHours } from "../taskGlance";

describe("plainSummary", () => {
  it("returns the first paragraph without markdown headings", () => {
    const md = "## Ziel\nDie Sperre verhindert Doppel-Dispatch.\n\n## Details\nViel Technik.";
    expect(plainSummary(md)).toBe("Die Sperre verhindert Doppel-Dispatch.");
  });

  it("strips inline code and links but keeps the words", () => {
    expect(plainSummary("Nutze `transition()` und [ADR-081](x).")).toBe("Nutze transition() und ADR-081.");
  });

  it("caps at 220 characters on a word boundary with an ellipsis", () => {
    const long = Array(60).fill("wort").join(" ");
    const out = plainSummary(long);
    expect(out.length).toBeLessThanOrEqual(221);
    expect(out.endsWith("…")).toBe(true);
    // Cut on a word boundary: every token before the ellipsis is a whole word.
    expect(out.slice(0, -1).split(" ").every((w) => w === "wort")).toBe(true);
  });

  it("returns empty string for empty or null description", () => {
    expect(plainSummary(null)).toBe("");
    expect(plainSummary("   \n\n")).toBe("");
  });
});

describe("humanStatus", () => {
  it("says the agent is waiting for the operator when status is waiting", () => {
    expect(humanStatus({ status: "waiting" })).toEqual({ key: "glanceWaitingForYou", tone: "warning" });
  });

  it("says the agent is working when in_progress", () => {
    expect(humanStatus({ status: "in_progress" })).toEqual({ key: "glanceWorking", tone: "info" });
  });

  it("asks for acceptance when in review or user_test", () => {
    expect(humanStatus({ status: "review" })).toEqual({ key: "glanceNeedsAcceptance", tone: "warning" });
    expect(humanStatus({ status: "user_test" })).toEqual({ key: "glanceNeedsAcceptance", tone: "warning" });
  });

  it("flags blocked/failed/aborted as needing attention", () => {
    expect(humanStatus({ status: "blocked" })).toEqual({ key: "glanceStuck", tone: "error" });
    expect(humanStatus({ status: "failed" })).toEqual({ key: "glanceFailed", tone: "error" });
    expect(humanStatus({ status: "aborted" })).toEqual({ key: "glanceAborted", tone: "muted" });
  });

  it("is quiet for inbox and done", () => {
    expect(humanStatus({ status: "inbox" })).toEqual({ key: "glanceNotStarted", tone: "muted" });
    expect(humanStatus({ status: "done" })).toEqual({ key: "glanceDone", tone: "online" });
  });
});

describe("silenceHours", () => {
  const now = new Date("2026-09-11T18:00:00Z");

  it("returns whole hours since the last activity", () => {
    expect(silenceHours("2026-09-11T09:30:00Z", now)).toBe(8);
  });

  it("returns 0 for activity less than an hour ago", () => {
    expect(silenceHours("2026-09-11T17:40:00Z", now)).toBe(0);
  });

  it("returns null without a timestamp", () => {
    expect(silenceHours(null, now)).toBeNull();
  });
});
