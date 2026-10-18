// Why a config check ended the way it did — in the operator's language.
//
// The backend sets a machine-readable `reason` (+ `reason_params`, e.g.
// `{status: 401}`) on every result; `message` is only an English free text.
// The UI words the reason through i18n (`checks.reason.*`, EN and DE) and
// shows the free text only for `unknown`, folded away as a detail.
//
// Keep CHECK_REASON_CODES in step with REASON_CODES in
// backend/app/services/config_checks/registry.py — a test compares them.
import type { ConfigCheckItem } from "@/lib/types";

export const CHECK_REASON_CODES = [
  "ok",
  "socket_mode_not_ready",
  "rejected",
  "rate_limited",
  "http_error",
  "unreachable",
  "timeout",
  "undecryptable",
  "dimension_mismatch",
  "wrong_token_type",
  "missing_scope",
  "invalid_owner",
  "no_model",
  "not_configured",
  "unknown",
] as const;

export type CheckReasonCode = (typeof CHECK_REASON_CODES)[number];

export function isCheckReasonCode(v: unknown): v is CheckReasonCode {
  return typeof v === "string" && (CHECK_REASON_CODES as readonly string[]).includes(v);
}

/** Reasons that say the VALUE is wrong (send a different one), as opposed
 *  to the test not getting through (timeout, network, rate limit, unknown —
 *  the same value may well work on a retry). */
const VALUE_REASONS: readonly CheckReasonCode[] = [
  "rejected",
  "undecryptable",
  "dimension_mismatch",
  "wrong_token_type",
  "missing_scope",
  "invalid_owner",
];

export function isValueRejected(item: Pick<ConfigCheckItem, "state" | "reason"> | null | undefined): boolean {
  return !!item && item.state === "fail" && isCheckReasonCode(item.reason) && VALUE_REASONS.includes(item.reason);
}

/** The catalog key under `checks.reason` and its values, or null when there
 *  is nothing to say (never tested). An unlisted or missing code on a failed
 *  result reads as `unknown`. Codes with an optional HTTP status have a
 *  `…Status` variant, since the test i18n mock has no ICU `select`. */
export function checkReasonMessage(
  item: Pick<ConfigCheckItem, "state" | "reason" | "reason_params"> | undefined | null,
): { code: CheckReasonCode; key: string; values: Record<string, string | number> } | null {
  if (!item || item.state === "never_tested") return null;
  const code: CheckReasonCode = isCheckReasonCode(item.reason)
    ? item.reason
    : item.state === "ok"
      ? "ok"
      : "unknown";
  const params = item.reason_params ?? {};
  const status = typeof params.status === "number" ? params.status : null;
  if ((code === "rejected" || code === "rate_limited") && status != null) {
    return { code, key: `${code}Status`, values: { status } };
  }
  if (code === "http_error") {
    return status != null
      ? { code, key: "http_errorStatus", values: { status } }
      : { code, key: "unknown", values: {} };
  }
  if (code === "dimension_mismatch") {
    const dimension = typeof params.dimension === "number" ? params.dimension : null;
    const expected = typeof params.expected === "number" ? params.expected : null;
    if (dimension != null && expected != null) {
      return { code, key: "dimension_mismatch", values: { dimension, expected } };
    }
    return { code, key: "dimension_mismatchPlain", values: {} };
  }
  return { code, key: code, values: {} };
}
