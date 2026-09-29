"use client";

/**
 * Home → one quiet line with last night's journey tests (E6, docs/journeys.md).
 *
 *   ✓ Journey tests: 3 of 3 passed, 1 known gap · 5 h 12 min ago
 *   ✕ Journey tests: Cost & local share failed · 5 h 12 min ago
 *
 * Only the icon carries a status colour, and only when the run failed or did
 * not finish. Renders nothing until a run has written a result.
 */

import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, MinusCircle, XCircle } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { journeysReason, type JourneysResponse, type JourneysResult } from "@/lib/journeys";
import { formatAbsolute, formatAge } from "@/lib/taskDetail/format";

export const JOURNEYS_KEY = ["system", "journeys"] as const;

export function JourneysLine() {
  const q = useQuery<JourneysResponse>({
    queryKey: JOURNEYS_KEY,
    queryFn: () => api.system.journeys(),
    retry: false,
    refetchInterval: 15 * 60_000,
  });
  if (!q.data?.result) return null;
  return <JourneysLineView result={q.data.result} />;
}

export function JourneysLineView({ result }: { result: JourneysResult }) {
  const t = useTranslations("home.journeys");
  const locale = useLocale();

  let text: string;
  let Icon = CheckCircle2;
  let iconColor: string = C.textMuted;
  let textColor: string = C.textSecondary;
  if (result.status === "green") {
    text = t("green", { passed: result.passed, total: result.total });
    if (result.known_gaps > 0) text += `, ${t("gaps", { count: result.known_gaps })}`;
  } else if (result.status === "red") {
    // Human names from i18n (home.journeys.names.<id>) — the one source; an
    // id without a name yet falls back to the id itself.
    const names = result.failed.map((id) => (t.has(`names.${id}`) ? t(`names.${id}`) : id)).join(", ");
    text = names ? t("red", { names }) : t("redUnnamed", { failed: result.total - result.passed, total: result.total });
    Icon = XCircle;
    iconColor = STATUS_TEXT.error;
    textColor = C.textPrimary;
  } else if (result.status === "skipped") {
    const reason = journeysReason(result.reason);
    text = reason ? t("skippedBecause", { reason: t(`reason.${reason}`) }) : t("skipped");
    Icon = MinusCircle;
  } else {
    const reason = journeysReason(result.reason);
    text = reason ? t("errorBecause", { reason: t(`reason.${reason}`) }) : t("error");
    Icon = AlertTriangle;
    iconColor = STATUS_TEXT.warning;
    textColor = C.textPrimary;
  }

  const age = formatAge(result.finished_at, locale);

  return (
    // px-3 sm:px-4: the same inner edge as the metrics card above (K9)
    <p
      className="flex items-start gap-2 px-3 sm:px-4 text-xs leading-snug"
      style={{ color: textColor }}
      data-testid="journeys-line"
      data-status={result.status}
    >
      <Icon size={13} aria-hidden className="shrink-0 mt-px" style={{ color: iconColor }} />
      <span className="min-w-0">
        {text}
        {age && (
          <span className="whitespace-nowrap" style={{ color: C.textMuted }} title={formatAbsolute(result.finished_at, locale)}>
            {" · "}
            {t("ago", { age })}
          </span>
        )}
      </span>
    </p>
  );
}
