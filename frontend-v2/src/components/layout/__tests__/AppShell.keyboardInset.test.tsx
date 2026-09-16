/**
 * AppShell-Hoehe vs. Bildschirmtastatur (Defekt 2, Operator-Befund 15.09.2026).
 *
 * `useKeyboardInset` (MOBILE-SPEC M9) stellt seit jeher `--keyboard-inset`
 * bereit — gelesen hat sie niemand. Die Shell hing weiter an `100dvh` und
 * blieb auf voller Fensterhoehe stehen, waehrend das visuelle Viewport um die
 * Tastatur schrumpfte: die Eingabezeile des Threads verschwand darunter.
 * In Chromium 390x844 mit 300px Tastatur nachgemessen: 284px unterhalb der
 * sichtbaren Kante, bei 430x932 waren es 268px.
 *
 * jsdom rechnet kein Layout. Dieser Test prueft deshalb den Vertrag in
 * globals.css — dasselbe Vorgehen wie `ChatView.test.tsx` fuer `.pt-safe-top`.
 * Den Beweis am lebenden Objekt liefert daneben die Browser-Messung.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Kommentare raus: die Begruendung nennt `--keyboard-inset` selbst, und ein
// Test, der auf Kommentartext anschlaegt, prueft die Doku statt die Regel.
const GLOBALS_CSS = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "../../../styles/globals.css"),
  "utf-8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** Regelkoerper der `.app-shell-height`-Deklaration unterhalb eines At-Rules. */
function shellHeightUnder(atRule: string): string | undefined {
  let from = 0;
  for (;;) {
    const start = GLOBALS_CSS.indexOf(atRule, from);
    if (start === -1) return undefined;
    const open = GLOBALS_CSS.indexOf("{", start);
    const close = GLOBALS_CSS.indexOf("\n}", open);
    // `@media (max-width: 767px)` gibt es mehrfach (u. a. die
    // iOS-Zoom-Sperre); gesucht ist der Block, der die Shell wirklich setzt.
    const body = GLOBALS_CSS.slice(open, close);
    if (body.includes(".app-shell-height")) return body;
    from = start + atRule.length;
  }
}

describe("AppShell-Hoehe zieht die Tastatur ab", () => {
  it("verrechnet --keyboard-inset in der Shell-Hoehe", () => {
    // Ohne den Fallback-Wert waere die Hoehe bei fehlender Variable ungueltig
    // und die Shell kollabierte — `0px` haelt den Normalzustand stabil.
    expect(shellHeightUnder("@media (max-width: 767px) {")).toMatch(
      /\.app-shell-height\s*\{[^}]*height:\s*calc\(100dvh\s*-\s*var\(--keyboard-inset,\s*0px\)\)/,
    );
  });

  it("tut dasselbe fuer die installierte PWA, deren Shell an 100lvh haengt", () => {
    const block = shellHeightUnder(
      "@media (max-width: 767px) and (display-mode: standalone)",
    );
    expect(block).toBeDefined();
    expect(block).toMatch(/height:\s*calc\(100lvh\s*-\s*var\(--keyboard-inset,\s*0px\)\)/);
  });

  it("haelt Desktop heraus: keine Inset-Regel ausserhalb einer max-width-767px-Abfrage", () => {
    const hits = [...GLOBALS_CSS.matchAll(/--keyboard-inset/g)];
    expect(hits.length).toBeGreaterThan(0);
    for (const hit of hits) {
      // Naechstes oeffnendes At-Rule oberhalb der Fundstelle. Steht zwischen
      // ihm und dem Fund ein `}`, gehoert die Regel zu keinem Media-Block
      // mehr — dann gilt sie global, auch auf dem Desktop.
      const before = GLOBALS_CSS.slice(0, hit.index);
      const at = before.lastIndexOf("@media");
      expect(at).toBeGreaterThanOrEqual(0);
      const scope = before.slice(at, before.indexOf("\n", at));
      expect(scope).toMatch(/\(max-width:\s*767px\)/);
      expect(before.slice(before.indexOf("{", at)).includes("}")).toBe(false);
    }
  });
});
