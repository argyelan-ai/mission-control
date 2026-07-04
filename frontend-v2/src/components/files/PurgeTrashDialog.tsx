"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Trash2, AlertTriangle } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { fileIcon, fileIconColor } from "./fileUtils";

interface PurgeTrashDialogProps {
  open: boolean;
  /** The `.trash`-relative ids to purge (`<ts>/<root_key>/<rel>`). */
  trashIds: string[];
  onClose: () => void;
  onDone: () => void;
}

/** Last path segment of a trash_id — the human-readable filename. */
function basename(trashId: string): string {
  const i = trashId.lastIndexOf("/");
  return i >= 0 ? trashId.slice(i + 1) : trashId;
}

/**
 * Confirmation dialog for emptying the trash — the one irreversible action.
 * Unlike DeleteFilesDialog (calm, reversible), this leans into the warning:
 * a red-tinted alert box, an explicit "cannot be restored" sentence, and a
 * red confirm button. Purge never fires on mount; it requires the click.
 */
export function PurgeTrashDialog({
  open, trashIds, onClose, onDone,
}: PurgeTrashDialogProps) {
  const qc = useQueryClient();

  const mutation = useMutation({
    mutationFn: () => api.files.trash.purge(trashIds),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["files-trash"] });
      qc.invalidateQueries({ queryKey: ["files-list"] });
      qc.invalidateQueries({ queryKey: ["files-roots"] });
      notify.success(`${res.purged.length} deleted permanently`);
      onDone();
    },
    onError: () => {
      notify.error("Failed to empty trash");
    },
  });

  const count = trashIds.length;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="purge-trash-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2
          id="purge-trash-title"
          className="text-base font-semibold"
          style={{ color: C.textPrimary }}
        >
          Empty trash permanently?
        </h2>
      </div>

      {/* Irreversible warning — strong, red-tinted */}
      <div className="px-5 pt-3 shrink-0">
        <div
          className="flex items-start gap-2.5 rounded-xl px-3 py-2.5 text-xs"
          style={{
            background: "rgba(194,56,56,0.12)",
            border: `1px solid rgba(194,56,56,0.30)`,
            color: STATUS_TEXT.error,
          }}
        >
          <AlertTriangle size={15} className="shrink-0 mt-px" />
          <span>
            Irreversible — these files will be permanently deleted and CANNOT
            be restored.
          </span>
        </div>
      </div>

      {/* Scrollable file list */}
      <div className="px-5 py-3 overflow-y-auto" style={{ maxHeight: "40vh" }}>
        <ul className="flex flex-col gap-0.5">
          {trashIds.map((id) => {
            const name = basename(id);
            const Icon = fileIcon(name, false);
            const color = fileIconColor(name, false);
            return (
              <li key={id} className="flex items-center gap-2.5 py-1 min-w-0">
                <Icon size={15} style={{ color, flexShrink: 0 }} />
                <span className="text-sm truncate" style={{ color: C.textPrimary }}>{name}</span>
              </li>
            );
          })}
        </ul>
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
          Cancel
        </button>
        <button
          onClick={() => mutation.mutate()}
          disabled={mutation.isPending || count === 0}
          className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-sm font-semibold transition-opacity cursor-pointer disabled:opacity-70 disabled:cursor-not-allowed"
          style={{ background: C.error, color: C.textPrimary }}
        >
          {mutation.isPending
            ? <Loader2 size={15} className="animate-spin" />
            : <Trash2 size={15} />}
          Delete permanently
        </button>
      </div>
    </ResponsiveModal>
  );
}
