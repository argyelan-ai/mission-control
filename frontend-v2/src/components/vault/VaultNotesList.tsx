"use client";

import { useRef, useCallback } from "react";
import { useLocale, useTranslations } from "next-intl";
import type { VaultNote } from "@/lib/types";
import { VaultNoteRow, MonthMarker, parseDateFromNote, formatMonthLabel } from "./VaultNoteRow";
import { VaultEmptyState } from "./VaultEmptyState";

// ── Grouping ──────────────────────────────────────────────────────────────────
//
// Group by MONTH using parseDateFromNote — pulls frontmatter.date first, then
// path stem, then null. Far fewer notes end up in "Undated" than the previous
// path-only logic (most vault notes name files by slug, not date prefix).

// Keys are locale-neutral ("2026-10"); the label is formatted in the UI
// language ("Oct 2026" / "Okt. 2026"). null label = undated.
function groupByMonth(
  notes: VaultNote[],
  locale: string,
): Array<{ key: string; label: string | null; notes: VaultNote[] }> {
  const groups: Map<string, { label: string | null; notes: VaultNote[] }> = new Map();

  for (const note of notes) {
    const date = parseDateFromNote(note);
    const key = date ? date.monthKey : "undated";
    if (!groups.has(key)) {
      groups.set(key, { label: date ? formatMonthLabel(date, locale) : null, notes: [] });
    }
    groups.get(key)!.notes.push(note);
  }

  return Array.from(groups.entries()).map(([key, g]) => ({ key, ...g }));
}

// ── Component ─────────────────────────────────────────────────────────────────

interface VaultNotesListProps {
  notes: VaultNote[];
  selectedPath: string | null;
  onSelect: (note: VaultNote) => void;
  isLoading: boolean;
  isError: boolean;
  query?: string;
  scope?: string;
  hasNextPage?: boolean;
  isFetchingNextPage?: boolean;
  onLoadMore?: () => void;
}

export function VaultNotesList({
  notes,
  selectedPath,
  onSelect,
  isLoading,
  isError,
  query,
  scope,
  hasNextPage,
  isFetchingNextPage,
  onLoadMore,
}: VaultNotesListProps) {
  const t = useTranslations("vault");
  const locale = useLocale();
  const observerRef = useRef<IntersectionObserver | null>(null);

  // Infinite scroll sentinel
  const sentinelRef = useCallback(
    (node: HTMLDivElement | null) => {
      if (!node || !onLoadMore) return;
      if (observerRef.current) observerRef.current.disconnect();
      observerRef.current = new IntersectionObserver(
        ([entry]) => {
          if (entry.isIntersecting && hasNextPage && !isFetchingNextPage) {
            onLoadMore();
          }
        },
        { threshold: 0.5 }
      );
      observerRef.current.observe(node);
    },
    [hasNextPage, isFetchingNextPage, onLoadMore]
  );

  if (isLoading) {
    // Skeleton mimics the marginalia + content split so the layout doesn't
    // jolt when real rows replace placeholders.
    return (
      <div className="flex flex-col">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="flex animate-pulse">
            {/* Marginalia placeholder */}
            <div className="shrink-0 w-[92px] py-5 pl-3 pr-4 flex flex-col items-end gap-1.5">
              <div
                className="h-7 w-9 rounded-sm"
                style={{ background: "var(--color-bg-hover)" }}
              />
              <div
                className="h-2.5 w-8 rounded-sm"
                style={{ background: "var(--color-bg-elevated)" }}
              />
              <div
                className="h-2 w-10 rounded-sm"
                style={{ background: "var(--color-bg-surface)" }}
              />
            </div>
            {/* Rule */}
            <div
              className="shrink-0 w-px self-stretch"
              style={{ background: "var(--color-bg-elevated)" }}
            />
            {/* Content placeholder */}
            <div className="flex-1 py-5 px-5">
              <div className="flex items-center gap-2 mb-2.5">
                <div
                  className="h-4 w-16 rounded-sm"
                  style={{ background: "var(--color-bg-elevated)" }}
                />
                <div
                  className="h-2.5 w-14 rounded-sm"
                  style={{ background: "var(--color-bg-elevated)" }}
                />
              </div>
              <div
                className="h-4 w-3/5 rounded-sm mb-2"
                style={{ background: "var(--color-bg-hover)" }}
              />
              <div
                className="h-3 w-full rounded-sm mb-1.5"
                style={{ background: "var(--color-bg-elevated)" }}
              />
              <div
                className="h-3 w-4/5 rounded-sm"
                style={{ background: "var(--color-bg-elevated)" }}
              />
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (isError) {
    return <VaultEmptyState isError />;
  }

  if (notes.length === 0) {
    return <VaultEmptyState query={query} scope={scope} />;
  }

  const groups = groupByMonth(notes, locale);

  return (
    <div className="flex flex-col">
      {groups.map(({ key, label, notes: groupNotes }) => (
        <div key={key}>
          <MonthMarker label={label ?? t("undated")} />
          <div>
            {groupNotes.map((note) => (
              <VaultNoteRow
                key={note.path}
                note={note}
                selected={selectedPath === note.path}
                onSelect={onSelect}
              />
            ))}
          </div>
        </div>
      ))}

      {/* Infinite scroll sentinel */}
      {onLoadMore && (
        <div ref={sentinelRef} className="py-4 flex justify-center">
          {isFetchingNextPage && (
            <span
              className="font-mono uppercase"
              style={{
                fontSize: "10px",
                letterSpacing: "0.18em",
                color: "var(--color-text-muted)",
              }}
            >
              {t("loadingMore")}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
