/**
 * Round-7 re-check t1 NICE-2: a status step in the timeline read
 * "in progress → inbox · review_rejection_dispatch_blocked" — the raw reason
 * code of the backend. The reason is said in words (UI language); an
 * unknown code still never renders as snake_case.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";
import { TaskTimeline } from "../TaskTimeline";
import type { TaskTimelineEntry } from "@/lib/types";

function step(reason: string | null): TaskTimelineEntry {
  return {
    ts: "2026-10-23T15:15:06Z",
    source: "task_event",
    kind: "status_change",
    title: "review → inbox",
    detail: reason,
    actor: "system",
    meta: { from_status: "review", to_status: "inbox" },
  };
}

describe("status step reason in words", () => {
  it("a known reason code reads as a sentence, not the code", () => {
    render(<TaskTimeline entries={[step("review_rejection_agent_paused")]} isLoading={false} />);
    expect(screen.queryByText("review_rejection_agent_paused")).toBeNull();
    expect(screen.getByText(en.tasks.detail.statusReason.review_rejection_agent_paused)).toBeInTheDocument();
  });

  it("every review-rejection code has words in both languages", () => {
    for (const code of [
      "review_rejection_redispatch",
      "review_rejection_queued",
      "review_rejection_no_developer",
      "review_rejection_dispatch_blocked",
      "review_rejection_agent_paused",
      "review_approved",
    ]) {
      const enWords = (en.tasks.detail.statusReason as Record<string, string>)[code];
      const deWords = (de.tasks.detail.statusReason as Record<string, string>)[code];
      expect(enWords, code).toBeTruthy();
      expect(deWords, code).toBeTruthy();
      expect(deWords).not.toBe(enWords);
    }
  });

  it("an unknown code is humanized, never shown raw", () => {
    render(<TaskTimeline entries={[step("some_new_reason")]} isLoading={false} />);
    expect(screen.queryByText("some_new_reason")).toBeNull();
    expect(screen.getByText("some new reason")).toBeInTheDocument();
  });

  it("a free-text reason stays as it is", () => {
    render(<TaskTimeline entries={[step("Operator: wrong file")]} isLoading={false} />);
    expect(screen.getByText("Operator: wrong file")).toBeInTheDocument();
  });
});
