/**
 * Zaun gegen fest verdrahtete Bedienoberflächen-Texte im Chat-Bereich.
 *
 * `docs/i18n.md` verlangt `t()` für neue UI-Texte. Der Befund aus dem Review
 * zu PR #331 war genau das: `aria-label="Detailgrad"` stand deutsch in der
 * englischen Standard-Oberfläche. Ein Test, der nur die Labels selbst prüft,
 * fängt den nächsten Rückfall nicht — dieser hier prüft die REGEL.
 *
 * Zwei Zusicherungen pro Datei:
 *  1. keine Zeichenketten-Literale in `aria-label` / `title` / `placeholder`
 *     / `alt` — dort gehört `{t("…")}` hin;
 *  2. kein Umlaut ausserhalb von Kommentaren — die Kommentare dieses
 *     Bereichs schreiben ohnehin `ae/oe/ue`, deutscher Text im Code fällt
 *     damit sofort auf.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));

// Die vier Bedienflächen, die PR #331 angefasst hat, plus die gemeinsame
// Label-Quelle beider Detailgrad-Schalter.
const FILES = [
  "ChatView.tsx",
  "Composer.tsx",
  "ChatOptionsSheet.tsx",
  // Die Fehlerkarte der ACP-Agenten: sie zeigt ihren Text ausschliesslich
  // ueber `chat.error.*` (Spec docs/specs/chat-over-acp.md).
  "ChatErrorCard.tsx",
  "AgentCard.tsx",
  "NotificationRow.tsx",
  "chatOptions.ts",
  "../layout/AppShell.tsx",
];

function read(rel: string): string {
  return readFileSync(join(HERE, rel), "utf-8");
}

/** Blockkommentare und ganze Kommentarzeilen raus — der Rest ist Code. */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^[ \t]*\/\/.*$/gm, "");
}

describe("Chat-Oberfläche — keine fest verdrahteten Texte", () => {
  for (const rel of FILES) {
    it(`${rel}: aria-label/title/placeholder kommen aus t()`, () => {
      const code = stripComments(read(rel));
      const literals = [...code.matchAll(/\b(aria-label|title|placeholder|alt)="([^"]*[A-Za-z][^"]*)"/g)]
        .map((m) => `${m[1]}="${m[2]}"`);
      expect(literals).toEqual([]);
    });

    it(`${rel}: kein deutscher Text im Code`, () => {
      const code = stripComments(read(rel));
      const german = code
        .split("\n")
        .map((line, i) => ({ line, no: i + 1 }))
        .filter(({ line }) => /[äöüÄÖÜß]/.test(line))
        .map(({ line, no }) => `${no}: ${line.trim()}`);
      expect(german).toEqual([]);
    });
  }
});

/**
 * `chat.error.*` — EN/DE-Schluesselgleichheit.
 *
 * Die Fehlerkarte schlaegt `chat.error.<code>` nach und faellt auf
 * `chat.error.generic` zurueck. Fehlt ein Schluessel in nur EINEM Baum, steht
 * auf Deutsch der rohe Punkt-Pfad im Chat (next-intl-Rueckfall) — das liest
 * sich als kaputte Oberflaeche, nicht als englischer Rest.
 */
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";

const ERROR_CODES = [
  "rpc_error",
  "provider_error",
  "empty_turn",
  "process_exit",
  "busy",
  "session_reset",
  "generic",
];

function errorNs(tree: unknown): Record<string, unknown> {
  const chat = (tree as Record<string, unknown>).chat as Record<string, unknown> | undefined;
  return (chat?.error as Record<string, unknown>) ?? {};
}

describe("chat.error — EN/DE", () => {
  it("defines every code the chat daemon can emit, in both locales", () => {
    for (const code of ERROR_CODES) {
      expect(typeof errorNs(en)[code], `chat.error.${code} missing in EN`).toBe("string");
      expect(typeof errorNs(de)[code], `chat.error.${code} missing in DE`).toBe("string");
    }
  });

  it("has an identical key set and no empty value", () => {
    expect(Object.keys(errorNs(en)).sort()).toEqual(Object.keys(errorNs(de)).sort());
    for (const [key, value] of Object.entries(errorNs(de))) {
      expect(typeof value === "string" && value.trim().length > 0, `chat.error.${key} is empty`).toBe(true);
    }
  });
});
