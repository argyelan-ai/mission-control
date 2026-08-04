import type { Runtime } from "@/lib/types";

export type RuntimeGroup = { label: string | null; runtimes: Runtime[] };

/**
 * Bundle runtimes into vendor groups for <optgroup>, preserving API order.
 *
 * The server already returns the rows grouped (GET /api/v1/runtimes sorts by
 * provider) and ships the label as `provider_label`. This function only makes
 * that structure visible — it does NOT decide membership. Deriving the vendor
 * client-side would be a second copy of a backend rule, which is exactly how
 * the runtime-switch gate drifted apart before.
 *
 * Rows without a recognised vendor (local vLLM, LM Studio, unsloth) collect in
 * a trailing group with `label: null`, matching where the server sorts them.
 */
export function groupRuntimesByProvider(runtimes: Runtime[]): RuntimeGroup[] {
  const groups: RuntimeGroup[] = [];
  for (const rt of runtimes) {
    const label = rt.provider_label ?? null;
    const last = groups[groups.length - 1];
    // Same label as the previous row → same block. Relies on the server's
    // ordering rather than re-sorting, so one source decides the order.
    if (last && last.label === label) {
      last.runtimes.push(rt);
    } else {
      groups.push({ label, runtimes: [rt] });
    }
  }
  return groups;
}
