/**
 * The HTTP status inside an error from `lib/api.ts`, which throws
 * `Error("API <status>: <body>")`. `null` for anything else (network error,
 * an error from somewhere else).
 */
export function apiErrorStatus(err: unknown): number | null {
  if (!(err instanceof Error)) return null;
  const m = /^API (\d{3}):/.exec(err.message);
  return m ? Number(m[1]) : null;
}

/**
 * The record does not exist: 404, or 422 for an id that cannot be one (a
 * typo in a link, `/agents/kaputt`). Asking again will not change that —
 * a page shows "not found" instead of retrying (round-3 t4 S1).
 */
export function isMissingRecord(err: unknown): boolean {
  const status = apiErrorStatus(err);
  return status === 404 || status === 422;
}
