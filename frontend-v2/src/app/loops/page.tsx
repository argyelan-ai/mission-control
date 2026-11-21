"use client";

/**
 * Loops page (ADR-051) — outcome-driven task loops. A runner spins up one
 * normal parent task per round; once the round ends it decides to continue,
 * pause, escalate, or finish. This page lists loops on the active board,
 * lets the operator start/pause/stop/delete them, and shows the
 * round-by-round history in a detail drawer.
 *
 * Frozen since Etappe 2 (ADR-102 §3, lib/nav.ts FROZEN_ITEMS): the page left
 * every menu and is reachable by URL only. Create, Start and Delete are hidden
 * here and refused by the API (410 "frozen"); Pause and Stop stay as the safe
 * way out of a running loop. The code stays for the way back — take /loops out
 * of FROZEN_ITEMS (and set FROZEN = False in backend/app/routers/loops.py).
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, Loader2, Plus, Repeat, X } from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { LoopCard } from "@/components/loops/LoopCard";
import { LoopDetailPanel } from "@/components/loops/LoopDetailPanel";
import { CreateLoopDialog } from "@/components/loops/CreateLoopDialog";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import { isFrozenRoute } from "@/lib/nav";
import { C, alpha } from "@/lib/colors";
import type { Loop } from "@/lib/types";

/**
 * Pull the human-readable detail out of `API 409: {"detail":"..."}` errors,
 * and out of `API 410: {"detail":{"code":"frozen","message":"..."}}`.
 */
function extractApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart));
      if (typeof parsed.detail === "string") return parsed.detail;
      if (typeof parsed.detail?.message === "string") return parsed.detail.message;
    } catch {
      /* fall through to raw message */
    }
  }
  return msg;
}

export default function LoopsPage() {
  const t = useTranslations("loops");
  const frozen = isFrozenRoute("/loops");
  const qc = useQueryClient();
  const activeBoardId = useAppStore((s) => s.activeBoardId);
  const [createOpen, setCreateOpen] = useState(false);
  const [selectedLoopId, setSelectedLoopId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Loop | null>(null);

  const { data: loops = [], isLoading } = useQuery({
    queryKey: ["loops", activeBoardId],
    queryFn: () => api.loops.list(activeBoardId ?? undefined),
    enabled: !!activeBoardId,
    refetchInterval: 15_000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["loops", activeBoardId] });
    qc.invalidateQueries({ queryKey: ["loop", selectedLoopId] });
  };

  const startMutation = useMutation({
    mutationFn: (id: string) => api.loops.start(id),
    onMutate: (id) => { setPendingId(id); setActionError(null); },
    onSuccess: invalidate,
    onError: (e) => setActionError(extractApiError(e)),
    onSettled: () => setPendingId(null),
  });

  const pauseMutation = useMutation({
    mutationFn: (id: string) => api.loops.pause(id),
    onMutate: (id) => { setPendingId(id); setActionError(null); },
    onSuccess: invalidate,
    onError: (e) => setActionError(extractApiError(e)),
    onSettled: () => setPendingId(null),
  });

  const stopMutation = useMutation({
    mutationFn: (id: string) => api.loops.stop(id),
    onMutate: (id) => { setPendingId(id); setActionError(null); },
    onSuccess: invalidate,
    onError: (e) => setActionError(extractApiError(e)),
    onSettled: () => setPendingId(null),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.loops.remove(id),
    onMutate: (id) => { setPendingId(id); setActionError(null); },
    onSuccess: (_data, id) => {
      invalidate();
      if (selectedLoopId === id) setSelectedLoopId(null);
    },
    onError: (e) => setActionError(extractApiError(e)),
    onSettled: () => setPendingId(null),
  });

  const handleDelete = (loop: Loop) => {
    setDeleteTarget(loop);
  };

  return (
    <AppShell>
      <div className="py-6 max-w-4xl">
        {/* Header */}
        <div className="flex items-center justify-between gap-3 mb-6">
          <div>
            <div className="label-sys mb-2">Operations · Loops</div>
            <h1 className="display text-xl font-semibold flex items-center gap-2" style={{ color: C.textPrimary }}>
              <Repeat size={18} style={{ color: C.accent }} />
              {t("title")}
            </h1>
            <p className="text-[13px] mt-0.5" style={{ color: C.textSecondary }}>
              {t("subtitle")}
            </p>
          </div>
          {!frozen && (
          <button
            onClick={() => setCreateOpen(true)}
            disabled={!activeBoardId}
            className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer shrink-0 disabled:opacity-50 disabled:cursor-not-allowed"
            style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
          >
            <Plus size={11} />
            {t("newLoop")}
          </button>
          )}
        </div>

        {frozen && (
          <div
            role="status"
            data-testid="loops-frozen"
            className="flex items-start gap-2 rounded-md px-3 py-2 text-xs mb-4"
            style={{ background: C.bgSurface, border: `1px solid ${C.border}`, color: C.textSecondary }}
          >
            <Info size={14} className="shrink-0" style={{ color: C.textMuted }} />
            <span className="flex-1">
              <span className="font-medium" style={{ color: C.textPrimary }}>{t("frozenTitle")}</span>
              {" · "}
              {t("frozenBody")}
            </span>
          </div>
        )}

        {actionError && (
          <div
            className="flex items-start gap-2 rounded-md px-3 py-2 text-xs mb-4"
            style={{ background: alpha(C.error, 0.08), border: `1px solid ${alpha(C.error, 0.33)}`, color: C.error }}
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span className="flex-1">{actionError}</span>
            <button
              type="button"
              aria-label={t("dismissError")}
              onClick={() => setActionError(null)}
              className="cursor-pointer shrink-0"
            >
              <X size={13} />
            </button>
          </div>
        )}

        {!activeBoardId && (
          <div className="text-sm py-8 text-center" style={{ color: C.textMuted }}>
            {t("noBoard")}
          </div>
        )}

        {activeBoardId && isLoading && (
          <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
            <Loader2 size={13} className="animate-spin" />
            <span className="text-xs">{t("loading")}</span>
          </div>
        )}

        {activeBoardId && !isLoading && loops.length === 0 && (
          <div
            className="flex flex-col items-center gap-3 text-center py-16 rounded-xl"
            style={{ border: `1px dashed ${C.border}` }}
          >
            <Repeat size={28} style={{ color: C.textDim }} />
            <div>
              <p className="text-sm font-medium" style={{ color: C.textSecondary }}>
                {t("emptyTitle")}
              </p>
              {!frozen && (
                <p className="text-xs mt-1" style={{ color: C.textMuted }}>
                  {t("emptyHint")}
                </p>
              )}
            </div>
            {!frozen && (
            <button
              onClick={() => setCreateOpen(true)}
              className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer mt-1"
              style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
            >
              <Plus size={11} />
              {t("newLoop")}
            </button>
            )}
          </div>
        )}

        {activeBoardId && loops.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-2">
            {loops.map((loop) => (
              <LoopCard
                key={loop.id}
                loop={loop}
                onOpen={() => setSelectedLoopId(loop.id)}
                onStart={frozen ? undefined : () => startMutation.mutate(loop.id)}
                onPause={() => pauseMutation.mutate(loop.id)}
                onStop={() => stopMutation.mutate(loop.id)}
                onDelete={frozen ? undefined : () => handleDelete(loop)}
                actionPending={pendingId === loop.id}
              />
            ))}
          </div>
        )}
      </div>

      {!frozen && (
        <CreateLoopDialog
          open={createOpen}
          onClose={() => setCreateOpen(false)}
          onCreated={(loop) => setSelectedLoopId(loop.id)}
        />
      )}

      <LoopDetailPanel
        loopId={selectedLoopId}
        open={!!selectedLoopId}
        onClose={() => setSelectedLoopId(null)}
        onStart={frozen ? undefined : (id) => startMutation.mutate(id)}
        onPause={(id) => pauseMutation.mutate(id)}
        onStop={(id) => stopMutation.mutate(id)}
        onDelete={frozen ? undefined : (id) => {
          const loop = loops.find((l) => l.id === id);
          if (loop) handleDelete(loop);
        }}
        actionPending={!!pendingId}
      />

      {/* v3 confirm — replaces native confirm() (panel register rule 3) */}
      <ConfirmDialog
        open={!!deleteTarget}
        title={t("deleteTitle")}
        body={deleteTarget ? t("deleteBody", { name: deleteTarget.name }) : undefined}
        confirmLabel={t("delete")}
        loading={deleteMutation.isPending}
        onConfirm={() => {
          if (!deleteTarget) return;
          deleteMutation.mutate(deleteTarget.id, {
            onSettled: () => setDeleteTarget(null),
          });
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </AppShell>
  );
}
