import { C } from "@/lib/colors";

/**
 * Shared look of the "next step" under the task title (DESIGN.md K8/K11) —
 * the task state card and the head state card use the same pieces.
 */

/** The surface for "you have to act" — one step above the page ground. */
export const RAISED = "var(--detail-raised, var(--color-bg-elevated))";

/** Primary button (K11): accent fill, at most one per screen. */
export const PRIMARY_BTN =
  "inline-flex items-center justify-center gap-2 h-11 px-4 rounded-md text-sm cursor-pointer transition-colors hover:bg-[var(--color-accent-light)] disabled:opacity-40 disabled:cursor-not-allowed";
export const PRIMARY_STYLE = { background: C.accent, color: C.onAccent, fontWeight: 600 } as const;

/** Secondary button (K11): text without a frame, 44 px target. */
export const QUIET_BTN =
  "inline-flex items-center justify-center gap-2 h-11 px-3 rounded-md text-sm cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:opacity-40 disabled:cursor-not-allowed";

/** Body text of the next step: 16 px on the phone, 14 px in a wide pane. */
export const NEXT_TEXT = "text-base @min-[560px]:text-sm leading-relaxed";
