"use client";

/**
 * ActionBar — Zone 4 (Spec §2). Switch model (primär, wiederverwendet den
 * bestehenden HostRecipeSwitcher-Auslöser) · Zahnrad (Cockpit — vorerst das
 * bestehende RuntimeDetailPanel der Head-Runtime, das eigentliche Cockpit
 * kommt in PR 5) · Stop (Ghost, roter Text).
 *
 * Stop-Dispatch-Gate (Spec §5, Assumption zur genauen 409-Form — Backend-PR
 * parallel in Arbeit): ein 409 mit `agent`/`task` im Body zeigt eine inline
 * Bestätigungszeile ("Stop anyway") statt window.confirm; ein Klick darauf
 * wiederholt den Stop mit `force=true`. Jeder andere Fehler zeigt nur den
 * Satz aus humanApiError.
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Settings } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { RuntimeStopConflict } from "@/lib/types";
import { humanApiError } from "@/components/shared/HostRecipeSwitcher";
import { HostRecipeSwitcher } from "@/components/shared/HostRecipeSwitcher";

function parseStopConflict(err: Error): RuntimeStopConflict | null {
  const m = /^API 409: ([\s\S]*)$/.exec(err.message);
  if (!m) return null;
  try {
    const parsed = JSON.parse(m[1]) as { detail?: unknown };
    const detail = parsed.detail;
    if (detail && typeof detail === "object" && "agent" in detail && "task" in detail) {
      return detail as RuntimeStopConflict;
    }
  } catch {
    // kein JSON-Detail — kein strukturierter Konflikt
  }
  return null;
}

export function ActionBar({
  hostId,
  hostName,
  servingName,
  runtimeId,
  variant = "normal",
  onOpenCockpit,
}: {
  hostId: string;
  hostName: string | null;
  servingName: string | null;
  runtimeId: string;
  /** "trouble" = Störung (Spec §2 Zone 4): primär wird "Restart now", zweite
   *  Reihe "Other model" + Stop. */
  variant?: "normal" | "trouble";
  onOpenCockpit: () => void;
}) {
  const t = useTranslations("runtimes.stage");
  const queryClient = useQueryClient();
  const [conflict, setConflict] = useState<RuntimeStopConflict | null>(null);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    queryClient.invalidateQueries({ queryKey: ["runtimes", "live-status"] });
    queryClient.invalidateQueries({ queryKey: ["hosts"] });
  };

  const stopMutation = useMutation({
    mutationFn: (force: boolean) => api.runtimes.stop(runtimeId, { force }),
    onMutate: () => {
      setError(null);
    },
    onSuccess: () => {
      setConflict(null);
      invalidate();
    },
    onError: (err: Error) => {
      const parsed = parseStopConflict(err);
      if (parsed) {
        setConflict(parsed);
        return;
      }
      setError(t("stopFailed", { message: humanApiError(err) }));
    },
  });

  const restartMutation = useMutation({
    mutationFn: () => api.runtimes.restart(runtimeId),
    onSuccess: invalidate,
    onError: (err: Error) => setError(t("restartFailed", { message: humanApiError(err) })),
  });

  if (conflict) {
    return (
      <div
        className="flex items-center gap-3 px-4 py-3 flex-wrap text-xs"
        style={{ borderTop: `1px solid ${C.borderSubtle}`, color: C.textSecondary }}
        data-testid="stop-conflict-row"
      >
        <span>{t("stopConflict", { agent: conflict.agent, task: conflict.task })}</span>
        <div className="flex items-center gap-2 ml-auto">
          <button
            type="button"
            onClick={() => setConflict(null)}
            className="px-3 py-2 rounded-md cursor-pointer"
            style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
          >
            {t("cancel")}
          </button>
          <button
            type="button"
            onClick={() => stopMutation.mutate(true)}
            disabled={stopMutation.isPending}
            data-testid="stop-anyway"
            className="px-3 py-2 rounded-md cursor-pointer disabled:opacity-50"
            style={{ border: `1px solid ${C.error}`, color: C.error }}
          >
            {t("stopAnyway")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 px-4 py-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
      {error && (
        <div className="text-xs" style={{ color: C.error }}>{error}</div>
      )}
      <div className="flex items-center gap-2 flex-wrap">
        {variant === "trouble" ? (
          <button
            type="button"
            onClick={() => restartMutation.mutate()}
            disabled={restartMutation.isPending}
            data-testid="restart-now"
            className="flex-1 sm:flex-none text-xs font-medium px-3.5 py-2.5 rounded-md cursor-pointer disabled:opacity-50"
            style={{ background: C.accent, color: C.onAccent }}
          >
            {restartMutation.isPending ? t("restarting") : t("restartNow")}
          </button>
        ) : (
          <HostRecipeSwitcher hostId={hostId} hostName={hostName} servingName={servingName} compact />
        )}
        <button
          type="button"
          onClick={onOpenCockpit}
          aria-label={t("cockpitAria")}
          className="w-9 h-9 flex items-center justify-center rounded-md cursor-pointer shrink-0"
          style={{ border: `1px solid ${C.borderActive}` }}
        >
          <Settings size={14} style={{ color: C.textSecondary }} />
        </button>
        <div className="flex-1 flex items-center gap-2 flex-wrap justify-end">
          {variant === "trouble" && (
            <HostRecipeSwitcher hostId={hostId} hostName={hostName} servingName={servingName} compact />
          )}
          <button
            type="button"
            onClick={() => stopMutation.mutate(false)}
            disabled={stopMutation.isPending}
            data-testid="stop-runtime"
            className="text-xs px-3.5 py-2.5 rounded-md cursor-pointer disabled:opacity-50"
            style={{ color: C.error, border: `1px solid ${C.borderSubtle}` }}
          >
            {stopMutation.isPending ? t("stopping") : t("stop")}
          </button>
        </div>
      </div>
    </div>
  );
}
