/**
 * Phase 6 ActivityFeed event-type wiring contract test (Plan 06-06).
 *
 * Asserts that the four new Phase 6 backend audit event types render with the
 * correct StatusDot color per UI-SPEC §1 visual contract (lines 150-165 of
 * 06-UI-SPEC.md):
 *
 *   agent.compaction              -> warning (#A67F3E)
 *   agent.recovery_started        -> warning (#A67F3E)
 *   agent.recovery_tier_complete  -> online  (#55A964)
 *   agent.recovery_failed         -> error   (#FA4942)
 *
 * StatusDot.tsx renders the status color via inline `backgroundColor` style on
 * a child <span>, so we assert against `style.backgroundColor` (computed as
 * `rgb(...)` by jsdom) rather than a class name. Mapping:
 *   warning #A67F3E -> rgb(166, 127, 62)   (C.warning from colors.ts)
 *   online  #55A964 -> rgb(85, 169, 100)   (C.online from colors.ts)
 *   error   #FA4942 -> rgb(250, 73, 66)    (C.error from colors.ts)
 *
 * Note: Token values migrated again in v4 („Signal"): the status hues are now
 * the ONLY chromatic tokens in the app, and error outranks warning by chroma
 * (C .215 vs .095) instead of by brightness. These are the actual runtime
 * colors StatusDot renders.
 */
import { render } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ActivityFeed } from "@/components/shared/ActivityFeed";

const STATUS_TO_RGB: Record<string, string> = {
  warning: "rgb(166, 127, 62)",  // C.warning = #A67F3E
  online: "rgb(85, 169, 100)",   // C.online  = #55A964
  error: "rgb(250, 73, 66)",     // C.error   = #FA4942
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
