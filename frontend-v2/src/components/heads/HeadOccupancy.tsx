"use client";

/**
 * Box occupancy on /runtimes (docs/specs/head-launcher.md §8.3):
 *
 *   useHeadOccupancy()   {host id → working head}; empty while heads are off
 *   HeadOnBoxNotice      "⚠ A head is working on this box … Stop the head first."
 *                        — only after a refused action (409), never permanent;
 *                        the model card shows the reason when switch/stop is
 *                        clicked (ActionBar) and counts heads under "In use".
 *   useHeadConflictText  409 head_on_box / engine_busy → one translated sentence
 *   HeadOrphanRuns       active runs whose task card was deleted, with Stop
 */

import Link from "next/link";
import { useTranslations, useLocale } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import { formatDuration } from "@/lib/taskDetail/format";
import {
  headErrorKey,
  parseHeadError,
  parseHeadOnBox,
  runDurationSeconds,
  runPairLabel,
  type HeadBusy,
  type HeadRun,
} from "@/lib/heads";
import { HeadStopButton } from "./HeadStopButton";
import { useHeadsEnabled } from "./useHeadsEnabled";
import { useHeadPairsForLabels } from "./HeadStateCard";

export function useHeadOccupancy(): Record<string, HeadBusy> {
  const q = useQuery({
    queryKey: ["heads", "occupancy"],
    queryFn: () => api.heads.occupancy(),
    retry: false,
    refetchInterval: 15_000,
  });
  const boxes = q.data?.boxes;
  return boxes && typeof boxes === "object" ? boxes : {};
}

/** First working head on any of these boxes (a duo holds two). */
export function headOnBoxes(occupancy: Record<string, HeadBusy>, hostIds: (string | null | undefined)[]): HeadBusy | null {
  for (const id of hostIds) {
    if (id && occupancy[id]) return occupancy[id];
  }
  return null;
}

function taskHref(taskId: string) {
  return `/tasks?task=${encodeURIComponent(taskId)}`;
}

export function HeadOnBoxNotice({ head }: { head: { task_id: string | null; title: string | null } }) {
  const t = useTranslations("heads.runtimes");
  return (
    <div
      role="status"
      data-testid="head-on-box-notice"
      className="flex flex-col sm:flex-row sm:items-center gap-2 rounded-md px-3 py-2.5 text-xs"
      style={{ background: alpha(C.warning, 0.07), border: `1px solid ${alpha(C.warning, 0.25)}`, color: STATUS_TEXT.warning }}
    >
      <div className="flex items-start gap-2 min-w-0 flex-1">
        <AlertTriangle size={13} className="shrink-0 mt-0.5" aria-hidden />
        <span>
          {head.title ? t("onBoxTitle", { title: head.title }) : t("onBoxTitleNoTitle")} {t("onBoxBody")}
        </span>
      </div>
      {head.task_id && (
        <Link
          href={taskHref(head.task_id)}
          className="inline-flex items-center justify-center px-3 min-h-[36px] pointer-coarse:min-h-[44px] rounded-md shrink-0 hover:bg-[var(--color-bg-hover)]"
          style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
        >
          {t("openTask")}
        </Link>
      )}
    </div>
  );
}

/** Sentence for a refusal from the box guard, or null for any other error. */
export function useHeadConflictText() {
  const t = useTranslations("heads");
  return (err: unknown): string | null => {
    const onBox = parseHeadOnBox(err);
    if (onBox) {
      return `${onBox.title ? t("runtimes.onBoxTitle", { title: onBox.title }) : t("runtimes.onBoxTitleNoTitle")} ${t("runtimes.onBoxBody")}`;
    }
    const { status, code } = parseHeadError(err);
    if (status === 409 && code === "engine_busy") return t("errors.engine_busy");
    return null;
  };
}

/** Active heads whose task card was deleted — otherwise nobody could stop them. */
export function HeadOrphanRuns() {
  const t = useTranslations("heads.runtimes");
  const tHeads = useTranslations("heads");
  const locale = useLocale();
  const qc = useQueryClient();
  const headsEnabled = useHeadsEnabled();
  const q = useQuery({
    queryKey: ["heads", "active"],
    queryFn: () => api.heads.list({ active: true }),
    enabled: headsEnabled === true,
    retry: false,
    refetchInterval: 15_000,
  });
  const stop = useMutation({
    mutationFn: (runId: string) => api.heads.stop(runId),
    onSuccess: () => {
      notify.success(tHeads("card.stopRequested"));
      qc.invalidateQueries({ queryKey: ["heads"] });
    },
    onError: (err) => notify.error(tHeads(headErrorKey(err))),
  });
  const runs: HeadRun[] = Array.isArray(q.data?.runs) ? q.data!.runs.filter((r) => r.task_deleted) : [];
  // Display names instead of runtime slugs — only fetched when there is a row.
  const pairs = useHeadPairsForLabels(runs.length > 0);
  if (runs.length === 0) return null;
  return (
    <section data-testid="head-orphan-runs">
      <div className="flex items-center gap-2.5 mb-2">
        <span className="text-[10px] font-medium uppercase shrink-0" style={{ color: C.textMuted, letterSpacing: "0.08em" }}>
          {t("orphanTitle")}
        </span>
        <div className="flex-1 h-px" style={{ background: C.borderSubtle }} />
      </div>
      <ul className="space-y-1.5">
        {runs.map((run) => (
          <li
            key={run.run_id}
            className="flex items-center gap-3 rounded-md px-3 py-2 text-xs"
            style={{ background: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
          >
            <span className="min-w-0 flex-1 truncate" style={{ color: C.textSecondary }}>
              {t("orphanRow", { pair: runPairLabel(run, pairs), duration: formatDuration(runDurationSeconds(run), locale) ?? "—" })}
            </span>
            <span className="shrink-0">
              <HeadStopButton
                onStop={() => stop.mutate(run.run_id)}
                pending={stop.isPending && stop.variables === run.run_id}
                label={t("stop")}
                testId={`head-orphan-stop-${run.run_id}`}
              />
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
