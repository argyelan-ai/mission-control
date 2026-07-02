"use client";

import { useRef, useCallback } from "react";
import type { VaultNote } from "@/lib/types";
import { VaultNoteRow, MonthMarker, parseDateFromNote } from "./VaultNoteRow";
import { VaultEmptyState } from "./VaultEmptyState";

// ── Grouping ──────────────────────────────────────────────────────────────────
//
// Group by MONTH using parseDateFromNote — pulls frontmatter.date first, then
// path stem, then null. Far fewer notes end up in "Undated" than the previous
// path-only logic (most vault notes name files by slug, not date prefix).

function groupByMonth(notes: VaultNote[]): Array<{ label: string; notes: VaultNote[] }> {
  const groups: Map<string, VaultNote[]> = new Map();

  for (const note of notes) {
    const date = parseDateFromNote(note);
    const label = date ? date.monthKey : "Undated";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(note);
  }

  return Array.from(groups.entries()).map(([label, notes]) => ({ label, notes }));
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
                className="h-7 w-9 rounded"
                style={{ background: "rgba(255,255,255,0.07)" }}
              />
              <div
                className="h-2.5 w-8 rounded"
                style={{ background: "rgba(255,255,255,0.05)" }}
              />
              <div
                className="h-2 w-10 rounded"
                style={{ background: "rgba(255,255,255,0.03)" }}
              />
            </div>
            {/* Rule */}
            <div
              className="shrink-0 w-px self-stretch"
              style={{ background: "rgba(255,255,255,0.04)" }}
            />
            {/* Content placeholder */}
            <div className="flex-1 py-5 px-5">
              <div className="flex items-center gap-2 mb-2.5">
                <div
                  className="h-4 w-16 rounded-sm"
                  style={{ background: "rgba(255,255,255,0.06)" }}
                />
                <div
                  className="h-2.5 w-14 rounded"
                  style={{ background: "rgba(255,255,255,0.04)" }}
                />
              </div>
              <div
                className="h-4 w-3/5 rounded mb-2"
                style={{ background: "rgba(255,255,255,0.08)" }}
              />
              <div
                className="h-3 w-full rounded mb-1.5"
                style={{ background: "rgba(255,255,255,0.04)" }}
              />
              <div
                className="h-3 w-4/5 rounded"
                style={{ background: "rgba(255,255,255,0.04)" }}
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

  const groups = groupByMonth(notes);

  return (
    <div className="flex flex-col">
      {groups.map(({ label, notes: groupNotes }) => (
        <div key={label}>
          <MonthMarker label={label} />
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
              loading more…
            </span>
          )}
        </div>
      )}
    </div>
  );
}
