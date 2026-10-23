/**
 * The backend answers 503 when the Mac's wake helper is not running — no
 * fresh heartbeat (`watcher.alive`) or a request left unread (PR #976 review,
 * round-7 L5). Nothing woke the box, so a wake button says why instead of
 * "Waking…". `request()` throws `Error("API 503: …")`.
 *
 * Shared by both wake paths: the box card (`WakeBoxButton`, per host) and the
 * runtime detail panel (per runtime).
 */
export function isWakeHelperDown(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("API 503");
}
