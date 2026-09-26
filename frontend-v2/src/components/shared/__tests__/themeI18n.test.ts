import { describe, expect, it } from "vitest";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

// The theme switch is labelled in both UI languages with the same keys.
describe("theme labels (EN/DE)", () => {
  it("both catalogs carry the same theme keys, all non-empty", () => {
    const e: Record<string, string> = en.theme;
    const d: Record<string, string> = de.theme;
    expect(Object.keys(e).sort()).toEqual(Object.keys(d).sort());
    for (const k of Object.keys(e)) {
      expect(e[k], `en.theme.${k}`).toBeTruthy();
      expect(d[k], `de.theme.${k}`).toBeTruthy();
    }
    expect(en.settings.sections.appearance).toBe("Appearance");
    expect(de.settings.sections.appearance).toBe("Darstellung");
  });
});
