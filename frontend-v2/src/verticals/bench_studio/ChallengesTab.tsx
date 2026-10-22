"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Plus, FlaskConical, Archive } from "lucide-react";
import { C } from "@/lib/colors";
import { Pill } from "@/components/shared/Pill";
import { benchApi } from "@/verticals/bench_studio/api";
import { ChallengeDetail } from "./ChallengeDetail";
import { NewChallengeDialog } from "./NewChallengeDialog";
import { abortReason } from "./status";
import { startedAt } from "./format";
import type { BenchChallenge, BenchEntry, PromptTemplate } from "./types";

// Status palette (DESIGN "Status & Lanes"): colour means status. Running is
// info blue; a finished challenge waits for the operator, which is the accent
// (the user_test rule) — never the warning ochre it used to be (round-1 t4 N6).
export const BENCH_STATUS_COLOR: Record<string, string> = {
  generating: C.info,
  rendering: C.info,
  composing: C.info,
  review: C.accent,
  drafted: C.accent,
  published: C.online,
  failed: C.error,
};

export const ENTRY_STATUS_COLOR: Record<string, string> = {
  pending: C.textMuted,
  generating: C.info,
  // HTML is there, the recording is not yet: an in-between step, not a warning.
  generated: C.textSecondary,
  rendered: C.online,
  failed: C.error,
};

/** Status pill with the i18n word for the state (K10); a failed challenge
 *  also names why it ended ("Aborted – task deleted") when the reason is
 *  known. Shared by the list row and the detail header. */
export function ChallengeStatusPill({ challenge }: { challenge: BenchChallenge }) {
  const t = useTranslations("bench.status");
  const reason = abortReason(challenge);
  const label = reason
    ? t("abortedWithReason", { reason: t(`reason.${reason}`) })
    : t.has(challenge.status)
      ? t(challenge.status)
      : challenge.status;
  return <Pill color={BENCH_STATUS_COLOR[challenge.status] ?? C.textMuted}>{label}</Pill>;
}

/** The backend's raw error line — only when the pill could not name the
 *  reason itself (never the same fact twice, K3). */
export function challengeErrorLine(challenge: BenchChallenge): string | null {
  if (!challenge.error) return null;
  return abortReason(challenge) ? null : challenge.error;
}

/** The entry pill's word for its state (K10), the raw code only for a state
 *  the catalog does not know yet. */
export function useEntryStatusWord(): (status: string) => string {
  const t = useTranslations("bench.entryStatus");
  return (status) => (t.has(status) ? t(status) : status);
}

export function ChallengesTab({
  prefillTemplate,
  onPrefillConsumed,
  selectedId: selectedIdProp,
  onSelectedChange,
}: {
  prefillTemplate: PromptTemplate | null;
  onPrefillConsumed: () => void;
  /** Controlled by the page so the open challenge lives in the URL
   *  (`/bench?challenge=<id>` — reload and share keep it, t2 SOLL-4).
   *  Left out, the tab keeps the selection itself. */
  selectedId?: string | null;
  onSelectedChange?: (id: string | null) => void;
}) {
  const t = useTranslations("bench.challengesTab");
  const tCommon = useTranslations("bench.common");
  const [ownSelectedId, setOwnSelectedId] = useState<string | null>(null);
  const controlled = onSelectedChange !== undefined;
  const selectedId = controlled ? selectedIdProp ?? null : ownSelectedId;
  const setSelectedId = controlled ? onSelectedChange : setOwnSelectedId;
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
      {/* Phone: the count may take its own line, the buttons never break
          their words ("Neue / Challenge", r2 S4). */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm whitespace-nowrap" style={{ color: C.textSecondary }}>
          {t("count", { count: rows.length })}
        </span>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowArchived((v) => !v)}
            aria-pressed={showArchived}
            className="flex items-center gap-2 px-3 min-h-11 rounded-lg text-sm whitespace-nowrap"
            style={{
              color: showArchived ? C.accent : C.textSecondary,
              border: `1px solid ${showArchived ? C.borderAccent : C.border}`,
            }}
          >
            <Archive size={13} /> {t("showArchived")}
          </button>
          <button
            onClick={() => setCreateOpen(true)}
            className="flex items-center gap-2 px-3 min-h-11 rounded-lg text-sm font-medium whitespace-nowrap"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            <Plus size={14} /> {t("newChallenge")}
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
            {t("emptyState")}
          </span>
        </div>
      )}

      {rows.map((ch) => (
        <ChallengeRow key={ch.id} challenge={ch} onOpen={() => setSelectedId(ch.id)} archivedLabel={tCommon("archivedPill")} />
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
  archivedLabel,
}: {
  challenge: BenchChallenge;
  onOpen: () => void;
  archivedLabel: string;
}) {
  const locale = useLocale();
  const errorLine = challengeErrorLine(challenge);
  const started = startedAt(challenge.created_at, locale);
  // A named reason makes the pill long ("Aborted – task deleted"): it gets
  // its own line under the entries so the title and labels keep their width
  // on a phone; short states stay in the top-right corner.
  const longStatus = abortReason(challenge) !== null;
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
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2">
            {/* Seven "Pagoda - OMP" rows were told apart by nothing (t2
                SOLL-4): the start time leads the meta line. */}
            {started && (
              <time
                dateTime={challenge.created_at}
                data-testid="bench-row-started"
                className="text-xs whitespace-nowrap"
                style={{ color: C.textMuted }}
              >
                {started}
              </time>
            )}
            {challenge.entries.map((e) => (
              <EntryProgress key={e.id} entry={e} />
            ))}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {challenge.archived_at && (
            <Pill color={C.textMuted} variant="outline">
              {archivedLabel}
            </Pill>
          )}
          {!longStatus && <ChallengeStatusPill challenge={challenge} />}
        </div>
      </div>
      {longStatus && (
        <div className="mt-2">
          <ChallengeStatusPill challenge={challenge} />
        </div>
      )}
      {errorLine && (
        <div className="mt-2 text-xs" style={{ color: C.error }}>
          {errorLine}
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
