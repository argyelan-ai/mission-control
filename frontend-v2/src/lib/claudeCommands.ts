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

const DEFAULT_CONTEXT_WINDOW = 200_000;
const MILLION_CONTEXT_WINDOW = 1_000_000;

/**
 * Static context-window size for the context meter, keyed off the
 * transcript's truthful `usage.model` string. Claude model identifiers carry
 * a "[1m]" suffix for the 1M-token beta variants (e.g.
 * "claude-sonnet-4-6[1m]"); every other known/unknown model — including
 * `null` before the first usage event arrives — gets the standard 200k
 * window. Never guesses a bigger window than what's documented.
 */
export function contextWindow(model: string | null | undefined): number {
  if (model?.includes("[1m]")) return MILLION_CONTEXT_WINDOW;
  return DEFAULT_CONTEXT_WINDOW;
}
