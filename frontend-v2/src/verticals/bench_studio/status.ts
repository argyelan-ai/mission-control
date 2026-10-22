// Human status words for a bench challenge (W0.8). The API keeps its codes
// (generating/review/failed …); the UI shows one i18n word per state (DESIGN
// K10) and, for a failed challenge, why it ended — derived from the stored
// error strings, so the list can say "Aborted – task deleted" without the
// detail page being opened.
import type { BenchChallenge } from "./types";

export type AbortReason =
  | "taskDeleted"
  | "taskFailed"
  | "taskAborted"
  | "stopped"
  | "renderFailed"
  | "composeFailed";

// Entry errors written by the backend (orchestrator.reconcile_challenge,
// stop_challenge) — exact strings, not guesses.
const ENTRY_REASONS: Record<string, AbortReason> = {
  "agent task deleted": "taskDeleted",
  "agent task failed": "taskFailed",
  "agent task aborted": "taskAborted",
  "stopped by operator": "stopped",
};

/** Why a failed challenge ended, or null when it did not fail or the
 *  reason is not one we can name (the raw error is shown then). */
export function abortReason(challenge: BenchChallenge): AbortReason | null {
  if (challenge.status !== "failed") return null;
  const error = challenge.error ?? "";
  if (error === "stopped by operator") return "stopped";
  if (error.startsWith("compose failed")) return "composeFailed";
  if (
    error === "all entries failed during rendering" ||
    error.startsWith("entry rerender failed")
  ) {
    return "renderFailed";
  }
  if (error === "all entries failed during generation") {
    const reasons = new Set(
      challenge.entries
        .filter((e) => e.status === "failed")
        .map((e) => ENTRY_REASONS[e.error ?? ""] ?? null),
    );
    if (reasons.size === 1) return [...reasons][0];
  }
  return null;
}
