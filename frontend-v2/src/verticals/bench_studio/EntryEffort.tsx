"use client";

import { Brain } from "lucide-react";
import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { useEffortWord } from "@/lib/effortWord";
import { Pill } from "@/components/shared/Pill";
import type { BenchEntry } from "./types";

/** True when a failed run's reason is "stopped at max_tokens": EntryEffort
 *  then says so in the UI language, and the detail page hides the backend's
 *  English error text for that entry (no mixed languages on a German page). */
export function failedAtMaxTokens(entry: BenchEntry): boolean {
  return entry.status === "failed" && !!entry.metrics?.truncated;
}

/** The level an entry ran at, for its pill: the chosen one for a direct-API
 *  row, the level of the agent's own omp session for an agent row. Null when
 *  the entry never stored one (older runs) or the agent's level is unknown. */
function pillLevel(entry: BenchEntry): string | null {
  const m = entry.metrics ?? {};
  if (entry.source_kind === "agent") {
    const observed = m.effort_observed;
    return observed && "source" in observed && observed.source === "omp_session"
      ? observed.level
      : null;
  }
  return m.effort_requested ?? null;
}

/** The effort as its own pill in the entry head, next to the name (round-2
 *  Q2: it was cut away with the label on a phone and stood under the video).
 *  Brain icon = the same sign as the chat's effort chip; the word comes from
 *  common.effortLevel. What went on the wire ("reasoning_effort=none") is
 *  machine detail — it lives in the tooltip, not in the visible text (K10). */
export function EntryEffortPill({ entry }: { entry: BenchEntry }) {
  const t = useTranslations("bench.effort");
  const word = useEffortWord();
  const level = pillLevel(entry);
  if (!level) return null;
  const m = entry.metrics ?? {};

  let detail: string;
  if (entry.source_kind === "agent") {
    const observed = m.effort_observed;
    const isDefault =
      observed && "source" in observed && observed.source === "omp_session" &&
      observed.configured == null && !observed.changed;
    detail = isDefault ? t("ompSessionDefaultTitle") : t("ompSessionTitle");
  } else if (m.effort_sent) {
    detail = t("sentTitle", { wire: `${m.effort_sent.key}=${String(m.effort_sent.value)}` });
  } else if (level !== "default" && (entry.status === "pending" || entry.status === "generating")) {
    detail = t("sendingTitle");
  } else {
    detail = t("sentNothingTitle");
  }

  return (
    <Pill
      color={C.textSecondary}
      variant="outline"
      casing="sentence"
      role="img"
      aria-label={t("pillAria", { level: word(level) })}
      title={detail}
      data-testid="bench-entry-effort"
    >
      <Brain size={12} strokeWidth={1.75} aria-hidden="true" />
      {word(level)}
    </Pill>
  );
}

/** One quiet line per entry: what the finished run showed about thinking
 *  (bench W0.6/W0.7) and the max_tokens warning. The level itself is the pill in
 *  the head. Old entries without a record render nothing. */
export function EntryEffort({ entry }: { entry: BenchEntry }) {
  const t = useTranslations("bench.effort");
  const locale = useLocale();
  const m = entry.metrics ?? {};
  const observed = m.effort_observed;
  if (!m.effort_requested && !observed && !m.truncated) return null;

  let line: string | null = null;
  if (entry.source_kind === "agent") {
    // Agent rows: the pill names the session level; only an unknown one is said here.
    if (observed && !("thinking" in observed) && !("level" in observed)) line = t("agentUnknown");
  } else if (observed && "thinking" in observed) {
    line =
      observed.thinking === "on"
        ? observed.reasoning_tokens != null
          ? t("thinkingOnTokens", { count: observed.reasoning_tokens.toLocaleString(locale) })
          : t("thinkingOn")
        : t("thinkingOff");
  }
  // A run still on its way says nothing here: the pill already names the
  // level, its tooltip says it is being sent (round-1 "sent: nothing" while
  // the max run was on the wire cannot happen any more), and the spinner
  // below says the state.

  if (!line && !m.truncated) return null;
  return (
    <div className="flex flex-col gap-1">
      {line && (
        <span className="text-xs" style={{ color: C.textMuted }}>
          {line}
        </span>
      )}
      {m.truncated && (
        <span className="text-xs" style={{ color: entry.status === "failed" ? C.error : C.warning }}>
          {t(entry.status === "failed" ? "truncatedFailed" : "truncated", { max: m.max_tokens ?? "?" })}
        </span>
      )}
    </div>
  );
}
