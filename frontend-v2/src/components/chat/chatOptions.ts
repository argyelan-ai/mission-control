/**
 * The chat view's two display options, split out of ChatView so both the
 * desktop header and the mobile options sheet can import them without a
 * module cycle (ChatView renders the sheet, so the sheet must not import
 * ChatView). ChatView re-exports all four names — existing importers
 * (sessions/page.tsx, tests) keep working unchanged.
 */

/** How much of the transcript's machinery to show. */
export type DetailLevel = "compact" | "normal" | "verbose";

export const DETAIL_LEVELS: { key: DetailLevel; label: string }[] = [
  { key: "compact", label: "Kompakt" },
  { key: "normal", label: "Normal" },
  { key: "verbose", label: "Ausführlich" },
];

/** What occupies the center column: the parsed chat, or the raw tmux pane. */
export type CenterView = "chat" | "terminal";

export const CENTER_VIEWS: { key: CenterView; label: string }[] = [
  { key: "chat", label: "Chat" },
  { key: "terminal", label: "Terminal" },
];
