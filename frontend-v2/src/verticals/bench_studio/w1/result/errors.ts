// Error helpers of package C (own copy — the legacy apiError.ts is not
// imported, CONTRACT §4). request() throws `Error("API <status>: <body>")`.

/** The HTTP status of a request() error, or null. */
export function apiStatus(err: unknown): number | null {
  if (!(err instanceof Error)) return null;
  const m = /^API (\d{3})/.exec(err.message);
  return m ? Number(m[1]) : null;
}

/** The server's plain-text `detail`, else the fallback (never raw JSON). */
export function apiErrorText(err: unknown, fallback: string): string {
  if (!(err instanceof Error)) return fallback;
  const m = /^API \d+: ([\s\S]*)$/.exec(err.message);
  if (!m) return fallback;
  try {
    const detail = (JSON.parse(m[1]) as { detail?: unknown }).detail;
    return typeof detail === "string" && detail.trim() ? detail.trim() : fallback;
  } catch {
    return fallback;
  }
}

/** Overlay actions POST with the operator's token: same-origin paths only. */
export function isSafeEndpoint(endpoint: string): boolean {
  return endpoint.startsWith("/") && !endpoint.startsWith("//");
}
