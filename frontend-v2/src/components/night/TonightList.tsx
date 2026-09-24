"use client";

/**
 * "Tonight" on the task list (ROADMAP E2): what the night shift will start,
 * in marking order, plus last night's outcome in one line.
 *
 *   TONIGHT · 22:00–06:00 · Europe/Berlin                         2
 *   ⚠ The night shift is off — marked tasks will not start. Settings
 *   Fix flaky retry test         omp · GLM local · Queued         [×]
 *   Refactor upload worker       Waiting · another head uses the box
 *   Last night (2026-09-23): 2 passed · 1 failed · 1 needs you
 *
 * Renders nothing while heads are off, or when nothing is marked and there
 * is no report yet. Rows open the task; × removes the mark (before start).
 */

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { AlertTriangle, Moon, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { headStateKey, pairLabel } from "@/lib/heads";
import {
  NIGHT_CATEGORIES,
  nightErrorKey,
  nightReasonKey,
  reportCounts,
  type NightEntry,
  type NightTonight,
} from "@/lib/nightShift";
import { useHeadsEnabled } from "@/components/heads/useHeadsEnabled";
import { useHeadPairsForLabels } from "@/components/heads/HeadStateCard";

export const TONIGHT_KEY = ["nightShift", "tonight"] as const;

/** ``onOpenTask`` returns true when it opened the task in place; otherwise
 *  the row's link navigates to ``/tasks?task=<id>``. */
type OpenTask = (taskId: string) => boolean;

export function TonightList({ onOpenTask }: { onOpenTask?: OpenTask }) {
  const headsEnabled = useHeadsEnabled();
  const q = useQuery<NightTonight>({
    queryKey: TONIGHT_KEY,
    queryFn: () => api.nightShift.tonight(),
    enabled: headsEnabled === true,
    retry: false,
    refetchInterval: 60_000,
  });
  if (headsEnabled !== true || !q.data) return null;
  return <TonightListView data={q.data} onOpenTask={onOpenTask} />;
}

export function TonightListView({ data, onOpenTask }: { data: NightTonight; onOpenTask?: OpenTask }) {
  const t = useTranslations("nightShift");
  const tHeads = useTranslations("heads");
  const qc = useQueryClient();
  const pairs = useHeadPairsForLabels(data.entries.length > 0);
  const unmark = useMutation({
    mutationFn: (taskId: string) => api.nightShift.unmark(taskId),
    onSuccess: () => {
      notify.success(t("unqueued"));
      qc.invalidateQueries({ queryKey: ["nightShift"] });
      qc.invalidateQueries({ queryKey: ["tasks"] });
    },
    onError: (err) => notify.error(t(nightErrorKey(err))),
  });

  const { config, entries, last_report: report } = data;
  if (entries.length === 0 && !report) return null;

  const labelFor = (e: NightEntry) => {
    const p = pairs?.find((x) => x.harness === e.harness && x.runtime_slug === e.runtime_slug);
    return p ? pairLabel(p) : pairLabel({ harness: e.harness, runtime_slug: e.runtime_slug });
  };
  const stateText = (e: NightEntry) => {
    if (e.state === "started" && e.run) return tHeads(headStateKey(e.run.state));
    if (e.state === "waiting" || e.state === "skipped") return `${t(`state.${e.state}`)} · ${t(nightReasonKey(e.reason))}`;
    return t(`state.${e.state}`);
  };
  const counts = reportCounts(report);

  return (
    <section className="px-2 pt-1 pb-2" aria-labelledby="tonight-heading" data-testid="tonight-list">
      <div className="flex items-center gap-2 px-2 py-1.5">
        <Moon size={12} aria-hidden style={{ color: C.textMuted }} />
        <h2 id="tonight-heading" className="label-sys">{t("tonightTitle")}</h2>
        <span className="text-[10.5px] font-mono truncate" style={{ color: C.textMuted }} data-testid="tonight-window">
          {t("windowLine", { start: config.start, end: config.end, timezone: config.timezone })}
          {config.active ? ` · ${t("activeNow")}` : ""}
        </span>
        <span className="ml-auto text-[10.5px] font-mono" style={{ color: C.textDim }}>{entries.length}</span>
      </div>

      {!config.enabled && entries.length > 0 && (
        <p className="mx-2 mb-1 flex items-start gap-1.5 text-[11px]" style={{ color: STATUS_TEXT.warning }} data-testid="tonight-off">
          <AlertTriangle size={12} className="shrink-0 mt-0.5" aria-hidden />
          <span>
            {t("offWarning")}{" "}
            <Link href="/settings?section=night-shift" className="underline" style={{ color: C.textPrimary }}>
              {t("openSettings")}
            </Link>
          </span>
        </p>
      )}

      {entries.length > 0 && (
        <ul className="space-y-px">
          {entries.map((e) => (
            <li key={e.task_id} className="flex items-stretch gap-1 rounded-md hover:bg-[var(--color-bg-hover)]" data-testid={`tonight-row-${e.task_id}`}>
              <Link
                href={`/tasks?task=${e.task_id}`}
                onClick={(ev) => {
                  if (onOpenTask?.(e.task_id)) ev.preventDefault();
                }}
                className="flex-1 min-w-0 flex flex-col justify-center px-2 py-1.5 min-h-[44px] sm:min-h-[36px]"
              >
                <span className="text-xs truncate" style={{ color: C.textPrimary }}>{e.title}</span>
                <span className="text-[10.5px] truncate" style={{ color: e.state === "waiting" || e.state === "skipped" ? STATUS_TEXT.warning : C.textMuted }}>
                  {labelFor(e)} · {stateText(e)}
                </span>
              </Link>
              {e.state !== "started" && (
                <button
                  type="button"
                  onClick={() => unmark.mutate(e.task_id)}
                  disabled={unmark.isPending}
                  aria-label={t("removeAria", { title: e.title })}
                  title={t("remove")}
                  data-testid={`tonight-remove-${e.task_id}`}
                  className="shrink-0 inline-flex items-center justify-center min-w-[44px] min-h-[44px] sm:min-h-[36px] rounded-md cursor-pointer hover:bg-[var(--color-bg-elevated)] disabled:opacity-40"
                  style={{ color: C.textMuted }}
                >
                  <X size={13} aria-hidden />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {entries.length === 0 && (
        <p className="px-2 py-1 text-[11px]" style={{ color: C.textMuted }}>{t("empty")}</p>
      )}

      {report && (
        <p className="px-2 pt-1.5 text-[11px]" style={{ color: C.textSecondary }} data-testid="tonight-last-report">
          {t("lastNight", { night: report.night })}{" "}
          {NIGHT_CATEGORIES.filter((c) => c !== "running" || counts.running > 0)
            .map((c) => `${counts[c]} ${t(`category.${c}`)}`)
            .join(" · ")}
          {report.state === "undelivered" && <span style={{ color: STATUS_TEXT.warning }}> · {t("reportNotDelivered")}</span>}
        </p>
      )}
    </section>
  );
}
