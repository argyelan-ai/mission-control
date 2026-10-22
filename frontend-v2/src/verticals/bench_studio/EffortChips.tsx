"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { benchApi } from "@/verticals/bench_studio/api";
import type { EffortLevel } from "./types";

/** Effort chip row under one model row of the New-Challenge dialog (bench
 *  W0.6). Direct-API rows offer only what the backend's effort probe found
 *  for the served model ("Default" always: nothing is sent then). Agent rows
 *  show a note: their session's own level is recorded after the run, it
 *  cannot be set from here yet (operator decision D5). */
export function EffortChips({
  index,
  sourceKind,
  model,
  sparkOffline,
  value,
  onChange,
}: {
  index: number;
  sourceKind: "spark" | "agent";
  // Explicit model, or "" / null for the Spark's active model.
  model: string | null | undefined;
  sparkOffline: boolean;
  value: EffortLevel | null | undefined;
  onChange: (level: EffortLevel | null) => void;
}) {
  const t = useTranslations("bench.effort");
  const locale = useLocale();
  const isSpark = sourceKind === "spark";
  const modelKey = (model ?? "").trim();

  const query = useQuery({
    queryKey: ["bench-effort-levels", modelKey || "active"],
    queryFn: () => benchApi.effortLevels.get(modelKey || null),
    enabled: isSpark && !sparkOffline,
    staleTime: 60_000,
    retry: false,
  });
  const data = query.data;
  const offered: EffortLevel[] = data?.status === "ok" ? data.levels : [];
  const selected: EffortLevel = value ?? "default";

  // A level the newly chosen model does not offer falls back to Default —
  // the backend would refuse it with a 422 otherwise.
  useEffect(() => {
    if (!data || selected === "default") return;
    if (!offered.includes(selected)) onChange(null);
  }, [data, offered, selected, onChange]);

  if (!isSpark) {
    return (
      <span className="text-xs" style={{ color: C.textMuted }}>
        {t("agentNote")}
      </span>
    );
  }

  let hint: string;
  if (sparkOffline || data?.reachable === false) hint = t("offline");
  else if (query.isPending) hint = t("checking");
  else if (query.isError || data?.status === "failed") hint = t("failed");
  else if (data?.status === "no_effect") hint = t("noEffect");
  else if (selected === "high" || selected === "max") hint = t("deepHint");
  else if (data?.default_observed === "on") hint = t("serverDefaultOn");
  else if (data?.default_observed === "off") hint = t("serverDefaultOff");
  else hint = t("serverDefaultUnknown");
  const checked =
    data?.status === "ok" && data.probed_at ? t("checkedAt", { when: timeAgo(data.probed_at, locale) }) : null;
  const canRetry = !sparkOffline && !query.isFetching && (query.isError || data?.status === "failed");

  const chip = (level: EffortLevel) => {
    const active = selected === level;
    return (
      <button
        key={level}
        type="button"
        role="radio"
        aria-checked={active}
        onClick={() => onChange(level === "default" ? null : level)}
        className="min-h-11 px-3 rounded-full text-sm"
        style={{
          backgroundColor: active ? C.accentSubtle : "transparent",
          color: active ? C.accent : C.textSecondary,
          border: `1px solid ${active ? C.borderAccent : C.border}`,
        }}
      >
        {t(`level.${level}`)}
      </button>
    );
  };

  return (
    <div className="flex flex-col gap-1">
      <div
        role="radiogroup"
        aria-label={t("groupAria", { index: index + 1 })}
        className="flex flex-wrap items-center gap-2"
      >
        <span className="text-xs" style={{ color: C.textMuted }}>
          {t("label")}
        </span>
        {chip("default")}
        {offered.map(chip)}
        {canRetry && (
          <button
            type="button"
            onClick={() => query.refetch()}
            className="min-h-11 px-3 text-sm"
            style={{ color: C.accent }}
          >
            {t("retry")}
          </button>
        )}
      </div>
      <span className="text-xs" style={{ color: C.textMuted }} aria-live="polite">
        {checked ? `${hint} · ${checked}` : hint}
      </span>
    </div>
  );
}
