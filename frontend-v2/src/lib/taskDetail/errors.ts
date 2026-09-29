/**
 * The backend rejects a status change it does not allow with HTTP 409 and a
 * structured detail ({error: "invalid_transition", current_status, expected,
 * allowed}) — routers/tasks.py and services/task_state.py. lib/api.ts throws
 * that as `Error("API 409: <json body>")`. This reads it back so the UI can
 * say one clear sentence instead of dumping JSON. The allowed transitions are
 * NOT modelled in the frontend — the backend stays the only state machine.
 */

export interface InvalidTransition {
  current: string;
  expected: string;
  allowed: string[];
}

export function parseInvalidTransition(err: unknown): InvalidTransition | null {
  if (!(err instanceof Error)) return null;
  const m = /^API 409: ([\s\S]*)$/.exec(err.message);
  if (!m) return null;
  try {
    const body = JSON.parse(m[1]) as { detail?: unknown };
    const d = body?.detail as Record<string, unknown> | undefined;
    if (!d || typeof d !== "object" || d.error !== "invalid_transition") return null;
    return {
      current: String(d.current_status ?? ""),
      expected: String(d.expected ?? ""),
      allowed: Array.isArray(d.allowed) ? d.allowed.map(String) : [],
    };
  } catch {
    return null;
  }
}
