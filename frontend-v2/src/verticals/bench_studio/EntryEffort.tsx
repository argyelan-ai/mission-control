"use client";

import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import type { BenchEntry, EffortLevel } from "./types";

const KNOWN_LEVELS: EffortLevel[] = ["default", "off", "on", "low", "medium", "high", "max"];

/** One line per entry: which effort was asked for, what went on the wire
 *  and what the run showed (bench W0.6/W0.7). Old entries without a record
 *  render nothing — they never stored it. */
export function EntryEffort({ entry }: { entry: BenchEntry }) {
  const t = useTranslations("bench.effort");
  const locale = useLocale();
  const m = entry.metrics ?? {};
  const observed = m.effort_observed;
  if (!m.effort_requested && !observed) return null;

  const levelWord = (level: string) =>
    (KNOWN_LEVELS as string[]).includes(level) ? t(`level.${level}`) : level;

  const parts: string[] = [];
  if (entry.source_kind === "agent") {
    if (observed && "source" in observed && observed.source === "omp_session") {
      const word = levelWord(observed.level);
      parts.push(
        observed.configured == null && !observed.changed
          ? t("ompSessionDefault", { level: word })
          : t("ompSession", { level: word })
      );
    } else {
      parts.push(t("agentUnknown"));
    }
  } else {
    const requested = m.effort_requested ?? "default";
    parts.push(t("requested", { level: levelWord(requested) }));
    parts.push(
      m.effort_sent ? t("sent", { wire: `${m.effort_sent.key}=${String(m.effort_sent.value)}` }) : t("sentNothing")
    );
    if (observed && "thinking" in observed) {
      parts.push(
        observed.thinking === "on"
          ? observed.reasoning_tokens != null
            ? t("thinkingOnTokens", { count: observed.reasoning_tokens.toLocaleString(locale) })
            : t("thinkingOn")
          : t("thinkingOff")
      );
    }
  }

  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs" style={{ color: C.textMuted }}>
        {parts.join(" · ")}
      </span>
      {m.truncated && (
        <span className="text-xs" style={{ color: C.warning }}>
          {t("truncated", { max: m.max_tokens ?? "?" })}
        </span>
      )}
    </div>
  );
}
