import { describe, it, expect } from "vitest";
import { parseDocOutline } from "./docOutline";

/** Gekürzt, aber Struktur 1:1 aus einem echten Lauf (22.08.2026) — inklusive
 *  des Vorspann-Zitats, das die Engine ins Gerüst schreibt. */
const ECHTES_DOKUMENT = `# Kürze-Probe: Ergebnis-Dokument im Handy-Panel

> Lebendes Ergebnis-Dokument dieser Gruppe. Es schreibt NUR der Lead-Agent —
> im Synthese-Turn am Ende jeder Runde.

## Ziel

Wie sollte das Ergebnis-Dokument auf dem Handy dargestellt werden?

## Stand nach Runde 1

**Empfehlung: Kopfzone mit Verdikt + darunter aufklappbare Abschnitte.**

### Beiträge

**@researcher:** Progressive Disclosure senkt die kognitive Last.

## Dissens-Protokoll

Kein offener Dissens.
`;

describe("parseDocOutline", () => {
  it("nimmt die Überschrift und wirft den Vorspann nicht weg, zeigt ihn aber getrennt", () => {
    // Das Zitat ist eine Anweisung an den Lead-Agenten, kein Text für den
    // Leser. Es stand bisher zuoberst im Panel und schob die eigentliche
    // Antwort unter die Falz (Operator-Befund 22.08.2026). Es verschwindet
    // aus dem Fluss — verloren geht es nicht.
    const o = parseDocOutline(ECHTES_DOKUMENT);
    expect(o.title).toBe("Kürze-Probe: Ergebnis-Dokument im Handy-Panel");
    expect(o.note).toContain("Lead-Agent");
    expect(o.sections.some((s) => s.body.includes("Lebendes Ergebnis-Dokument"))).toBe(false);
  });

  it("holt das Verdikt aus dem ersten Abschnitt, der nicht das Ziel ist", () => {
    const o = parseDocOutline(ECHTES_DOKUMENT);
    expect(o.lead).toContain("Kopfzone mit Verdikt");
    expect(o.lead).not.toContain("**"); // fürs Auge geputzt, nicht roh
  });

  it("stellt das Ziel ans Ende — der Leser hat es selbst getippt", () => {
    const o = parseDocOutline(ECHTES_DOKUMENT);
    expect(o.sections.map((s) => s.title)).toEqual([
      "Stand nach Runde 1",
      "Dissens-Protokoll",
      "Ziel",
    ]);
    expect(o.sections.find((s) => s.title === "Ziel")?.isGoal).toBe(true);
  });

  it("gibt jedem Abschnitt eine Vorschauzeile ohne Markdown-Zeichen", () => {
    const o = parseDocOutline(ECHTES_DOKUMENT);
    const stand = o.sections.find((s) => s.title === "Stand nach Runde 1")!;
    expect(stand.preview).toContain("Empfehlung: Kopfzone mit Verdikt");
    expect(stand.preview).not.toContain("**");
  });

  it("lässt Unterabschnitte im Abschnitt statt sie zu Einträgen zu machen", () => {
    // `###` gehört zum Inhalt seines `##`. Zöge man es hoch, zerfiele ein
    // Abschnitt in Bruchstücke, die einzeln nichts aussagen.
    const o = parseDocOutline(ECHTES_DOKUMENT);
    const stand = o.sections.find((s) => s.title === "Stand nach Runde 1")!;
    expect(stand.body).toContain("### Beiträge");
    expect(o.sections.some((s) => s.title === "Beiträge")).toBe(false);
  });

  it("hält Rauten in Code-Blöcken für Code, nicht für Überschriften", () => {
    // Sonst zerschneidet ein Shell-Kommentar das Dokument an einer Stelle,
    // an der gar keine Überschrift steht.
    const o = parseDocOutline(
      "# T\n\n## Eins\n\n```bash\n## kein Abschnitt\necho hi\n```\n\nText.\n",
    );
    expect(o.sections.map((s) => s.title)).toEqual(["Eins"]);
    expect(o.sections[0].body).toContain("## kein Abschnitt");
  });

  it("meldet ein leeres Dokument als leer statt einen leeren Rahmen zu bauen", () => {
    expect(parseDocOutline("").empty).toBe(true);
    expect(parseDocOutline("   \n\n").empty).toBe(true);
  });

  it("kommt ohne Überschriften aus — dann ist der ganze Text ein Abschnitt", () => {
    const o = parseDocOutline("Nur ein Absatz ohne jede Struktur.");
    expect(o.empty).toBe(false);
    expect(o.sections).toHaveLength(1);
    expect(o.sections[0].body).toContain("Nur ein Absatz");
  });
});
