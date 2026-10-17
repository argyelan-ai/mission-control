import { describe, expect, it } from "vitest";
import { pickLocale } from "../request";

// First-visit language preselection (PR 1-5): the stored cookie choice
// always wins; only without a cookie does the browser's Accept-Language
// header decide; anything unmatched falls back to the English default.
describe("pickLocale", () => {
  it("uses the saved cookie choice over the browser language", () => {
    expect(pickLocale("de", "en-US,en;q=0.9")).toBe("de");
  });

  it("falls back to Accept-Language when there is no saved cookie", () => {
    expect(pickLocale(undefined, "de-CH,de;q=0.9,en;q=0.8")).toBe("de");
  });

  it("matches a region-qualified tag to its base language", () => {
    expect(pickLocale(undefined, "de-DE,de;q=0.9")).toBe("de");
  });

  it("picks the first supported language in a multi-value header", () => {
    expect(pickLocale(undefined, "fr-FR,fr;q=0.9,de;q=0.8,en;q=0.5")).toBe(
      "de",
    );
  });

  it("defaults to English when nothing matches", () => {
    expect(pickLocale(undefined, "fr-FR,fr;q=0.9")).toBe("en");
  });

  it("defaults to English when there is no header at all", () => {
    expect(pickLocale(undefined, null)).toBe("en");
  });

  it("ignores an invalid cookie value and falls back to the header", () => {
    expect(pickLocale("xx", "de-CH,de;q=0.9")).toBe("de");
  });
});
