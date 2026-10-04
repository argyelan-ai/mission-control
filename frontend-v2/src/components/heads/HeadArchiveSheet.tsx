"use client";

/**
 * HeadArchiveSheet — "Archive · N older heads" (bauplan `heads-sichtbar`
 * PR 2 §3.2). A read-only sheet over `GET /heads?archived=true`: client-side
 * search by title/pair, same row component the active list uses
 * (`HeadChatRow`), nothing more — the whole point of the archive is "find
 * it again", not act on it.
 */
import { useMemo, useState } from "react";
import { useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { Search, X } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { headListTitle, pairShort, sortRunsNewestFirst, type HeadRun } from "@/lib/heads";
import { HEAD_RUNS_RECENT_DAYS } from "./useHeadRuns";
import { HeadChatRow } from "./HeadChatRow";

function matches(run: HeadRun, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return headListTitle(run).toLowerCase().includes(q) || pairShort(run).toLowerCase().includes(q);
}

interface HeadArchiveSheetProps {
  open: boolean;
  onClose: () => void;
  onSelectHead: (runId: string) => void;
  selectedHeadId?: string | null;
}

export function HeadArchiveSheet({ open, onClose, onSelectHead, selectedHeadId = null }: HeadArchiveSheetProps) {
  const t = useTranslations("heads");
  const [query, setQuery] = useState("");

  const archiveQuery = useQuery({
    queryKey: ["heads", "runs", "archived"],
    queryFn: () => api.heads.list({ recentDays: HEAD_RUNS_RECENT_DAYS, archived: true }),
    enabled: open,
    staleTime: 30_000,
  });

  const runs = useMemo(() => sortRunsNewestFirst(archiveQuery.data?.runs ?? []), [archiveQuery.data]);
  const filtered = useMemo(() => runs.filter((r) => matches(r, query)), [runs, query]);

  return (
    <ResponsiveModal open={open} onClose={onClose} dismissOnOutside={false} aria-labelledby="head-archive-title">
      <div className="flex flex-col min-h-0 max-h-[80vh]" data-testid="head-archive-sheet">
        <div className="flex items-center gap-2 px-4 py-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
          <h2 id="head-archive-title" className="text-sm font-semibold flex-1 min-w-0 truncate" style={{ color: C.textPrimary }}>
            {t("list.archiveTitle")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("list.archiveTitle")}
            data-testid="head-archive-close"
            className="shrink-0 flex items-center justify-center w-9 h-9 rounded-md cursor-pointer"
            style={{ color: C.textMuted }}
          >
            <X size={16} aria-hidden />
          </button>
        </div>

        <div className="px-4 py-2 shrink-0">
          <div className="relative">
            <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2" style={{ color: C.textDim }} aria-hidden />
            <input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("list.searchPlaceholder")}
              data-testid="head-archive-search"
              className="w-full pl-8 pr-3 py-2 text-sm rounded-md"
              style={{ background: "var(--color-bg-hover)", color: C.textPrimary, border: `1px solid ${C.border}` }}
            />
          </div>
        </div>

        <div className="flex-1 min-h-0 overflow-y-auto pb-2">
          {archiveQuery.isLoading ? null : runs.length === 0 ? (
            <p className="px-4 py-6 text-xs" style={{ color: C.textMuted }}>
              {t("list.empty")}
            </p>
          ) : filtered.length === 0 ? (
            <p className="px-4 py-6 text-xs" style={{ color: C.textMuted }}>
              {t("list.noResults")}
            </p>
          ) : (
            <div className="flex flex-col" role="listbox" aria-label={t("list.archiveTitle")}>
              {filtered.map((run) => (
                <HeadChatRow
                  key={run.run_id}
                  run={run}
                  variant="list"
                  selected={run.run_id === selectedHeadId}
                  onSelect={(id) => {
                    onSelectHead(id);
                    onClose();
                  }}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </ResponsiveModal>
  );
}
