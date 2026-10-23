"use client";

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { useEffortWord } from "@/lib/effortWord";
import { benchApi } from "@/verticals/bench_studio/api";
import type { EffortLevel } from "./types";

/** Effort chip row under one model row of the New-Challenge dialog (bench
 *  W0.6). Direct-API rows offer only what the backend's effort probe found
 *  for the served model ("Default" always: nothing is sent then). Opening
 *  the dialog only READS the stored probe; probing the box (2-4 real
 *  requests) is the operator's explicit "Check now" / "Check again", and the
 *  backend refuses it while the Spark is busy (round-1 review). Agent rows
 *  show a note: their session's own level is recorded after the run, it
 *  cannot be set from here yet (operator decision D5). */
export function EffortChips({
  index,
  sourceKind,
  model,
  sparkOffline,
  value,
  onChange,
  id,
  "aria-invalid": ariaInvalid,
  "aria-describedby": ariaDescribedBy,
}: {
  index: number;
  sourceKind: "spark" | "agent";
  // Explicit model, or "" / null for the Spark's active model.
  model: string | null | undefined;
  sparkOffline: boolean;
  value: EffortLevel | null | undefined;
  onChange: (level: EffortLevel | null) => void;
  // The dialog's field wiring: a server refusal of this row's effort is said
  // under the chips and focuses the group (round-6 L4).
  id?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
}) {
  const t = useTranslations("bench.effort");
  const word = useEffortWord();
  const locale = useLocale();
  const isSpark = sourceKind === "spark";
  const modelKey = (model ?? "").trim();

  const qc = useQueryClient();
  const queryKey = ["bench-effort-levels", modelKey || "active"];
  const query = useQuery({
    queryKey,
    queryFn: () => benchApi.effortLevels.get(modelKey || null),
    enabled: isSpark && !sparkOffline,
    staleTime: 60_000,
    retry: false,
  });
  const check = useMutation({
    mutationFn: () => benchApi.effortLevels.check(modelKey || null),
    onSuccess: (fresh) => qc.setQueryData(queryKey, fresh),
  });
  // A check result (e.g. "Spark busy") belongs to the model it ran for —
  // forget it when the row switches to another model.
  const resetCheck = check.reset;
  useEffect(() => {
    resetCheck();
  }, [modelKey, resetCheck]);
  // The backend answers 409 while a bench run or an agent works on the Spark.
  const checkBusy = check.isError && /^API 409\b/.test((check.error as Error)?.message ?? "");
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
  else if (query.isPending || check.isPending) hint = t("checking");
  else if (checkBusy) hint = t("busy");
  else if (check.isError) hint = t("failed");
  else if (data?.status === "unchecked") hint = t("unchecked");
  else if (query.isError || data?.status === "failed") hint = t("failed");
  else if (data?.status === "no_effect") hint = t("noEffect");
  else if (selected === "high" || selected === "max") hint = t("deepHint");
  // The hint describes the chosen level, not Default (round-1 t2 SOLL-5).
  else if (selected === "off") hint = t("offHint");
  else if (selected === "on") hint = t("onHint");
  else if (selected !== "default") hint = t("levelHint", { level: word(selected) });
  else if (data?.default_observed === "on") hint = t("serverDefaultOn");
  else if (data?.default_observed === "off") hint = t("serverDefaultOff");
  else hint = t("serverDefaultUnknown");
  const checked =
    data?.status === "ok" && data.probed_at ? t("checkedAt", { when: timeAgo(data.probed_at, locale) }) : null;
  const unchecked = data?.status === "unchecked";
  const canCheck =
    !sparkOffline &&
    !query.isFetching &&
    !check.isPending &&
    (unchecked || check.isError || query.isError || data?.status === "failed");

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
        {word(level)}
      </button>
    );
  };

  return (
    <div className="flex flex-col gap-1">
      <div
        role="radiogroup"
        id={id}
        tabIndex={id ? -1 : undefined}
        aria-invalid={ariaInvalid}
        aria-describedby={ariaDescribedBy}
        aria-label={t("groupAria", { index: index + 1 })}
        className="flex flex-wrap items-center gap-2"
      >
        <span className="text-xs" style={{ color: C.textMuted }}>
          {t("label")}
        </span>
        {chip("default")}
        {offered.map(chip)}
        {canCheck && (
          <button
            type="button"
            onClick={() => check.mutate()}
            className="min-h-11 px-3 text-sm"
            style={{ color: C.accent }}
          >
            {t(unchecked && !check.isError ? "checkNow" : "retry")}
          </button>
        )}
      </div>
      <span className="text-xs" style={{ color: C.textMuted }} aria-live="polite">
        {checked ? `${hint} ${checked}` : hint}
      </span>
    </div>
  );
}
