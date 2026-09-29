/**
 * Settings → API keys: every key the section asks for exists in EN and DE.
 *
 * `t("cancel")` in the API-keys section had no catalog entry, so the button
 * rendered the raw key "settings.apikeys.cancel" instead of a label.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

const SRC = readFileSync(join(__dirname, "../page.tsx"), "utf-8");

function sectionKeys(): string[] {
  const start = SRC.indexOf('useTranslations("settings.apikeys")');
  expect(start).toBeGreaterThan(-1);
  // The section ends where the next top-level component starts.
  const end = SRC.slice(start).search(/\n(export )?(default )?function /) + start;
  const body = SRC.slice(start, end > start ? end : undefined);
  const keys = new Set<string>();
  for (const m of body.matchAll(/[^\w.]t\("([\w.]+)"/g)) keys.add(m[1]);
  return [...keys];
}

function has(catalog: unknown, path: string): boolean {
  let cur: unknown = catalog;
  for (const p of ["settings", "apikeys", ...path.split(".")]) {
    if (typeof cur !== "object" || cur === null) return false;
    cur = (cur as Record<string, unknown>)[p];
  }
  return typeof cur === "string";
}

describe("settings.apikeys catalog", () => {
  const keys = sectionKeys();

  it("finds the section's keys (guards the scanner itself)", () => {
    expect(keys).toContain("cancel");
    expect(keys.length).toBeGreaterThan(5);
  });

  it.each(["en", "de"] as const)("%s has every key the section uses", (lang) => {
    const catalog = lang === "en" ? en : de;
    const missing = keys.filter((k) => !has(catalog, k));
    expect(missing).toEqual([]);
  });
});
