/**
 * Engine death on the stage card (backend services/engine_death.py): when it
 * died, in the viewer's locale. Today → "17:20"; another day → "18 Oct, 17:20"
 * / "18. Okt., 17:20". Unparseable → null (the card then shows no time —
 * never an invented one).
 */
export function formatCrashTime(at: string | null | undefined, locale: string, now: Date = new Date()): string | null {
  if (!at) return null;
  const d = new Date(at);
  if (Number.isNaN(d.getTime())) return null;
  const tag = locale === "de" ? "de-CH" : "en-GB";
  const sameDay =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString(tag, { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString(tag, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

/** Reason codes the catalog knows (runtimes.stage.crashReason.*); anything else → "unknown". */
export const CRASH_REASONS = [
  "oom",
  "memguard",
  "killed",
  "exited_error",
  "exited_clean",
  "hung",
  "container_gone",
  "process_gone",
  "unknown",
] as const;

export function crashReasonKey(code: string | null | undefined): (typeof CRASH_REASONS)[number] {
  return (CRASH_REASONS as readonly string[]).includes(code ?? "") ? (code as (typeof CRASH_REASONS)[number]) : "unknown";
}
