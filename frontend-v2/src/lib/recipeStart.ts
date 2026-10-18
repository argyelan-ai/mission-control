/**
 * Recipe start (`POST /hosts/{id}/recipes/{slug}/start`) — shared reading of
 * its structured refusals, used by every place that starts a recipe (deploy
 * dialog, box recipe switcher, model browser).
 */

/** The structured 409 of a recipe start whose instance on this box is paused
 *  (`runtimes.enabled = false`). The backend refuses instead of creating a
 *  second row; the dialog offers the explicit resume. */
export interface PausedRuntimeDetail {
  code: "runtime_paused";
  runtime_id: string;
  runtime_slug: string;
  message?: string;
}

export function pausedRuntimeDetail(err: unknown): PausedRuntimeDetail | null {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart < 0) return null;
  try {
    const detail = JSON.parse(msg.slice(jsonStart))?.detail;
    if (detail && typeof detail === "object" && detail.code === "runtime_paused") {
      return detail as PausedRuntimeDetail;
    }
  } catch {
    /* not JSON */
  }
  return null;
}
