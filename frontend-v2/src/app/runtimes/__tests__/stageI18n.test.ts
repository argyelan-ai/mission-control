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

function flattenKeys(obj: unknown, prefix = ""): string[] {
  if (obj == null || typeof obj !== "object") return [prefix];
  return Object.entries(obj as Record<string, unknown>).flatMap(([k, v]) =>
    flattenKeys(v, prefix ? `${prefix}.${k}` : k)
  );
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

  it("no DE value is empty", () => {
    const deValues = Object.entries(deStage?.stage as Record<string, unknown>);
    for (const [key, value] of deValues) {
      expect(typeof value === "string" ? value.trim().length > 0 : true, `runtimes.stage.${key} is empty`).toBe(true);
    }
  });
});
