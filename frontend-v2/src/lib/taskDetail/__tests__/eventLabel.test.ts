import { describe, it, expect } from "vitest";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";
import { eventLabel, eventSlug } from "../eventLabel";

function translator(messages: Record<string, unknown>) {
  const lookup = (key: string): unknown =>
    key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown> | undefined)?.[part], messages);
  const t = (key: string) => String(lookup(key));
  return Object.assign(t, { has: (key: string) => typeof lookup(key) === "string" });
}

describe("eventLabel", () => {
  it("uses the i18n label for known friction, decision and timeline types", () => {
    const tDe = translator(de.tasks as Record<string, unknown>);
    expect(eventLabel(tDe, "blocker.escalated_to_operator")).toBe("An Operator eskaliert");
    expect(eventLabel(tDe, "task.stale_update_rejected")).toBe("Veraltetes Update abgelehnt");
    expect(eventLabel(tDe, "visual_review")).toBe("Visuelle Prüfung");
  });

  it("falls back to the humanized key for an unknown type", () => {
    const tEn = translator(en.tasks as Record<string, unknown>);
    expect(eventLabel(tEn, "task.brand_new_thing")).toBe("Brand new thing");
  });

  it("slugs dotted types without nesting", () => {
    expect(eventSlug("task.blocked_reminder")).toBe("blocked_reminder");
    expect(eventSlug("review.self_review_escalated")).toBe("review_self_review_escalated");
  });

  it("has the same event keys in EN and DE", () => {
    expect(Object.keys(de.tasks.detail.event).sort()).toEqual(Object.keys(en.tasks.detail.event).sort());
  });
});
