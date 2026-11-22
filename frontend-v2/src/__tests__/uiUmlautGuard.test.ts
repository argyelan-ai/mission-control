/**
 * CI guard: no German UI text hard-coded in .tsx (umlauts/ß in JSX text or in
 * ANY string or template literal of the code — attributes, props, ternaries,
 * constants). Runs scripts/check-ui-umlauts.mjs's
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

  // Critic 21.11.: these five got past the first version of the guard.
  it.each([
    ["a literal in a JSX expression", '<span>{"Schließen"}</span>', "Schließen"],
    ["a ternary", '<span>{open ? "Schließen" : "Öffnen"}</span>', "Öffnen"],
    ["a ternary inside an attribute", '<button title={busy ? "Lädt…" : "Go"} />', "Lädt…"],
    ["a constant rendered later", 'const L = "Prüfen";\nexport const X = () => <b>{L}</b>;', "Prüfen"],
    ["any other prop", '<Field label="Größe" />', "Größe"],
    ["a template literal", "const s = `${n} Einträge`;", "Einträge"],
    ["a string inside a template expression", "const s = `${ok ? \"Fertig\" : \"Läuft\"}`;", "Läuft"],
    ["a single-quoted string", "notify('Gespeichert, gelöscht');", "Gespeichert, gelöscht"],
  ])("catches %s", (_name, src, text) => {
    expect(scan(src).map((h) => h.text)).toContain(text);
  });

  it("counts a string once, even when it sits in an attribute", () => {
    expect(scan('<div title="Schließen" />')).toHaveLength(1);
  });

  it("an apostrophe in JSX text does not swallow the next lines", () => {
    const src = ["<p>Don't stop</p>", '<b title="Öffnen" />'].join("\n");
    expect(scan(src).map((h) => h.text)).toEqual(["Öffnen"]);
  });

  it("regexes and comments inside templates are not strings", () => {
    const src = [
      "const r = s.replace(/[`*_]/g, \"\");",
      "const cls = `flex ${",
      "  // Kommentar: hält die Leiste frei",
      "  wide ? \"a\" : \"b\"",
      "}`;",
      "<div className={cls}>Ok</div>",
    ].join("\n");
    expect(scan(src)).toEqual([]);
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
