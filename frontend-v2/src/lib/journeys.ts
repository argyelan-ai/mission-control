/** Last night's journey-test result (GET /api/v1/system/journeys, docs/journeys.md). */
export type JourneysStatus = "green" | "red" | "skipped" | "error";

export interface JourneysResult {
  status: JourneysStatus;
  finished_at: string | null;
  commit: string | null;
  total: number;
  passed: number;
  /** Journey ids (e.g. "J-usage") with at least one failed test. */
  failed: string[];
  known_gaps: number;
  /** Short runner reason for skipped / error runs (English machine text). */
  reason: string | null;
}

export interface JourneysResponse {
  result: JourneysResult | null;
}

export type JourneysReason = "head" | "deploy" | "disk" | "port" | "timeout";

/** Maps the runner's reason text onto a translatable key (never shown raw). */
export function journeysReason(reason: string | null): JourneysReason | null {
  if (!reason) return null;
  if (reason.startsWith("head running")) return "head";
  if (reason.startsWith("deploy running")) return "deploy";
  if (reason.startsWith("disk")) return "disk";
  if (reason.startsWith("port")) return "port";
  if (reason.startsWith("time limit")) return "timeout";
  return null;
}
