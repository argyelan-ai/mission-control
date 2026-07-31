import { C, STATUS_TEXT } from "@/lib/colors";
import type { LoopStatus } from "@/lib/types";

// ── Status vocabulary — single source for the Loops UI (ADR-051) ───────────
// Chip pattern per DESIGN.md: `${color}22` bg, `${color}55` border, color text.
// `labelKey` resolves against the "loops.status" namespace at the render site
// (i18n — module-level constants can't call useTranslations() themselves).

export const LOOP_STATUS_META: Record<
  LoopStatus,
  { labelKey: string; color: string; textColor: string }
> = {
  draft: { labelKey: "draft", color: C.textDim, textColor: C.textSecondary },
  running: { labelKey: "running", color: C.accent, textColor: C.accent },
  waiting_gate: { labelKey: "waitingGate", color: C.warning, textColor: STATUS_TEXT.warning },
  paused: { labelKey: "paused", color: C.warning, textColor: STATUS_TEXT.warning },
  done: { labelKey: "done", color: C.online, textColor: STATUS_TEXT.online },
  failed: { labelKey: "failed", color: C.error, textColor: STATUS_TEXT.error },
};

/** Loops in these statuses are considered inactive — safe to delete client-side (backend still enforces the 409). */
export function isLoopInactive(status: LoopStatus): boolean {
  return status === "draft" || status === "paused" || status === "done" || status === "failed";
}

export function canStartLoop(status: LoopStatus): boolean {
  return status === "draft" || status === "paused";
}

export function canPauseLoop(status: LoopStatus): boolean {
  return status === "running";
}

export function canStopLoop(status: LoopStatus): boolean {
  return status === "running" || status === "waiting_gate" || status === "paused";
}
