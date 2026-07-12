"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Plus, FlaskConical, Archive } from "lucide-react";
import { C } from "@/lib/colors";
import { Pill } from "@/components/shared/Pill";
import { benchApi } from "@/verticals/bench_studio/api";
import { ChallengeDetail } from "./ChallengeDetail";
import { NewChallengeDialog } from "./NewChallengeDialog";
import type { BenchChallenge, BenchEntry, PromptTemplate } from "./types";

// Muted Leitstand status palette (no purple, no glow):
export const BENCH_STATUS_COLOR: Record<string, string> = {
  generating: C.info,
  rendering: C.info,
  composing: C.info,
  review: C.warning,
  drafted: C.accent,
  published: C.online,
  failed: C.error,
};

export const ENTRY_STATUS_COLOR: Record<string, string> = {
  pending: C.textMuted,
  generating: C.info,
  generated: C.warning,
  rendered: C.online,
  failed: C.error,
};

export function ChallengesTab({
  prefillTemplate,
  onPrefillConsumed,
}: {
  prefillTemplate: PromptTemplate | null;
  onPrefillConsumed: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  // Prefill from Prompt Library ("Challenge starten"):
  useEffect(() => {
    if (prefillTemplate) setCreateOpen(true);
  }, [prefillTemplate]);

  // Live progress via 5s polling — no generic SSE hook exists for bench.
  const { data: challenges } = useQuery({
    queryKey: ["bench-challenges", showArchived],
    queryFn: () => benchApi.challenges.list(showArchived),
    refetchInterval: 5000,
  });

  if (selectedId) {
    return <ChallengeDetail challengeId={selectedId} onBack={() => setSelectedId(null)} />;
  }

  const rows = challenges ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-sm" style={{ color: C.textSecondary }}>
          {rows.length} Challenges
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowArchived((v) => !v)}
            aria-pressed={showArchived}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm"
            style={{
              color: showArchived ? C.accent : C.textSecondary,
              border: `1px solid ${showArchived ? C.borderAccent : C.border}`,
            }}
          >
            <Archive size={13} /> Archiv anzeigen
          </button>
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            <Plus size={14} /> Neue Challenge
          </button>
        </div>
      </div>

      {rows.length === 0 && (
        <div
          className="flex flex-col items-center gap-2 py-16 rounded-xl"
          style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
        >
          <FlaskConical size={22} style={{ color: C.textDim }} />
          <span className="text-sm" style={{ color: C.textSecondary }}>
            Noch keine Challenges — starte die erste aus der Prompt Library.
          </span>
        </div>
      )}

      {rows.map((ch) => (
        <ChallengeRow key={ch.id} challenge={ch} onOpen={() => setSelectedId(ch.id)} />
      ))}

      <NewChallengeDialog
        open={createOpen}
        onClose={() => {
          setCreateOpen(false);
          onPrefillConsumed();
        }}
        prefillTemplate={prefillTemplate}
      />
    </div>
  );
}

function ChallengeRow({
  challenge,
  onOpen,
}: {
  challenge: BenchChallenge;
  onOpen: () => void;
}) {
  return (
    <button
      onClick={onOpen}
      className="text-left rounded-xl px-4 py-3 transition-colors"
      style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
              {challenge.title}
            </span>
            {challenge.series_label && challenge.series_no != null && (
              <span className="text-xs font-mono shrink-0" style={{ color: C.textMuted }}>
                {challenge.series_label} #{challenge.series_no}
              </span>
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5">
            {challenge.entries.map((e) => (
              <EntryProgress key={e.id} entry={e} />
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {challenge.archived_at && (
            <Pill color={C.textMuted} variant="outline">
              archiviert
            </Pill>
          )}
          <Pill color={BENCH_STATUS_COLOR[challenge.status] ?? C.textMuted}>
            {challenge.status}
          </Pill>
        </div>
      </div>
      {challenge.error && (
        <div className="mt-2 text-xs" style={{ color: C.error }}>
          {challenge.error}
        </div>
      )}
    </button>
  );
}

function EntryProgress({ entry }: { entry: BenchEntry }) {
  const color = ENTRY_STATUS_COLOR[entry.status] ?? C.textMuted;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs" style={{ color: C.textSecondary }}>
      <span
        aria-hidden
        className="inline-block w-1.5 h-1.5 rounded-full"
        style={{ backgroundColor: color }}
      />
      {entry.model_label}
    </span>
  );
}
