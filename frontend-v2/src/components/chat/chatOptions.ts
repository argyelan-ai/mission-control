/**
 * The chat view's two display options, split out of ChatView so both the
 * desktop header and the mobile options sheet can import them without a
 * module cycle (ChatView renders the sheet, so the sheet must not import
 * ChatView). ChatView re-exports all four names — existing importers
 * (sessions/page.tsx, tests) keep working unchanged.
 *
 * Die Listen tragen KEINE fertigen Beschriftungen, sondern Katalog-Schluessel:
 * ein Modul ohne React kann `useTranslations` nicht aufrufen, und eine hier
 * eingebaute deutsche Beschriftung landete sonst mitten in der englischen
 * Standard-Oberflaeche (Review-Befund PR #331). Aufgeloest wird erst dort, wo
 * gerendert wird — `t(labelKey)` im Namensraum `sessions`.
 */

/** How much of the transcript's machinery to show. */
export type DetailLevel = "compact" | "normal" | "verbose";

export const DETAIL_LEVELS: { key: DetailLevel; labelKey: string }[] = [
  { key: "compact", labelKey: "detailCompact" },
  { key: "normal", labelKey: "detailNormal" },
  { key: "verbose", labelKey: "detailVerbose" },
];

/** What occupies the center column: the parsed chat, or the raw tmux pane. */
export type CenterView = "chat" | "terminal";

export const CENTER_VIEWS: { key: CenterView; labelKey: string }[] = [
  { key: "chat", labelKey: "chat" },
  { key: "terminal", labelKey: "terminal" },
];
