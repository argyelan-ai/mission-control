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
 *
 * Lücke gefunden bei der ContextPanel-i18n-Migration (Okt 2026): die Mehrzahl
 * der dort fest verdrahteten Wörter ("Eingabe", "Kontext", "Schliessen",
 * "Quelle", "Frei", "Belegt", "Fenster gesamt", "unbekannt" — und in
 * claudeCommands.ts "Modell wechseln", "Kontext komprimieren" etc.) hat GAR
 * KEINEN Umlaut, die Sabotage-Probe mit "Schliessen" lief durch Zusicherung 2
 * also unbemerkt durch. Zusicherung 3 unten schliesst diese Lücke gezielt für
 * genau die Woerter, die in diesem Fund zurueckfielen — kein allgemeiner
 * Woerterbuch-Scan (zu viele False-Positives in TSX mit Generics), sondern
 * die konkreten Strings dieses Rueckfalls, dauerhaft verboten.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { SLASH_COMMANDS } from "../../lib/claudeCommands";

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
  // The head-chat read-only view (ADR-085 §4 Nachtrag) made ToolGroup part
  // of the frozen Chat surface's own exception, so its summary line is no
  // longer a one-off — review finding on PR #756 round 2: it rendered
  // hardcoded German ("5 Tools verwendet, 2× nachgedacht") inside every
  // transcript, agent and head alike, in the otherwise-English UI.
  "ToolGroup.tsx",
  // Die Diff-Ansicht im Chat (Operator-Befund 04.10.2026: „Arbeitsstand",
  // „Letzter Commit", „Kein Workspace" standen deutsch in der englischen
  // Oberflaeche — die Datei stand nie auf dieser Liste) samt Panel-Schiene,
  // Sessions-Seite und der geteilten Diff-Darstellung.
  "DiffPanel.tsx",
  "PanelRail.tsx",
  "../git/GitDiffView.tsx",
  "../../app/sessions/page.tsx",
  // Das Kontext-Panel hinter dem Ring im Composer: stand bis zum Fund fest
  // deutsch ("Eingabe", "Frei", "Schliessen", ...) in der sonst zweisprachigen
  // Oberflaeche.
  "ContextPanel.tsx",
  // claudeCommands.ts (die statische Slash-Kommando-Liste und das
  // CLAUDE_MODELS-Array, die der Composer fuer "/"-Palette + Modell-Switcher
  // einbindet) steht bewusst NICHT in dieser Liste: CLAUDE_MODELS trägt
  // `label: "Opus"/"Sonnet"/"Haiku"/"Default"` — Markennamen, in beiden
  // Sprachen identisch (wie "CLI"), kein Rueckfall. Die generische
  // `label:`-Zusicherung oben kann Markennamen nicht von echten
  // deutschen Labels unterscheiden; claudeCommands.ts bekommt dafuer die
  // praeziseren, dediziert gebauten Zusicherungen weiter unten
  // (Rueckfall-Strings + descriptionKey/description-Form).
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
 * Konkrete Rückfall-Woerter des ContextPanel-/claudeCommands-Funds (Okt
 * 2026) — siehe Datei-Kopfkommentar. Umlaut-frei, darum eine eigene Liste
 * statt sich auf Zusicherung 2 oben zu verlassen.
 */
const REGRESSION_STRINGS: Record<string, string[]> = {
  "ContextPanel.tsx": [
    "Eingabe",
    "Cache gelesen",
    "Cache geschrieben",
    "Ausgabe",
    "Belegt",
    "Frei",
    "Kontext",
    "Fenster gesamt",
    "unbekannt",
    "Quelle",
    "Schliessen",
    "Statuszeile",
  ],
  "../../lib/claudeCommands.ts": [
    "Modell wechseln",
    "Verlauf löschen",
    "Kontext komprimieren",
    "Kontext-Nutzung anzeigen",
    "Session-Status anzeigen",
    "Hilfe anzeigen",
  ],
};

describe("ContextPanel / claudeCommands — Rückfall-Strings bleiben draussen", () => {
  for (const [rel, strings] of Object.entries(REGRESSION_STRINGS)) {
    it(`${rel}: keines der frueher fest verdrahteten Woerter ist zurueck`, () => {
      const code = stripComments(read(rel));
      const found = strings.filter((s) => code.includes(s));
      expect(found).toEqual([]);
    });
  }
});

/**
 * claudeCommands.ts — die statische Slash-Liste traegt einen i18n-Schluessel,
 * nie einen rohen Text. Der Fund war genau das: `description: "Modell
 * wechseln"` statt `descriptionKey: "model"`. Diese Zusicherung prueft die
 * FORM (Schluessel vorhanden, kein roher Text), unabhaengig von der Sprache —
 * ein englischer Rueckfall ("description: 'Switch model'") faellt genauso
 * durch wie ein deutscher.
 */
describe("claudeCommands — statische Slash-Liste trägt descriptionKey, nie Text", () => {
  it("jeder statische Eintrag hat descriptionKey und keine literale description", () => {
    for (const cmd of SLASH_COMMANDS) {
      expect(cmd.descriptionKey, `${cmd.command} hat keinen descriptionKey`).toBeTruthy();
      expect(cmd.description, `${cmd.command} darf keine literale description tragen`).toBeUndefined();
    }
  });
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
