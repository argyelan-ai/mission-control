"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Download, RefreshCw, RotateCcw, Send } from "lucide-react";
import { api, getToken } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { Pill } from "@/components/shared/Pill";
import { FilePreview } from "@/components/task/FilePreview";
import { benchApi } from "@/verticals/bench_studio/api";
import { BENCH_STATUS_COLOR, ENTRY_STATUS_COLOR } from "./ChallengesTab";
import { DraftDialog } from "./DraftDialog";
import type { BenchEntry } from "./types";

function sharedUrl(absPath: string): string {
  return api.files.contentUrl("shared-deliverables", benchApi.sharedSubpath(absPath));
}

async function downloadFile(absPath: string, filename: string) {
  const res = await fetch(sharedUrl(absPath), {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) {
    notify.error("Download fehlgeschlagen");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function metricsLine(m: BenchEntry["metrics"]): string {
  const parts: string[] = [];
  if (m.duration_ms) parts.push(`${(m.duration_ms / 1000).toFixed(0)} s`);
  if (m.tok_per_s) parts.push(`${m.tok_per_s.toFixed(0)} tok/s`);
  if (m.tokens_out) parts.push(`${m.tokens_out} tok`);
  return parts.join(" · ");
}

export function ChallengeDetail({
  challengeId,
  onBack,
}: {
  challengeId: string;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  const [draftOpen, setDraftOpen] = useState(false);

  const { data: challenge } = useQuery({
    queryKey: ["bench-challenge", challengeId],
    queryFn: () => benchApi.challenges.get(challengeId),
    refetchInterval: 5000, // polling — no generic SSE hook for bench
  });

  const rerenderMutation = useMutation({
    mutationFn: () => benchApi.challenges.rerender(challengeId),
    onSuccess: () => {
      notify.success("Rerender gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    },
    onError: () => notify.error("Rerender nicht möglich"),
  });

  const retryMutation = useMutation({
    mutationFn: (entryId: string) => benchApi.entries.retry(entryId),
    onSuccess: () => {
      notify.success("Retry gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    },
    onError: () => notify.error("Retry nicht möglich"),
  });

  if (!challenge) return null;
  const canDraft = challenge.status === "review" || challenge.status === "drafted";
  const canRerender = ["review", "drafted", "failed"].includes(challenge.status);

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <button onClick={onBack} aria-label="Zurück" style={{ color: C.textSecondary }}>
            <ArrowLeft size={18} />
          </button>
          <h2 className="text-lg font-semibold truncate" style={{ color: C.textPrimary }}>
            {challenge.title}
          </h2>
          <Pill color={BENCH_STATUS_COLOR[challenge.status] ?? C.textMuted}>
            {challenge.status}
          </Pill>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            onClick={() => rerenderMutation.mutate()}
            disabled={!canRerender}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm disabled:opacity-40"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            <RefreshCw size={13} /> Neu rendern
          </button>
          <button
            onClick={() => setDraftOpen(true)}
            disabled={!canDraft}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            <Send size={13} /> Draft erstellen
          </button>
        </div>
      </div>

      {challenge.error && (
        <div
          className="rounded-lg px-3 py-2 text-sm"
          style={{ color: C.error, border: `1px solid ${C.error}40`, backgroundColor: `${C.error}10` }}
        >
          {challenge.error}
        </div>
      )}

      {/* Grid video (side_by_side) */}
      {challenge.composed_video_path && (
        <section>
          <h3 className="text-sm font-medium mb-2" style={{ color: C.textSecondary }}>
            Grid-Video
          </h3>
          <div className="rounded-xl p-3" style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.borderSubtle}` }}>
            <FilePreview
              fileUrl={sharedUrl(challenge.composed_video_path)}
              path={challenge.composed_video_path}
            />
          </div>
        </section>
      )}

      {/* Per-model gallery */}
      <section className="grid gap-4 sm:grid-cols-2">
        {challenge.entries.map((entry) => (
          <div
            key={entry.id}
            className="rounded-xl p-3 flex flex-col gap-2"
            style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
                {entry.model_label}
              </span>
              <Pill color={ENTRY_STATUS_COLOR[entry.status] ?? C.textMuted}>
                {entry.status}
              </Pill>
            </div>
            {metricsLine(entry.metrics) && (
              <span className="text-xs font-mono" style={{ color: C.textMuted }}>
                {metricsLine(entry.metrics)}
              </span>
            )}
            {entry.video_path ? (
              <FilePreview fileUrl={sharedUrl(entry.video_path)} path={entry.video_path} />
            ) : entry.screenshot_path ? (
              <FilePreview fileUrl={sharedUrl(entry.screenshot_path)} path={entry.screenshot_path} />
            ) : null}
            {entry.error && (
              <span className="text-xs" style={{ color: C.error }}>{entry.error}</span>
            )}
            <div className="flex items-center gap-2 mt-auto">
              {entry.artifact_path && (
                <button
                  onClick={() =>
                    downloadFile(entry.artifact_path!, `${entry.model_label}-index.html`)
                  }
                  className="flex items-center gap-1 text-xs"
                  style={{ color: C.textSecondary }}
                >
                  <Download size={12} /> HTML
                </button>
              )}
              {entry.status === "failed" && (
                <button
                  onClick={() => retryMutation.mutate(entry.id)}
                  className="flex items-center gap-1 text-xs"
                  style={{ color: C.accent }}
                >
                  <RotateCcw size={12} /> Retry
                </button>
              )}
            </div>
          </div>
        ))}
      </section>

      <DraftDialog challenge={challenge} open={draftOpen} onClose={() => setDraftOpen(false)} />
    </div>
  );
}
