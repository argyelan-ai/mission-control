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
  // Die Diff-Ansicht im Chat (Operator-Befund 04.10.2026: „Arbeitsstand",
  // „Letzter Commit", „Kein Workspace" standen deutsch in der englischen
  // Oberflaeche — die Datei stand nie auf dieser Liste) samt Panel-Schiene,
  // Sessions-Seite und der geteilten Diff-Darstellung.
  "DiffPanel.tsx",
  "PanelRail.tsx",
  "../git/GitDiffView.tsx",
  "../../app/sessions/page.tsx",
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

    // Der Umlaut-Test allein liess „Arbeitsstand" und „Kein Workspace"
    // durch. Diese zwei Muster fangen Text, der als JSX-Knoten oder als
    // `label: "…"` im Code steht. Erlaubt bleiben Maschinen-Kuerzel ohne
    // Kleinbuchstaben (Git-Status „A"/„M", Datei-Marke „IMG").
    it(`${rel}: kein Wort als JSX-Text oder label-Literal`, () => {
      const code = stripComments(read(rel));
      const jsxText = [...code.matchAll(/>\s*([A-Za-zÄÖÜäöüß][^<>{}=;()&|]*?)\s*<\//g)]
        .map((m) => m[1])
        .filter((txt) => /[a-zäöüß]/.test(txt));
      const labels = [...code.matchAll(/\blabel:\s*"([^"]*[a-zäöüß][^"]*)"/g)].map((m) => m[1]);
      expect([...jsxText, ...labels]).toEqual([]);
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

/**
 * Diff-Ansicht, Panel-Schiene, geteilte Diff-Darstellung — EN/DE gleich.
 * Ein Schluessel nur in EN zeigte auf Deutsch den rohen Punkt-Pfad.
 */
function flatten(tree: unknown, prefix = ""): Record<string, unknown> {
  if (typeof tree !== "object" || tree === null) return { [prefix]: tree };
  return Object.entries(tree as Record<string, unknown>).reduce<Record<string, unknown>>(
    (acc, [k, v]) => ({ ...acc, ...flatten(v, prefix ? `${prefix}.${k}` : k) }),
    {},
  );
}

function pick(tree: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((cur, p) => (cur as Record<string, unknown> | undefined)?.[p], tree);
}

describe("sessions.diff / sessions.panels / gitDiff — EN/DE", () => {
  for (const ns of ["sessions.diff", "sessions.panels", "gitDiff"]) {
    it(`${ns}: identical keys, no empty value`, () => {
      const enKeys = flatten(pick(en, ns));
      const deKeys = flatten(pick(de, ns));
      expect(Object.keys(enKeys).length).toBeGreaterThan(0);
      expect(Object.keys(deKeys).sort()).toEqual(Object.keys(enKeys).sort());
      for (const [k, v] of Object.entries(deKeys)) {
        expect(typeof v === "string" && v.trim().length > 0, `${ns}.${k} empty in DE`).toBe(true);
      }
    });
  }

  it("every source kind the backend sends has a label", () => {
    for (const kind of ["task", "session", "recent"]) {
      expect(typeof pick(en, `sessions.diff.kind.${kind}`)).toBe("string");
      expect(typeof pick(de, `sessions.diff.kind.${kind}`)).toBe("string");
    }
  });
});
