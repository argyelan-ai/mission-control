/**
 * CI guard: no German UI text hard-coded in .tsx (umlauts/ß in JSX text or in
 * title / aria-label / placeholder / alt). Runs scripts/check-ui-umlauts.mjs's
 * rule inside `npm run test:run`, so CI enforces it. Existing findings are a
 * shrinking baseline (scripts/check-ui-umlauts.baseline.json).
 */
import { describe, it, expect } from "vitest";
import { regressions, scanSource } from "../../../scripts/check-ui-umlauts.mjs";

type Hit = { line: number; kind: string; text: string };
const scan = scanSource as (src: string) => Hit[];

describe("UI umlaut guard", () => {
  it("no .tsx file gained German UI text beyond the baseline", () => {
    const bad = regressions() as Record<string, { now: number; allowed: number; hits: Hit[] }>;
    const report = Object.entries(bad).flatMap(([f, b]) => b.hits.map((h) => `${f}:${h.line} ${h.text}`));
    expect(report).toEqual([]);
  });

  it("catches JSX text and spoken attributes", () => {
    const src = [
      "export function X() {",
      "  return (",
      '    <div title="Schließen" aria-label="Menü öffnen">',
      "      <span>Nicht bestätigt</span>",
      '      <input placeholder={"Suchen…"} />',
      "      <p>",
      "        Verlauf löschen",
      "      </p>",
      "    </div>",
      "  );",
      "}",
    ].join("\n");
    const kinds = scan(src).map((h) => h.text);
    expect(kinds).toEqual(expect.arrayContaining(["Schließen", "Menü öffnen", "Nicht bestätigt", "Verlauf löschen"]));
  });

  it("ignores comments, catalogs-by-key and plain ASCII", () => {
    const src = [
      "// Kommentar mit Umlaut: prüfen",
      "/* Größe */",
      "export const X = () => <span>{t(\"check\")}</span>;",
      '{/* Hinweis für später */}',
      "<b>Check the terminal</b>",
    ].join("\n");
    expect(scan(src)).toEqual([]);
  });
});
