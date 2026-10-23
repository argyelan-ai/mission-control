"use client";

import { useTranslations } from "next-intl";

/**
 * One word per effort (thinking) level, from `common.effortLevel` — the bench
 * dialog, the bench entry and the chat effort menu all say the same word
 * ("Aus" / "Max" in German, "Off" / "Max" in English; round-2 Q2, DESIGN
 * K10). A level the catalog does not know yet (a new harness ladder) is shown
 * as the raw value instead of a guessed word.
 */
export function useEffortWord(): (level: string | null | undefined) => string {
  const t = useTranslations("common.effortLevel");
  return (level) => {
    if (!level) return "";
    return t.has(level) ? t(level) : level;
  };
}
