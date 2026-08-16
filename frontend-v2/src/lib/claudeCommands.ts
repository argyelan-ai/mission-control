/**
 * Static catalogue for the Sessions Chat Composer (Task B4) — the model
 * switcher chip and the "/" slash-command palette. These are fixed
 * identifiers passed straight through as chat text (e.g. "/model sonnet");
 * there is no discovery call, since the CLI on the other end already
 * understands them.
 */

export interface ClaudeModel {
  /** Argument passed to `/model <name>` — matches Claude Code's own CLI. */
  name: string;
  label: string;
}

export const CLAUDE_MODELS: ClaudeModel[] = [
  { name: "opus", label: "Opus" },
  { name: "sonnet", label: "Sonnet" },
  { name: "haiku", label: "Haiku" },
  { name: "default", label: "Default" },
];

export interface SlashCommand {
  command: string;
  description: string;
}

/** Listed in the "/" palette. Anything the user types that isn't one of
 *  these passes through as free text — the palette is a shortcut, not a
 *  gate. */
export const SLASH_COMMANDS: SlashCommand[] = [
  { command: "/model", description: "Modell wechseln" },
  { command: "/clear", description: "Verlauf löschen" },
  { command: "/compact", description: "Kontext komprimieren" },
  { command: "/context", description: "Kontext-Nutzung anzeigen" },
  { command: "/status", description: "Session-Status anzeigen" },
  { command: "/help", description: "Hilfe anzeigen" },
];

/**
 * There is deliberately no model→context-window map here. Two fix rounds in
 * a row shipped a wrong one (first missing the Claude 5 family's 1M default,
 * then still guessing for models this frontend doesn't know about) — model
 * lineups change faster than this file gets reviewed. The backend stamps the
 * real window straight onto `UsageEvent.contextWindow` from its own model
 * registry; the Composer renders the meter only when that field is a
 * positive number, and renders nothing otherwise. See `chatTypes.ts`.
 */

/** Compact "153k" / "1M" formatting for the context-meter label — exact
 *  numbers still go in the tooltip, this is just the at-a-glance figure. */
export function formatCompactTokens(tokens: number): string {
  if (tokens >= 1_000_000) {
    const millions = tokens / 1_000_000;
    return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`;
  }
  return `${Math.round(tokens / 1000)}k`;
}
