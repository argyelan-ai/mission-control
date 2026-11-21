import { describe, it, expect } from "vitest";
import { isOperatorEvent } from "../operatorEvents";

describe("isOperatorEvent — Home leaves plumbing out", () => {
  it.each([
    ["task.dispatch_attempt_rotated", "Silent retry: dispatch_attempt_id rotiert für 'X' nach 6min ohne ACK"],
    ["task.stale_update_rejected", "Stale Update von A auf 'X' abgelehnt (falsche dispatch_attempt_id)"],
    ["task.stale_update_warning", "Stale Update …"],
    ["task.missing_dispatch_attempt_id", "…"],
    ["task.some_new_type", "Something with dispatch_attempt_id inside"],
  ])("hides %s", (event_type, title) => {
    expect(isOperatorEvent({ event_type, title })).toBe(false);
  });

  it.each([
    ["task.done", "Task 'Write docs' done"],
    ["task.dispatch_escalated", "Dispatch escalation: 'X'"],
    ["agent.online", "Builder is online"],
  ])("keeps %s", (event_type, title) => {
    expect(isOperatorEvent({ event_type, title })).toBe(true);
  });
});
