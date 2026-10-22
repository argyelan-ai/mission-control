/**
 * The effort chip's reason texts exist in BOTH catalogs — a German operator
 * must never get the key or an English sentence for a locked chip
 * (sessions.effortLocked*, Hermes fix 22.10.2026).
 */
import { describe, it, expect } from "vitest";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";

type Catalog = { sessions: Record<string, unknown> };
const lockedKeys = (c: Catalog) =>
  Object.keys(c.sessions).filter((k) => k.startsWith("effortLocked")).sort();

describe("sessions.effortLocked* catalog", () => {
  it("German and English have exactly the same reason keys", () => {
    expect(lockedKeys(de as Catalog)).toEqual(lockedKeys(en as Catalog));
  });

  it("covers the headless-agent reasons in both languages", () => {
    for (const cat of [en, de] as Catalog[]) {
      for (const key of ["effortLockedStateMissing", "effortLockedHarnessNoEffort", "effortLockedHarnessNoEffortGeneric"]) {
        expect(typeof cat.sessions[key]).toBe("string");
        expect((cat.sessions[key] as string).length).toBeGreaterThan(0);
      }
      expect(cat.sessions.effortLockedHarnessNoEffort).toContain("{harness}");
    }
    expect((de as Catalog).sessions.effortLockedHarnessNoEffort).toBe("{harness} bietet keine Denkstufe an.");
  });
});
