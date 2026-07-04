"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Download, Trash2, X, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { FsRoot } from "@/lib/types";
import { DeleteFilesDialog } from "./DeleteFilesDialog";

interface FilesActionBarProps {
  root: FsRoot;
  selected: Set<string>;
  onClear: () => void;
}

/** Last path segment — used as the download filename. */
function basename(sub: string): string {
  const i = sub.lastIndexOf("/");
  return i >= 0 ? sub.slice(i + 1) : sub;
}

/**
 * Floating action bar shown while files are selected. Sits above the page
 * (z-40) but below the preview panel / modals (z-50). Downloads run
 * sequentially so the browser doesn't drop concurrent saves; delete opens a
 * confirm dialog and is gated on the root being writable.
 */
export function FilesActionBar({ root, selected, onClear }: FilesActionBarProps) {
  const [downloading, setDownloading] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const subpaths = Array.from(selected);
  const canDelete = root.deletable;

  async function downloadAll() {
    setDownloading(true);
    try {
      for (const sub of selected) {
        try {
          const url = await api.files.fetchBlob(root.key, sub);
          const a = document.createElement("a");
          a.href = url;
          a.download = basename(sub);
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
        } catch {
          notify.error(`Failed to load ${basename(sub)}`);
        }
      }
    } finally {
      setDownloading(false);
    }
  }

  return (
    <>
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
        <span
          className="px-2 text-sm tabular-nums whitespace-nowrap"
          style={{ color: C.textSecondary }}
        >
          {selected.size} selected
        </span>

        <div className="w-px h-5 mx-0.5" style={{ background: C.border }} />

        <button
          onClick={downloadAll}
          disabled={downloading}
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ color: C.textPrimary }}
          onMouseEnter={(e) => { if (!downloading) e.currentTarget.style.background = C.bgHover; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          {downloading
            ? <Loader2 size={15} className="animate-spin" />
            : <Download size={15} />}
          Download
        </button>

        <button
          onClick={() => { if (canDelete) setDeleteOpen(true); }}
          disabled={!canDelete}
          title={canDelete ? undefined : "This area is read-only — delete not available"}
          aria-disabled={!canDelete}
          className={`inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors ${canDelete ? "cursor-pointer" : "cursor-not-allowed opacity-60"}`}
          style={{ color: STATUS_TEXT.error }}
          onMouseEnter={(e) => { if (canDelete) e.currentTarget.style.background = C.bgHover; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; }}
        >
          <Trash2 size={15} />
          Delete
        </button>

        <div className="w-px h-5 mx-0.5" style={{ background: C.border }} />

        <button
          onClick={onClear}
          className="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm font-medium transition-colors cursor-pointer"
          style={{ color: C.textMuted }}
          onMouseEnter={(e) => { e.currentTarget.style.background = C.bgHover; e.currentTarget.style.color = C.textSecondary; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = C.textMuted; }}
        >
          <X size={15} />
          Cancel
        </button>
      </motion.div>

      {deleteOpen && (
        <DeleteFilesDialog
          open={deleteOpen}
          root={root}
          subpaths={subpaths}
          onClose={() => setDeleteOpen(false)}
          onDone={() => { setDeleteOpen(false); onClear(); }}
        />
      )}
    </>
  );
}
