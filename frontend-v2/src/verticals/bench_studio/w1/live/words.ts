"use client";

// The shared bench words (`benchShared`, owned by the lead) as small typed
// helpers for package B: one word per state/harness/level in each language
// (K10). Machine codes the catalog does not know fall back to the code, so
// a new backend value is visible instead of silently blank.
import { useTranslations } from "next-intl";
import type { Effort, EffortOption, Harness, Reason } from "../types";

export function useBenchWords() {
  const t = useTranslations("benchShared");
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

  /** A wait reason in words, with its params (time already formatted). */
  const wait = (r: Reason, params: Record<string, string | number> = {}) =>
    has(`wait.${r.code}`) ? t(`wait.${r.code}`, { ...stringParams(r.params), ...params }) : r.code;

  return { t, has, word, level, harness, option, wait };
}

export function stringParams(p: Record<string, unknown> | null | undefined): Record<string, string | number> {
  const out: Record<string, string | number> = {};
  for (const [k, v] of Object.entries(p ?? {})) {
    if (typeof v === "string" || typeof v === "number") out[k] = v;
  }
  return out;
}
