/**
 * "Copy setup report" (Welle 3, PR 3-4; Settings → About). A plain-text
 * snapshot an operator can paste into a support request without exposing
 * anything private: version, health, which provider keys test ok/fail (no
 * values, no reasons), the onboarding timings from `lib/onboardingMetrics.ts`
 * and the last few fixed error codes. Nothing here is sent anywhere by
 * itself — it only becomes text on the clipboard when the operator clicks
 * the button.
 *
 * Privacy (AGENTS.md, public repo — no names, keys, host names, e-mails):
 * - `system.status()` contributes status words and counts only; the raw
 *   `components.*.error` / `gateway.url` fields are never read here, since
 *   they can carry an internal address or a server's own error text.
 * - `configChecks.list("secret")` contributes each key's `label` (e.g.
 *   "Anthropic API key") and `state` only — never `message`, `reason`,
 *   `reason_params` or any value.
 */
import { api } from "./api";
import {
  ONBOARDING_MILESTONE_ORDER,
  getOnboardingMetrics,
  type OnboardingMetrics,
  type OnboardingMilestoneKey,
} from "./onboardingMetrics";

export interface SetupReportLabels {
  heading: string;
  version: string;
  unknown: string;
  health: string;
  database: string;
  redis: string;
  agentsOnline: string;
  keysHeading: string;
  noKeys: string;
  keyState: Record<"ok" | "fail" | "never_tested", string>;
  timesHeading: string;
  milestone: Record<OnboardingMilestoneKey, string>;
  notYet: string;
  errorsHeading: string;
  noErrors: string;
}

/** `42000` → `"42s"`, `125000` → `"2m 5s"`. Exported for its own test. */
export function formatDurationMs(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

/**
 * Gathers version, health, key status and onboarding metrics and renders
 * them as one plain-text report. Every source is fetched independently and
 * a failed one is simply left out — a half-working install must still
 * produce a report, not an error.
 */
export async function buildSetupReport(labels: SetupReportLabels): Promise<string> {
  const [version, status, checks, metrics] = await Promise.all([
    api.system.version().catch(() => null),
    api.system.status().catch(() => null),
    api.configChecks.list("secret").catch(() => null),
    getOnboardingMetrics().catch(
      (): OnboardingMetrics => ({ t0: null, elapsed_ms: {}, errors: [] }),
    ),
  ]);

  const lines: string[] = [labels.heading, ""];

  lines.push(`${labels.version}: ${version?.current ?? labels.unknown}`);
  if (status) {
    const db = status.components?.database?.status ?? labels.unknown;
    const redis = status.components?.redis?.status ?? labels.unknown;
    lines.push(`${labels.health}: ${status.status} (${labels.database}: ${db}, ${labels.redis}: ${redis})`);
    lines.push(`${labels.agentsOnline}: ${status.agents.online}/${status.agents.total}`);
  } else {
    lines.push(`${labels.health}: ${labels.unknown}`);
  }

  lines.push("", labels.keysHeading);
  const keyItems = checks?.items ?? [];
  if (keyItems.length === 0) {
    lines.push(`  ${labels.noKeys}`);
  } else {
    for (const item of keyItems) {
      lines.push(`  ${item.label}: ${labels.keyState[item.state]}`);
    }
  }

  lines.push("", labels.timesHeading);
  for (const key of ONBOARDING_MILESTONE_ORDER) {
    const ms = metrics.elapsed_ms[key];
    lines.push(`  ${labels.milestone[key]}: ${ms === undefined ? labels.notYet : formatDurationMs(ms)}`);
  }

  lines.push("", labels.errorsHeading);
  if (metrics.errors.length === 0) {
    lines.push(`  ${labels.noErrors}`);
  } else {
    for (const err of metrics.errors) {
      lines.push(`  ${err.code} @ ${err.at}`);
    }
  }

  return lines.join("\n");
}
