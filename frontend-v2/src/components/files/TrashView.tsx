"use client";

import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { motion } from "framer-motion";
import { Loader2, Trash2, RotateCcw, X } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { TrashEntry } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { fileIcon, fileIconColor, humanSize } from "./fileUtils";
import { PurgeTrashDialog } from "./PurgeTrashDialog";

/** Group entries by their deleted_at bucket, newest bucket first. Within a
 *  bucket the source order is preserved (the backend already lists newest-mtime
 *  files first within a timestamp directory). */
function groupByDeletedAt(entries: TrashEntry[]): [string, TrashEntry[]][] {
  const groups = new Map<string, TrashEntry[]>();
  for (const e of entries) {
    const arr = groups.get(e.deleted_at);
    if (arr) arr.push(e);
    else groups.set(e.deleted_at, [e]);
  }
  return Array.from(groups.entries()).sort((a, b) => b[0].localeCompare(a[0]));
}

/** Absolute, locale-friendly rendering of an ISO deleted_at for the group header. */
function formatDeletedAt(iso: string): string {
  try {
    return new Date(iso).toLocaleString("de-CH", {
      day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

export function TrashView() {
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [purgeOpen, setPurgeOpen] = useState(false);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["files-trash"],
    queryFn: () => api.files.trash.list(),
    refetchInterval: 30_000,
  });

  const entries = useMemo(() => data?.entries ?? [], [data]);
  const groups = useMemo(() => groupByDeletedAt(entries), [entries]);
  const allIds = useMemo(() => entries.map((e) => e.trash_id), [entries]);

  function invalidateAfterMutation() {
    qc.invalidateQueries({ queryKey: ["files-trash"] });
    qc.invalidateQueries({ queryKey: ["files-list"] });
    qc.invalidateQueries({ queryKey: ["files-roots"] });
  }

  const restore = useMutation({
    mutationFn: (ids: string[]) => api.files.trash.restore(ids),
    onSuccess: (res) => {
      invalidateAfterMutation();
      setSelected(new Set());
      notify.success(
        `${res.restored.length} restored${res.skipped.length ? ` · ${res.skipped.length} skipped` : ""}`,
      );
    },
    onError: () => notify.error("Restore failed"),
  });

  function toggle(id: string, on: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function toggleAll(on: boolean) {
    setSelected(on ? new Set(allIds) : new Set());
  }

  const allSelected = allIds.length > 0 && selected.size === allIds.length;
  const someSelected = selected.size > 0 && !allSelected;
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  // ── Loading / error / empty ────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-16">
        <Loader2 size={18} className="animate-spin" style={{ color: C.accent }} />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex items-center gap-2 px-4 py-12 justify-center">
        <Trash2 size={16} style={{ color: C.error }} />
        <span className="text-sm" style={{ color: C.textMuted }}>
          Failed to load trash
        </span>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-16">
        <Trash2 size={28} style={{ color: C.textDim }} />
        <p className="text-sm" style={{ color: C.textMuted }}>Trash is empty</p>
      </div>
    );
  }

  return (
    <>
      {/* Header row: count + "Empty trash" */}
      <div className="flex items-center justify-between mb-3 gap-3">
        <div className="flex items-center gap-2.5">
          <input
            ref={(el) => {
              selectAllRef.current = el;
              if (el) el.indeterminate = someSelected;
            }}
            type="checkbox"
            checked={allSelected}
            aria-label="Select all files in trash"
            onChange={(e) => toggleAll(e.target.checked)}
            className="cursor-pointer"
            style={{ accentColor: C.accent }}
          />
          <span className="text-sm" style={{ color: C.textMuted }}>
            {entries.length} in trash
          </span>
        </div>

        <button
          onClick={() => setPurgeOpen(true)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer"
          style={{ color: STATUS_TEXT.error, border: `1px solid rgba(194,56,56,0.30)` }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(194,56,56,0.12)"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          <Trash2 size={15} />
          Empty trash
        </button>
      </div>

      {/* Grouped list */}
      <div className="rounded-2xl overflow-hidden" style={{ background: "rgba(255,255,255,0.03)", border: `1px solid ${C.border}` }}>
        {groups.map(([deletedAt, groupEntries], gi) => (
          <div key={deletedAt}>
            <div
              className="px-4 py-2.5 flex items-center justify-between"
              style={{
                borderBottom: `1px solid ${C.borderSubtle}`,
                borderTop: gi === 0 ? "none" : `1px solid ${C.borderSubtle}`,
                background: "rgba(255,255,255,0.015)",
              }}
            >
              <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: C.textSecondary }}>
                {formatDeletedAt(deletedAt)}
              </span>
              <span className="text-[11px]" style={{ color: C.textMuted }}>
                {timeAgo(deletedAt)}
              </span>
            </div>

            {groupEntries.map((entry) => {
              const Icon = fileIcon(entry.name, false);
              const color = fileIconColor(entry.name, false);
              const checked = selected.has(entry.trash_id);
              return (
                <div
                  key={entry.trash_id}
                  className="flex items-center gap-3 px-4 py-2.5 transition-colors"
                  style={{ borderBottom: `1px solid ${C.borderSubtle}`, background: checked ? C.accentSubtle : "transparent" }}
                  onMouseEnter={(e) => { if (!checked) e.currentTarget.style.background = "rgba(255,255,255,0.02)"; }}
                  onMouseLeave={(e) => { if (!checked) e.currentTarget.style.background = "transparent"; }}
                >
                  <input
                    type="checkbox"
                    checked={checked}
                    aria-label={`Select ${entry.name}`}
                    onChange={(e) => toggle(entry.trash_id, e.target.checked)}
                    className="cursor-pointer shrink-0"
                    style={{ accentColor: C.accent }}
                  />
                  <Icon size={15} style={{ color, flexShrink: 0 }} />
                  <div className="flex-1 min-w-0">
                    <div className="text-sm truncate" style={{ color: C.textPrimary }}>{entry.name}</div>
                    <div className="text-xs font-mono truncate" style={{ color: C.textMuted }}>
                      {entry.original_root} · {entry.original_subpath}
                    </div>
                  </div>
                  <span className="text-xs tabular-nums shrink-0 hidden sm:inline" style={{ color: C.textMuted }}>
                    {humanSize(entry.size)}
                  </span>
                  <button
                    onClick={() => restore.mutate([entry.trash_id])}
                    disabled={restore.isPending}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors cursor-pointer shrink-0 disabled:opacity-60 disabled:cursor-not-allowed"
                    style={{ color: C.accent }}
                    onMouseEnter={(e) => { if (!restore.isPending) e.currentTarget.style.background = C.bgHover; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
                  >
                    <RotateCcw size={14} />
                    <span className="hidden sm:inline">Restore</span>
                  </button>
                </div>
              );
            })}
          </div>
        ))}
      </div>

      {/* Floating multi-select action bar */}
      {selected.size > 0 && (
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 16 }}
          transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
          className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 flex items-center gap-1.5 px-2.5 py-2 rounded-xl"
          style={{
            background: C.bgElevated,
            border: `1px solid ${C.border}`,
            boxShadow: "0 8px 28px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
        >
          <span className="px-2 text-sm tabular-nums whitespace-nowrap" style={{ color: C.textSecondary }}>
            {selected.size} selected
          </span>
          <div className="w-px h-5 mx-0.5" style={{ background: C.border }} />
          <button
            onClick={() => restore.mutate(Array.from(selected))}
            disabled={restore.isPending}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
            style={{ color: C.accent }}
            onMouseEnter={(e) => { if (!restore.isPending) e.currentTarget.style.background = C.bgHover; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
          >
            {restore.isPending
              ? <Loader2 size={15} className="animate-spin" />
              : <RotateCcw size={15} />}
            Restore
          </button>
          <div className="w-px h-5 mx-0.5" style={{ background: C.border }} />
          <button
            onClick={() => setSelected(new Set())}
            className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors cursor-pointer"
            style={{ color: C.textMuted }}
            onMouseEnter={(e) => { e.currentTarget.style.background = C.bgHover; e.currentTarget.style.color = C.textSecondary; }}
            onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = C.textMuted; }}
          >
            <X size={15} />
            Cancel
          </button>
        </motion.div>
      )}

      {purgeOpen && (
        <PurgeTrashDialog
          open={purgeOpen}
          trashIds={allIds}
          onClose={() => setPurgeOpen(false)}
          onDone={() => { setPurgeOpen(false); setSelected(new Set()); }}
        />
      )}
    </>
  );
}
