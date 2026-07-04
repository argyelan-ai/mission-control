import { C, STATUS_TEXT } from "@/lib/colors";
import type { LoopStatus } from "@/lib/types";

// ── Status vocabulary — single source for the Loops UI (ADR-051) ───────────
// Chip pattern per DESIGN.md: `${color}22` bg, `${color}55` border, color text.

export const LOOP_STATUS_META: Record<
  LoopStatus,
  { label: string; color: string; textColor: string }
> = {
  draft: { label: "Draft", color: C.textDim, textColor: C.textSecondary },
  running: { label: "Running", color: C.accent, textColor: C.accent },
  waiting_gate: { label: "Waiting for your go", color: C.warning, textColor: STATUS_TEXT.warning },
  paused: { label: "Paused", color: C.warning, textColor: STATUS_TEXT.warning },
  done: { label: "Done", color: C.online, textColor: STATUS_TEXT.online },
  failed: { label: "Failed", color: C.error, textColor: STATUS_TEXT.error },
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
