"use client";

// The shared bench words (`benchShared`, owned by the lead) as small typed
// helpers for package B: one word per state/harness/level in each language
// (K10). Machine codes the catalog does not know fall back to the code, so
// a new backend value is visible instead of silently blank.
import { useLocale, useTranslations } from "next-intl";
import type { Effort, EffortOption, EffortRecord, Harness, Reason } from "../types";
import { clockTime } from "./format";

export function useBenchWords() {
  const t = useTranslations("benchShared");
  const locale = useLocale();
  const has = (key: string) => (typeof t.has === "function" ? t.has(key) : true);
  const word = (group: string, code: string) => (has(`${group}.${code}`) ? t(`${group}.${code}`) : code);

  const level = (l: Effort) => word("level", l);
  const harness = (h: Harness) => word("harness", h);

  /** "high (default)" when the server default is known, else "Default". */
  const option = (o: EffortOption, defaultObserved: "on" | "off" | "unknown") => {
    if (o.level !== "default") return level(o.level);
    const eff = o.merged_level ?? (defaultObserved === "on" || defaultObserved === "off" ? defaultObserved : null);
    return eff ? t("levelIsDefault", { level: level(eff) }) : level("default");
  };

  /** The effort a run had, in the sheet's words: "hoch (Standard)" when
   *  the probe knew what the server default maps to (K10: one name). */
  const effort = (e: EffortRecord) =>
    e.requested === "default" && e.default_level
      ? t("levelIsDefault", { level: level(e.default_level) })
      : level(e.requested);

  /** A wait reason in words. ISO times become a clock time, and the
   *  backend's `until` fills the `{time}` the sentences ask for — a missing
   *  param would print the raw key (review M1). */
  const wait = (r: Reason, params: Record<string, string | number> = {}) => {
    if (!has(`wait.${r.code}`)) return r.code;
    const p = waitParams(r, locale);
    return t(`wait.${r.code}`, { ...p, ...params });
  };

  /** Why a run failed: run error codes, then abort codes (a stopped or
   *  superseded run carries those), else the plain "failed" word. */
  const runError = (code: string | null | undefined) => {
    if (code && has(`reason.${code}`)) return t(`reason.${code}`);
    if (code && has(`abort.${code}`)) return t(`abort.${code}`);
    return t("reason.run_failed");
  };

  return { t, has, word, level, harness, option, wait, effort, runError };
}

/** Wait params as the sentences want them: ISO stamps → "11:45", and
 *  `until` / `not_before` also as `{time}`. */
export function waitParams(r: Reason, locale: string): Record<string, string | number> {
  const out = stringParams(r.params);
  for (const k of ["time", "until", "not_before"]) {
    const v = out[k];
    if (typeof v === "string" && v.includes("T")) out[k] = clockTime(v, locale) ?? v;
  }
  if (out.time === undefined) {
    const at = out.until ?? out.not_before;
    if (at !== undefined) out.time = at;
  }
  return out;
}

export function stringParams(p: Record<string, unknown> | null | undefined): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(p ?? {})) {
    if (typeof v === "string" || typeof v === "number") out[k] = v;
  }
  return out;
}
