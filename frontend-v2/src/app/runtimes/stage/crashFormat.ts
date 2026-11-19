/**
 * Engine death on the stage card (ADR-096 §11, backend engine_death): when it
 * died, in the viewer's locale — the app's one clock format (lib/clockTime).
 */
export { formatClockTime as formatCrashTime } from "@/lib/clockTime";

/** ADR-096 §11.3 reason codes the catalog knows (runtimes.stage.crashReason.*);
 *  anything else → "unknown". */
export const CRASH_REASONS = [
  "guard",
  "oom",
  "kernel_oom",
  "exit",
  "member_down",
  "crash_loop",
  "unknown",
] as const;

export type CrashReasonKey = (typeof CRASH_REASONS)[number] | "exitCode";

/** Catalog key for a reason code; an exit with a known code gets its own
 *  wording ("exited with code 137"), an exit without one reads "process gone". */
export function crashReasonKey(code: string | null | undefined, exitCode?: number | null): CrashReasonKey {
  const known = (CRASH_REASONS as readonly string[]).includes(code ?? "")
    ? (code as (typeof CRASH_REASONS)[number])
    : "unknown";
  return known === "exit" && exitCode != null ? "exitCode" : known;
}

/** Exit 137 is a SIGKILL. On a GPU box that is almost always the kernel OOM
 *  killer or the recipe's memory guard, so the card adds "probably out of
 *  memory" (operator decision 2026-10-20). Only a hint on the plain "exit"
 *  reason: `oom` / `kernel_oom` / `guard` already name the cause, and the
 *  backend's reason code stays `exit` — the box did not confirm memory. */
export function crashLikelyOutOfMemory(code: string | null | undefined, exitCode?: number | null): boolean {
  return crashReasonKey(code, exitCode) === "exitCode" && exitCode === 137;
}
