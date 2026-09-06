/**
 * Runtimes-Bühne v2 — i18n-Schlüsselgleichheit (PR 6 "Schliff", Spec §1
 * "UI-Texte sind i18n-Schlüssel, nie hart kodiert").
 *
 * Guards the `runtimes.stage` namespace specifically — the Bühne's own
 * Zonen-Texte (Lauflicht/Lebenszeichen/Instrumente/Mitglieder/Aktionen,
 * Leer/Frei/Blaupause-Zustände) — against EN/DE drift: a key present in one
 * tree but missing in the other renders as the raw dotted key on screen
 * (next-intl's fallback), which reads as broken UI rather than an English
 * leftover.
 */
import { describe, it, expect } from "vitest";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

/** Leaf (path, value) pairs — recurses through every nesting level, not just
 *  the namespace's direct children, so a nested group (e.g. a future
 *  `runtimes.stage.someGroup.label`) is covered exactly like a flat key. */
function flattenEntries(obj: unknown, prefix = ""): Array<[string, unknown]> {
  if (obj == null || typeof obj !== "object") return [[prefix, obj]];
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) =>
    flattenEntries(v, prefix ? `${prefix}.${k}` : k)
  );
}

function flattenKeys(obj: unknown, prefix = ""): string[] {
  return flattenEntries(obj, prefix).map(([k]) => k);
}

describe("runtimes.stage i18n — EN/DE key parity", () => {
  const enStage = (en as Record<string, unknown>).runtimes as Record<string, unknown> | undefined;
  const deStage = (de as Record<string, unknown>).runtimes as Record<string, unknown> | undefined;

  it("both trees define runtimes.stage", () => {
    expect(enStage?.stage).toBeTruthy();
    expect(deStage?.stage).toBeTruthy();
  });

  it("has an identical key set in EN and DE", () => {
    const enKeys = flattenKeys(enStage?.stage).sort();
    const deKeys = flattenKeys(deStage?.stage).sort();
    const missingInDe = enKeys.filter((k) => !deKeys.includes(k));
    const missingInEn = deKeys.filter((k) => !enKeys.includes(k));
    expect(missingInDe).toEqual([]);
    expect(missingInEn).toEqual([]);
  });

  it("no DE value is empty, at any nesting depth", () => {
    const deLeaves = flattenEntries(deStage?.stage);
    for (const [key, value] of deLeaves) {
      expect(typeof value === "string" ? value.trim().length > 0 : true, `runtimes.stage.${key} is empty`).toBe(true);
    }
  });
});
