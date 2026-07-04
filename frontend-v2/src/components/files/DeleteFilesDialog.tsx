"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2 } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";
import type { FsRoot } from "@/lib/types";
import { fileIcon, fileIconColor } from "./fileUtils";

interface DeleteFilesDialogProps {
  open: boolean;
  root: FsRoot;
  subpaths: string[];
  onClose: () => void;
  onDone: () => void;
}

/** Last path segment — the human-readable filename. */
function basename(sub: string): string {
  const i = sub.lastIndexOf("/");
  return i >= 0 ? sub.slice(i + 1) : sub;
}

/**
 * Confirmation dialog for moving files to the trash. The action is recoverable
 * (backend moves to ~/.mc/.trash), so the copy stays calm rather than alarming —
 * but the confirm button still reads as destructive (red tint).
 */
export function DeleteFilesDialog({
  open, root, subpaths, onClose, onDone,
}: DeleteFilesDialogProps) {
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => api.files.delete(root.key, subpaths),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["files-list"] });
      qc.invalidateQueries({ queryKey: ["files-roots"] });
      notify.success(
        res.skipped.length
          ? `${res.trashed.length} in Papierkorb · ${res.skipped.length} übersprungen`
          : `${res.trashed.length} in den Papierkorb verschoben`,
      );
      onDone();
    },
    onError: () => {
      notify.error("Löschen fehlgeschlagen");
    },
  });

  const count = subpaths.length;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="delete-files-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2
          id="delete-files-title"
          className="text-base font-semibold"
          style={{ color: C.textPrimary }}
        >
          {count} Datei{count === 1 ? "" : "en"} löschen?
        </h2>
      </div>

      {/* Scrollable file list */}
      <div className="px-5 py-3 overflow-y-auto" style={{ maxHeight: "40vh" }}>
        <ul className="flex flex-col gap-0.5">
          {subpaths.map((sub) => {
            const name = basename(sub);
            const Icon = fileIcon(name, false);
            const color = fileIconColor(name, false);
            return (
              <li key={sub} className="flex items-center gap-2.5 py-1 min-w-0">
                <Icon size={15} style={{ color, flexShrink: 0 }} />
                <span className="text-sm truncate" style={{ color: C.textPrimary }}>{name}</span>
                <span className="text-xs font-mono truncate ml-auto pl-3" style={{ color: C.textMuted }}>
                  {sub}
                </span>
              </li>
            );
          })}
        </ul>
      </div>

      {/* Trash note — calm, reversible */}
      <div className="px-5 pb-3 shrink-0">
        <div
          className="rounded-xl px-3 py-2.5 text-xs"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderSubtle}`, color: C.textSecondary }}
        >
          In den Papierkorb (~/.mc/.trash) — wiederherstellbar.
        </div>
      </div>

      {/* Footer */}
      <div
        className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
        style={{ borderTop: `1px solid ${C.borderSubtle}` }}
      >
        <button
          onClick={onClose}
          disabled={mutation.isPending}
          className="px-3.5 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          onMouseEnter={(e) => { if (!mutation.isPending) e.currentTarget.style.background = C.bgHover; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          Abbrechen
        </button>
        <button
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending}
          className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-sm font-semibold transition-opacity cursor-pointer disabled:opacity-70 disabled:cursor-not-allowed"
          style={{ background: C.error, color: C.textPrimary }}
        >
          {mutation.isPending
            ? <Loader2 size={15} className="animate-spin" />
            : <Trash2 size={15} />}
          Löschen
        </button>
      </div>
    </ResponsiveModal>
  );
}
