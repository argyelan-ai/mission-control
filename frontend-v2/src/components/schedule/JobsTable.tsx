"use client";

/**
 * JobsTable — main listing of scheduled jobs with search, tag-filter
 * chips, multi-select + bulk actions, and a virtualized-friendly grid.
 */

import { useMemo, useState } from "react";
import { Search, X, Pause, Play, Trash2 } from "lucide-react";
import { AnimatePresence, motion } from "framer-motion";
import type { ScheduledJob } from "@/lib/types";
import { JobRow } from "./JobRow";
import { C, STATUS_TEXT } from "@/lib/colors";

interface JobsTableProps {
  jobs: ScheduledJob[];
  onEdit: (job: ScheduledJob) => void;
  onDelete: (id: string) => void;
  onTrigger: (id: string) => void;
  onToggleEnabled: (id: string, enabled: boolean) => void;
  onSnooze: (id: string) => void;
  onDuplicate: (id: string) => void;
}

export function JobsTable({
  jobs,
  onEdit,
  onDelete,
  onTrigger,
  onToggleEnabled,
  onSnooze,
  onDuplicate,
}: JobsTableProps) {
  const [search, setSearch] = useState("");
  const [activeTags, setActiveTags] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const allTags = useMemo(() => {
    const m = new Map<string, number>();
    for (const j of jobs) {
      for (const t of j.tags ?? []) m.set(t, (m.get(t) ?? 0) + 1);
    }
    return Array.from(m.entries()).sort((a, b) => b[1] - a[1]);
  }, [jobs]);

  const filteredJobs = useMemo(() => {
    const q = search.trim().toLowerCase();
    return jobs.filter((j) => {
      if (q && !j.name.toLowerCase().includes(q)) return false;
      if (activeTags.size > 0) {
        const ts = j.tags ?? [];
        for (const t of activeTags) {
          if (!ts.includes(t)) return false;
        }
      }
      return true;
    });
  }, [jobs, search, activeTags]);

  const toggleTag = (t: string) => {
    setActiveTags((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  };

  const toggleSelected = (id: string, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const allVisibleSelected =
    filteredJobs.length > 0 && filteredJobs.every((j) => selected.has(j.id));

  const toggleSelectAllVisible = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) {
        for (const j of filteredJobs) next.delete(j.id);
      } else {
        for (const j of filteredJobs) next.add(j.id);
      }
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const bulkPause = () => {
    for (const id of selected) onToggleEnabled(id, false);
    clearSelection();
  };
  const bulkEnable = () => {
    for (const id of selected) onToggleEnabled(id, true);
    clearSelection();
  };
  const bulkDelete = () => {
    if (
      typeof window !== "undefined" &&
      !window.confirm(`${selected.size} Job(s) wirklich loeschen?`)
    ) {
      return;
    }
    for (const id of selected) onDelete(id);
    clearSelection();
  };

  return (
    <div className="flex flex-col gap-3">
      {/* ── Search + Tag-Filter ── */}
      <div className="flex flex-col gap-2">
        <div className="relative">
          <Search
            size={14}
            className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2"
            style={{ color: C.textDim }}
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Jobs suchen…"
            aria-label="Jobs suchen"
            className="w-full rounded-lg py-2 pl-9 pr-9 text-sm"
            style={{
              border: `1px solid ${C.border}`,
              background: C.borderSubtle,
              color: C.textPrimary,
            }}
          />
          {search && (
            <button
              type="button"
              onClick={() => setSearch("")}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1"
              style={{ color: C.textDim }}
              aria-label="Suche zuruecksetzen"
            >
              <X size={12} />
            </button>
          )}
        </div>

        {allTags.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
              Tags:
            </span>
            {allTags.map(([tag, count]) => {
              const active = activeTags.has(tag);
              return (
                <button
                  key={tag}
                  type="button"
                  onClick={() => toggleTag(tag)}
                  className="rounded-full border px-2 py-0.5 text-[10px] font-medium transition"
                  style={{
                    borderColor: active ? C.borderAccent : "rgba(255,255,255,0.08)",
                    background: active ? C.accentSubtle : "transparent",
                    color: active ? C.accent : C.textSecondary,
                  }}
                >
                  {tag}
                  <span className="ml-1 opacity-60">{count}</span>
                </button>
              );
            })}
            {activeTags.size > 0 && (
              <button
                type="button"
                onClick={() => setActiveTags(new Set())}
                className="text-[10px]"
                style={{ color: C.textMuted }}
              >
                zuruecksetzen
              </button>
            )}
          </div>
        )}
      </div>

      {/* ── Bulk action bar ── */}
      <AnimatePresence>
        {selected.size > 0 && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="flex items-center justify-between gap-3 rounded-lg px-3 py-2"
            style={{
              border: `1px solid ${C.borderAccent}`,
              background: C.accentSubtle,
            }}
          >
            <span className="text-xs font-medium" style={{ color: C.textPrimary }}>
              {selected.size} ausgewaehlt
            </span>
            <div className="flex items-center gap-1.5">
              <BulkBtn onClick={bulkPause} icon={<Pause size={12} />}>
                Alle pausieren
              </BulkBtn>
              <BulkBtn onClick={bulkEnable} icon={<Play size={12} />}>
                Alle aktivieren
              </BulkBtn>
              <BulkBtn
                onClick={bulkDelete}
                icon={<Trash2 size={12} />}
                danger
              >
                Loeschen
              </BulkBtn>
              <button
                type="button"
                onClick={clearSelection}
                className="ml-1 text-xs"
                style={{ color: C.textSecondary }}
              >
                abbrechen
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* ── Table (horizontal scroll on mobile, M11/M17) ── */}
      <div
        className="overflow-x-auto -mx-4 px-4 md:mx-0 md:px-0"
        style={{ overscrollBehaviorX: "contain" } as React.CSSProperties}
        tabIndex={0}
        role="region"
        aria-label="Jobs Tabelle"
      >
        <div style={{ minWidth: 640 }}>
          {/* Header */}
          <div
            className="grid grid-cols-[24px_24px_minmax(0,2fr)_minmax(0,1.5fr)_1fr_1fr_minmax(0,1fr)_auto] items-center gap-3 px-3 py-1.5 text-[10px] uppercase tracking-wide"
            style={{ color: C.textDim }}
          >
            <input
              type="checkbox"
              checked={allVisibleSelected}
              onChange={toggleSelectAllVisible}
              className="h-3.5 w-3.5 cursor-pointer"
              style={{ accentColor: C.accent }}
              aria-label="Alle sichtbaren auswaehlen"
            />
            <span></span>
            {/* Sticky "Name" header cell — opaque bg so row content doesn't bleed through.
                bgSurface matches the row tone (borderSubtle over bgDeep) — bgBase read as a black hole on mobile. */}
            <span
              className="sticky z-10"
              style={{ left: "72px", backgroundColor: C.bgSurface }}
            >
              Name
            </span>
            <span>Trigger</span>
            <span>Naechster</span>
            <span>Letzter</span>
            <span>Agent</span>
            <span></span>
          </div>

          {/* Rows */}
          <div className="flex flex-col gap-1.5">
            {filteredJobs.length === 0 ? (
              <div
                className="rounded-lg border border-dashed p-8 text-center text-sm"
                style={{ borderColor: C.border, color: C.textDim }}
              >
                {jobs.length === 0
                  ? 'Noch keine Jobs angelegt — leg los mit "Neuer Job".'
                  : "Keine Treffer fuer die aktuellen Filter."}
              </div>
            ) : (
              filteredJobs.map((j) => (
                <JobRow
                  key={j.id}
                  job={j}
                  selected={selected.has(j.id)}
                  onSelectChange={(on) => toggleSelected(j.id, on)}
                  onEdit={onEdit}
                  onDelete={onDelete}
                  onTrigger={onTrigger}
                  onToggleEnabled={onToggleEnabled}
                  onSnooze={onSnooze}
                  onDuplicate={onDuplicate}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function BulkBtn({
  children,
  onClick,
  icon,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  icon: React.ReactNode;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-1 rounded-md border px-2 py-1 text-xs font-medium transition"
      style={{
        borderColor: danger ? `${C.error}59` : "rgba(255,255,255,0.1)",
        background: danger ? `${C.error}1A` : C.borderSubtle,
        color: danger ? STATUS_TEXT.error : C.textPrimary,
      }}
    >
      {icon}
      {children}
    </button>
  );
}
