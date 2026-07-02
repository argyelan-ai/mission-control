/**
 * Phase 6 ActivityFeed event-type wiring contract test (Plan 06-06).
 *
 * Asserts that the four new Phase 6 backend audit event types render with the
 * correct StatusDot color per UI-SPEC §1 visual contract (lines 150-165 of
 * 06-UI-SPEC.md):
 *
 *   agent.compaction              -> warning (#B8870A)
 *   agent.recovery_started        -> warning (#B8870A)
 *   agent.recovery_tier_complete  -> online  (#2B9A4A)
 *   agent.recovery_failed         -> error   (#C23838)
 *
 * StatusDot.tsx renders the status color via inline `backgroundColor` style on
 * a child <span>, so we assert against `style.backgroundColor` (computed as
 * `rgb(...)` by jsdom) rather than a class name. Mapping:
 *   warning #B8870A -> rgb(184, 135, 10)   (C.warning from colors.ts)
 *   online  #2B9A4A -> rgb(43, 154, 74)    (C.online from colors.ts)
 *   error   #C23838 -> rgb(194, 56, 56)    (C.error from colors.ts)
 *
 * Note: Token values migrated from old Tailwind bright palette to the
 * desaturated MC-Teal design system (C.warning/#B8870A, C.online/#2B9A4A,
 * C.error/#C23838). These are the actual runtime colors StatusDot renders.
 */
import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ActivityFeed } from "@/components/shared/ActivityFeed";

const STATUS_TO_RGB: Record<string, string> = {
  warning: "rgb(184, 135, 10)",  // C.warning = #B8870A
  online: "rgb(43, 154, 74)",    // C.online  = #2B9A4A
  error: "rgb(194, 56, 56)",     // C.error   = #C23838
};

describe("ActivityFeed Phase 6 events", () => {
  // Per UI-SPEC §1 — eventType -> StatusDot status mapping contract
  it.each([
    ["agent.compaction", "warning"],
    ["agent.recovery_started", "warning"],
    ["agent.recovery_tier_complete", "online"],
    ["agent.recovery_failed", "error"],
  ])(
    "maps event_type=%s to StatusDot status=%s",
    (eventType: string, expectedStatus: string) => {
      const event = {
        id: "test-1",
        title: `Test ${eventType}`,
        event_type: eventType,
        created_at: new Date().toISOString(),
      };
      const { container } = render(<ActivityFeed events={[event]} />);

      // StatusDot renders a wrapper <span> with a child <span> that carries
      // the status color via inline `backgroundColor`. Find the colored child
      // and assert its rgb matches the expected status color.
      const colorSpans = container.querySelectorAll("span[style*='background-color']");
      const expectedRgb = STATUS_TO_RGB[expectedStatus];
      const matched = Array.from(colorSpans).some(
        (el) => (el as HTMLElement).style.backgroundColor === expectedRgb,
      );
      expect(matched).toBe(true);
    },
  );
});
