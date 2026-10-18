"use client";

/**
 * RecipeStartProgress — what a "Create runtime & start" click is doing right now.
 *
 * Field finding (2026-10-18): after the click the deploy dialog kept showing
 * only the old install log; the start itself (memory prep, launch over SSH,
 * then 8–30 minutes of weight loading) had no visible trace. This block
 * reads what MC already knows — no new mechanism:
 *
 *   - while the start request runs: "sending to the box"
 *   - afterwards: the watcher's live status for the instance
 *     (`GET /runtimes/live-status`, the same feed as the runtimes page):
 *     `status: "switching"` + `phase` (evicting / launching / loading) while
 *     the planned downtime lasts, `reachable` once the engine answers.
 */

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { CheckCircle2, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import type { RuntimeLiveStatus } from "@/lib/types";

export type RecipeStartStage =
  | "sending"
  | "waiting"
  | "evicting"
  | "launching"
  | "loading"
  | "starting"
  | "serving"
  | "already"
  | "quiet";

/**
 * One stage from the request state and the watcher's live entry. Pure, so the
 * mapping is tested without a timer.
 */
export function recipeStartStage(
  sending: boolean,
  alreadyRunning: boolean,
  live: RuntimeLiveStatus | undefined,
): RecipeStartStage {
  if (sending) return "sending";
  if (alreadyRunning) return "already";
  if (!live) return "waiting";
  if (live.status === "switching") {
    if (live.phase === "evicting" || live.phase === "launching" || live.phase === "loading") {
      return live.phase;
    }
    return "starting";
  }
  if (live.reachable) return "serving";
  // Not answering and no planned-downtime marker: the start was accepted, but
  // the watcher has not seen the engine yet (or it stopped). Say exactly that.
  return "quiet";
}

/** Poll while something is still happening; stop once it serves. */
const POLL_MS = 5_000;

export function RecipeStartProgress({
  runtimeSlug,
  sending,
  alreadyRunning = false,
}: {
  runtimeSlug: string | null;
  sending: boolean;
  alreadyRunning?: boolean;
}) {
  const t = useTranslations("runtimes.localRegistry");
  const enabled = !!runtimeSlug && !sending && !alreadyRunning;
  const liveQuery = useQuery({
    queryKey: ["runtimes", "live-status"],
    queryFn: () => api.runtimes.liveStatus(),
    enabled,
    refetchInterval: (query) => {
      const entry = runtimeSlug ? query.state.data?.live?.[runtimeSlug] : undefined;
      return recipeStartStage(false, false, entry) === "serving" ? false : POLL_MS;
    },
  });
  const live = runtimeSlug ? liveQuery.data?.live?.[runtimeSlug] : undefined;
  const stage = recipeStartStage(sending, alreadyRunning, enabled ? live : undefined);
  const done = stage === "serving" || stage === "already";

  return (
    <div
      data-testid="recipe-start-progress"
      data-stage={stage}
      role="status"
      aria-live="polite"
      className="flex flex-col gap-1 rounded-md border border-subtle bg-surface px-3 py-2"
    >
      <span className="label-sys label-sys--dim">{t("startProgressLabel")}</span>
      <div className={`flex items-start gap-2 text-xs ${done ? "text-ok" : "text-primary"}`}>
        {done ? (
          <CheckCircle2 size={12} className="shrink-0 mt-1" />
        ) : (
          <Loader2 size={12} className="shrink-0 mt-1 animate-spin" />
        )}
        <span>
          {stage === "serving" && live?.served_model
            ? t("startStage.servingModel", { model: live.served_model })
            : t(`startStage.${stage}`)}
        </span>
      </div>
      {!done && stage !== "sending" && (
        <span className="text-xs text-muted">{t("startProgressHint")}</span>
      )}
    </div>
  );
}
